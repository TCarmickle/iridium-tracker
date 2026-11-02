"""Iridium Console - a detailed, burst-by-burst view of the receiver (like the original receive script).

  Iridium Console.bat                 watch what the tracker's receiver is decoding (read-only, runs
                                      alongside the tracker)
  Iridium Console.bat --receive       run the receiver from here, without the tracker
  Iridium Console.bat --capture NAME  print the frames of a finished capture, e.g. output-20261101-142243

Keys while running:  R start/stop receiving here   B beeps on/off   D damaged bursts on/off   Q quit

Only technical frames are described (which satellite, where, signal). Voice, pager and data
contents are never shown.
"""
import argparse
import collections
import ctypes
import datetime
import glob
import json
import msvcrt
import os
import queue
import re
import signal
import sys
import threading
import time
import winsound

os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from PyQt5 import QtCore  # noqa: E402
import paths  # noqa: E402
import catalog  # noqa: E402
import storage  # noqa: E402
import receiver as rx  # noqa: E402

KINDS = {
    'IRA': 'ring alert', 'ITL': 'time & location', 'IBC': 'broadcast', 'IMS': 'paging channel',
    'IRI': 'network control', 'ISY': 'sync', 'IME': 'messaging', 'IDA': 'data channel',
    'VOC': 'voice (private - not shown)', 'IIP': 'data packet', 'IIQ': 'data packet', 'IIR': 'data packet',
    'IIU': 'data packet', 'IU3': 'data packet', 'VDA': 'voice data (private - not shown)',
}
GREEN, GREY, CYAN, YELLOW, RED, BOLD, RESET = ('\x1b[92m', '\x1b[90m', '\x1b[96m', '\x1b[93m', '\x1b[91m',
                                               '\x1b[1m', '\x1b[0m')
STALE_S = 120       # a live file this quiet means its receiver has stopped


def enable_colours():
    k = ctypes.windll.kernel32
    h = k.GetStdHandle(-11)
    mode = ctypes.c_ulong()
    if k.GetConsoleMode(h, ctypes.byref(mode)):
        k.SetConsoleMode(h, mode.value | 0x0004)     # ENABLE_VIRTUAL_TERMINAL_PROCESSING


def set_title(text):
    ctypes.windll.kernel32.SetConsoleTitleW(text)


class Beeper:
    """beeps on its own thread so a long tone never holds up the display"""
    MS = {'damaged': 70, 'ok': 300, 'ira': 1000}

    def __init__(self):
        self.on = False
        self.q = queue.Queue(maxsize=8)
        threading.Thread(target=self._run, daemon=True).start()

    def __call__(self, kind):
        if self.on:
            try:
                self.q.put_nowait(self.MS[kind])
            except queue.Full:
                pass

    def _run(self):
        while True:
            winsound.Beep(1000, self.q.get())


class Names:
    """satellite names for network IDs and plane/slots, from your catalog (static labels)"""

    def __init__(self):
        self.by_net, self.by_slot, self._tles = {}, {}, None
        try:
            caps = json.load(open(catalog.DATA))
        except (OSError, ValueError):
            caps = {}
        for c in caps.values():
            for p in c.get('positions', []):
                if p.get('name'):
                    self.by_net[p['net_id']] = p['name']
            for s in c.get('slots', []):
                if s.get('name') and s.get('confidence') == 'confident':
                    self.by_slot[(s['plane'], s['slot'])] = s['name']

    def for_fix(self, net_id, t, lat, lon):
        if net_id not in self.by_net:
            if self._tles is None:
                try:
                    self._tles = catalog.load_tles()
                except Exception:
                    self._tles = []
            hit = catalog.match_position(self._tles, datetime.datetime.fromtimestamp(t, datetime.timezone.utc), lat, lon)
            if hit and hit[1] > 85:
                self.by_net[net_id] = hit[0]
        return self.by_net.get(net_id)


class Console(QtCore.QObject):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.beep = Beeper()
        self.beep.on = args.beep
        self.show_damaged = not args.no_damaged
        self.names = Names()
        self.rx = rx.Receiver()
        self.rx.status.connect(self._rx_status)
        self.rx.running.connect(self._rx_running)
        self.cap = None             # capture being followed
        self.pos = {}               # file -> read position
        self.start_t = None
        self.reset_counts()
        self.last_status = 0
        self.waiting_shown = False
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(250)

    def reset_counts(self):
        self.kinds = collections.Counter()
        self.heard = self.decoded = 0
        self.sats, self.fixes = set(), []
        self.first_t = self.last_t = None

    # ---- what to follow
    def newest_live(self):
        lives = sorted(glob.glob(os.path.join(paths.CAPTURES, 'output-*.live')), key=os.path.getmtime)
        return lives[-1] if lives else None

    def follow(self, live):
        self.cap = os.path.basename(live)[:-5]
        self.pos = {live: 0, live[:-5] + '.log': 0}
        self.start_t = None
        self.reset_counts()
        mine = self.rx.busy and self.cap == 'output-%s' % self.rx.stamp
        print('\n%s=== %s %s ===%s' % (BOLD, 'Receiving into' if mine else 'Watching', self.cap, RESET))
        # catch up quietly on what was decoded before we started watching
        size = os.path.getsize(live) if os.path.exists(live) else 0
        if size and not mine:
            self._read(live, quiet=True)
            print('%s(%d frames earlier in this capture: %d decoded, %d positions - showing new ones)%s'
                  % (GREY, self.heard + self.decoded, self.decoded, len(self.fixes), RESET))

    def tick(self):
        self.keys()
        if self.rx.busy:        # receiving from here: follow our own capture once it has frames
            target = self.rx.live_path       # (its decoder status shows before any frame is decoded)
        else:                   # watching: the capture the tracker is writing right now
            live = self.newest_live()
            target = live if live and time.time() - os.path.getmtime(live) < STALE_S else None
        if target and (self.cap is None or os.path.basename(target)[:-5] != self.cap):
            self.follow(target)
        if self.cap:
            path = os.path.join(paths.CAPTURES, self.cap + '.live')
            self._read(path)
            self._read_log(path[:-5] + '.log')
            if not self.rx.busy and (not os.path.exists(path) or time.time() - os.path.getmtime(path) > STALE_S)                     and not self.waiting_shown:
                print('%s--- the receiver stopped (no new frames for %d s) ---%s' % (YELLOW, STALE_S, RESET))
                self.summary()
                self.cap = None
        if self.cap is None and not self.rx.busy and not self.waiting_shown:
            print('%sWaiting for a receiver: press "Start receiver" in the tracker, or R to receive here.%s'
                  % (CYAN, RESET))
            self.waiting_shown = True
        if self.cap:
            self.waiting_shown = False
        set_title('Iridium Console - %s - %d decoded, %d damaged, %d positions, %d satellites'
                  % (self.cap or 'waiting', self.decoded, self.heard, len(self.fixes), len(self.sats)))

    # ---- reading
    def _clock(self, ms):
        if self.start_t is None:
            try:
                self.start_t = catalog.capture_start(os.path.join(paths.CAPTURES, self.cap))[0].timestamp()
            except Exception:
                return time.time()
        return self.start_t + ms / 1000

    def _read(self, path, quiet=False):
        try:
            with open(path, 'rb') as f:
                f.seek(self.pos.get(path, 0))
                data = f.read()
        except OSError:
            return
        data = data[:data.rfind(b'\n') + 1]     # leave a half-written line for next time
        self.pos[path] = self.pos.get(path, 0) + len(data)
        for line in data.decode('utf-8', 'replace').splitlines():
            self.frame(line.strip(), quiet)

    def frame(self, line, quiet=False):
        f = line.split()
        if len(f) < 6 or not f[0].endswith(':'):
            return
        kind = f[0][:-1]
        try:
            t = self._clock(float(f[2]))
            hz = int(f[3])
        except ValueError:
            return
        snr = float(f[5].split('|')[2]) if f[5].count('|') == 2 else 0.0
        self.first_t = self.first_t or t
        self.last_t = t
        bar = '#' * max(1, min(30, int(snr - 10)))
        when = datetime.datetime.fromtimestamp(t).strftime('%H:%M:%S')
        if kind in ('RAW', 'ERR'):
            self.heard += 1
            if not quiet and self.show_damaged:
                print('%s[%s]  heard    %5.1f dB  %-24s damaged burst - could not decode%s' % (GREY, when, snr, bar, RESET))
                self.beep('damaged')
            return
        self.decoded += 1
        self.kinds[kind] += 1
        what = KINDS.get(kind, 'other')
        extra, colour = '', GREEN
        m = re.search(r' P(\d) S(\d+) ', line)
        if kind == 'ITL':
            extra = '(no plane/slot in this part)'
        if kind == 'ITL' and m:
            ps = (int(m.group(1)), int(m.group(2)))
            name = self.names.by_slot.get(ps)
            if name:
                self.sats.add(name)
            extra = 'plane %d #%d%s' % (ps[0], ps[1], ' = ' + name if name else '')
        m = re.search(r'sat:(\d+) cell:(\d+)', line)
        if kind == 'IBC' and m:
            extra = 'satellite %03d, beam %s' % (int(m.group(1)), m.group(2))
        if kind == 'IRA':
            m = re.search(r'sat:(\d+) beam:(\d+).*?pos=\(([-+\d.]+)/([-+\d.]+)\) alt=(-?\d+)', line)
            if m and int(m.group(5)) > 100:
                net, lat, lon, alt = int(m.group(1)), float(m.group(3)), float(m.group(4)), int(m.group(5))
                name = self.names.for_fix(net, t, lat, lon)
                if name:
                    self.sats.add(name)
                self.fixes.append((t, name or 'net ID %03d' % net, lat, lon, alt))
                extra = '%s(sat %03d, beam %s) at %+.2f, %+.2f, %d km up%s' % (
                    BOLD + (name + ' ' if name else ''), net, m.group(2), lat, lon, alt, RESET)
                colour = CYAN
            else:
                extra = '(no position in this one)'
        if not quiet:
            print('%s[%s]  DECODED  %5.1f dB  %-24s %s %-22s %s%s  %s%.4f MHz%s' % (
                colour, when, snr, bar, kind, what, extra, colour, GREY, hz / 1e6, RESET))
            self.beep('ira' if colour == CYAN else 'ok')

    def _read_log(self, path):
        """the decoder's own status: errors right away, its totals every 10 s"""
        try:
            with open(path, errors='replace') as f:
                f.seek(self.pos.get(path, 0))
                text = f.read()
                self.pos[path] = f.tell()
        except OSError:
            return
        for line in text.splitlines():
            m = re.match(r'^(\d{10}) \|.*?i:\s*(\d+)/s.*?ok_avg:\s*(\d+)%.*?ok:\s*(\d+) ', line)
            if m:
                if time.time() - self.last_status >= 10:
                    self.last_status = time.time()
                    print('%s[decoder] %s bursts/s in  ·  %s%% decodable on average  ·  %s bursts so far%s'
                          % (YELLOW, m.group(2), m.group(3), m.group(4), RESET))
            elif line.strip() and not re.search(r'DeprecationWarning|setDaemon|^\s', line):
                print('%s[decoder] %s%s' % (YELLOW, line.strip(), RESET))

    # ---- receiver run from here
    def _rx_running(self, on):
        if not on and self.cap and self.cap == 'output-%s' % self.rx.stamp:
            path = os.path.join(paths.CAPTURES, self.cap + '.live')
            self._read(path)
            self.summary()
            self.cap = None
            self.waiting_shown = False

    def _rx_status(self, text):
        if not text.startswith('Receiving'):
            print('%s[receiver] %s%s' % (YELLOW, text, RESET))

    def keys(self):
        while msvcrt.kbhit():
            k = msvcrt.getwch().lower()
            if k == 'r':
                if self.rx.state == 'running':
                    self.rx.stop()
                elif self.rx.busy:
                    print('%s[receiver] still finishing the last capture...%s' % (YELLOW, RESET))
                elif self.cap and time.time() - os.path.getmtime(os.path.join(paths.CAPTURES, self.cap + '.live')) < STALE_S:
                    print('%sThe tracker is already receiving - stop it there first (only one program can use '
                          'the SDR).%s' % (RED, RESET))
                elif catalog.HOME is None:
                    print('%sSet your location in the tracker first (click it on the map).%s' % (RED, RESET))
                else:
                    self.rx.start()
            elif k == 'b':
                self.beep.on = not self.beep.on
                print('%sbeeps %s%s' % (GREY, 'on' if self.beep.on else 'off', RESET))
            elif k == 'd':
                self.show_damaged = not self.show_damaged
                print('%sdamaged bursts %s%s' % (GREY, 'shown' if self.show_damaged else 'hidden', RESET))
            elif k == 'q':
                self.quit()

    def summary(self):
        mins = (self.last_t - self.first_t) / 60 if self.first_t else 0
        print('%s--- %s: %d decoded, %d damaged in %.0f min%s' % (BOLD, self.cap, self.decoded, self.heard, mins, RESET))
        if self.kinds:
            print('    by type: ' + ', '.join('%s %d' % kv for kv in self.kinds.most_common()))
        if self.sats:
            print('    satellites identified: ' + ', '.join(sorted(self.sats, key=lambda n: int(re.sub(r'\D', '', n) or 0))))
        for t, name, lat, lon, alt in self.fixes[-10:]:
            print('    position  %s  %-12s %+.2f, %+.2f  %d km' % (datetime.datetime.fromtimestamp(t).strftime('%H:%M:%S'),
                                                              name, lat, lon, alt))

    def quit(self):
        if self.rx.state == 'running':
            print('%sStopping the receiver and adding the capture to the catalog...%s' % (YELLOW, RESET))
            self.rx.stop()
        if self.rx.busy:
            QtCore.QTimer.singleShot(500, self.quit)
            return
        if self.cap:
            self.summary()
        QtCore.QCoreApplication.quit()


def print_capture(name):
    """print a finished capture's frames"""
    stem = os.path.join(paths.CAPTURES, name)
    path = next((stem + e for e in ('.parsed', '.live', storage.REDUCED) if os.path.exists(stem + e)), None)
    if not path:
        sys.exit('No decoded frames for %s in %s' % (name, paths.CAPTURES))
    if path.endswith(storage.REDUCED):
        print('(only ring alerts and time & location frames are kept for this capture)')
        lines = storage.read_reduced(path)[1]
    else:
        lines = open(path, errors='replace')
    app = QtCore.QCoreApplication(sys.argv)  # noqa: F841 (the receiver object needs one)
    c = Console(argparse.Namespace(beep=False, no_damaged=True))
    c.timer.stop()
    c.cap = name
    for line in lines:
        c.frame(line.strip())
    c.summary()


def main():
    ap = argparse.ArgumentParser(description='Iridium Console - detailed live view of the receiver')
    ap.add_argument('--receive', action='store_true', help='run the receiver from here (not from the tracker)')
    ap.add_argument('--capture', help='print the frames of a finished capture')
    ap.add_argument('--beep', action='store_true', help='start with beeps on (short = damaged, long = decoded, '
                                                        'extra long = ring alert with a position)')
    ap.add_argument('--no-damaged', action='store_true', help="don't list damaged bursts")
    args = ap.parse_args()
    enable_colours()
    if args.capture:
        print_capture(args.capture)
        return
    app = QtCore.QCoreApplication(sys.argv)
    print('%sIRIDIUM CONSOLE%s  -  R receive here  ·  B beeps  ·  D damaged bursts  ·  Q quit' % (BOLD, RESET))
    print('%sGreen = decoded frame, cyan = ring alert with the satellite\'s position, grey = damaged burst.%s'
          % (GREY, RESET))
    c = Console(args)
    signal.signal(signal.SIGINT, lambda *_: c.quit())     # Ctrl+C stops cleanly too
    if args.receive:
        QtCore.QTimer.singleShot(300, lambda: c.keys() or (c.rx.start() if catalog.HOME else
                                                          print('%sSet your location in the tracker first.%s' % (RED, RESET))))
    app.exec_()


if __name__ == '__main__':
    main()
