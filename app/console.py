"""Satellite Console - a detailed, line-by-line log of whatever the receiver hears, on every band:
Iridium bursts, Orbcomm packets, ISS / ham frames, and GNSS (GPS, Galileo, WAAS, GLONASS) tracking.

  Satellite Console.bat                 watch what the tracker's receiver is decoding (read-only, runs
                                        alongside the tracker)
  Satellite Console.bat --receive       receive from here, without the tracker (on the band chosen with
                                        --band, or the tracker's last band)
  Satellite Console.bat --band gnss1    start with that band chosen (iridium, orbcomm, ham2m, ham70, gnss1, glo1, meteor)
  Satellite Console.bat --capture NAME  print a finished capture, e.g. output-20261101-142243 or gnss-20261103-215314

Keys while running:  R start/stop receiving here   1-7 choose the band to receive   B beeps on/off
                     D damaged / undecoded on/off   Q quit

Only technical frames are described (which satellite, where, signal). Voice, pager, message and data
contents are never shown.
"""
import argparse
import collections
import ctypes
import datetime
import glob
import json
import math
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
import sdr_settings  # noqa: E402
import storage  # noqa: E402
import systems  # noqa: E402
import receiver as rx  # noqa: E402

TITLE = 'Satellite Console'
IRIDIUM_KINDS = {
    'IRA': 'ring alert', 'ITL': 'time & location', 'IBC': 'broadcast', 'IMS': 'paging channel',
    'IRI': 'network control', 'ISY': 'sync', 'IME': 'messaging', 'IDA': 'data channel',
    'VOC': 'voice (private - not shown)', 'IIP': 'data packet', 'IIQ': 'data packet', 'IIR': 'data packet',
    'IIU': 'data packet', 'IU3': 'data packet', 'VDA': 'voice data (private - not shown)',
}
GREEN, GREY, CYAN, YELLOW, RED, MAGENTA, BOLD, RESET = ('\x1b[92m', '\x1b[90m', '\x1b[96m', '\x1b[93m', '\x1b[91m',
                                                        '\x1b[95m', '\x1b[1m', '\x1b[0m')
GNSS_EVERY_S = 15   # a tracked GNSS satellite's line: once in this many seconds (its numbers come every second)
GNSS_LOST_S = 10    # ...and it counts as lost after this long without one
BANDS = [b for b in systems.ORDER if b is not systems.RADIO]   # what keys 1-7 choose (Radio needs the tracker's sound)


def enable_colours():
    k = ctypes.windll.kernel32
    h = k.GetStdHandle(-11)
    mode = ctypes.c_ulong()
    if k.GetConsoleMode(h, ctypes.byref(mode)):
        k.SetConsoleMode(h, mode.value | 0x0004)     # ENABLE_VIRTUAL_TERMINAL_PROCESSING


def set_title(text):
    ctypes.windll.kernel32.SetConsoleTitleW(text)


def band_text(band, center_hz=None):
    """'GNSS L1 (GPS) - 1575.420 MHz, 2.048 MS/s (1574.40-1576.44 MHz)'"""
    s = sdr_settings.load()
    s['system'] = band['key']
    c = center_hz or sdr_settings.center_hz(s)
    rate = sdr_settings.sample_rate(s)
    lo, hi = c - rate / 2, c + rate / 2
    return '%s - tuned to %.3f MHz, %.3f MS/s (hears %.2f-%.2f MHz)' % (band['label'], c / 1e6, rate / 1e6, lo / 1e6, hi / 1e6)


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
    """satellite names (static labels) from your catalog: Iridium by network ID and plane/slot, Orbcomm by
    satellite ID"""

    def __init__(self):
        self.by_net, self.by_slot, self.orbcomm, self._tles = {}, {}, {}, None
        try:
            caps = catalog.load_data()
        except (OSError, ValueError, SystemExit):
            caps = {}
        for c in caps.values():
            oc = c.get('system') == 'orbcomm'
            for p in c.get('positions', []):
                if p.get('name'):
                    (self.orbcomm if oc else self.by_net)[p['net_id'] if not oc else str(p['net_id'])] = p['name']
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
        self.band = systems.get(getattr(args, 'band', None) or sdr_settings.load()['system'])   # what R receives
        self.cap = None             # capture being followed
        self.cap_band = None        # ...and the band it is on
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
        self.gnss_last = {}         # GNSS satellite -> (time of its newest numbers, time its line was last printed)
        self.gnss_orbits = set()
        self.meteor_best, self.meteor_printed = None, 0

    # ---- the band
    def choose_band(self, k):
        if self.rx.busy:
            print('%sThe receiver is running on %s - press R to stop it before choosing another band.%s'
                  % (RED, self.rx.system['label'], RESET))
            return
        self.band = BANDS[k]
        print('%sBand for R: [%d] %s%s' % (BOLD, k + 1, band_text(self.band), RESET))
        print('%s   antenna: %s%s' % (GREY, self.band.get('antenna', '-'), RESET))

    def band_menu(self):
        cur = self.band
        print('%sBands (press the number, then R to receive here):%s' % (GREY, RESET))
        for k, b in enumerate(BANDS):
            print('%s  %s[%d] %-34s %s%s' % (BOLD if b is cur else GREY, '>' if b is cur else ' ', k + 1, b['label'],
                                            '%.3f MHz' % ((b['carrier_hz'] if b is systems.METEOR else    # (its signal;
                                                           sdr_settings.center_hz(dict(sdr_settings.load(), system=b['key'])))
                                                          / 1e6),        # the SDR tunes 200 kHz below it)
                                            RESET))

    # ---- what to follow
    def follow(self, live):
        self.cap = os.path.basename(live)[:-5]
        self.cap_band = systems.of_capture(self.cap)
        self.pos = {live: 0, live[:-5] + '.log': 0}
        self.start_t = None
        self.reset_counts()
        mine = self.rx.busy and self.cap == self.rx.capname
        centre = sdr_settings.center_hz(self.rx.sdr) if mine else None
        print('\n%s=== %s %s ===%s' % (BOLD, 'Receiving into' if mine else 'Watching', self.cap, RESET))
        print('%sBand: %s%s' % (BOLD, band_text(self.cap_band, centre), RESET))
        # catch up quietly on what was decoded before we started watching
        size = os.path.getsize(live) if os.path.exists(live) else 0
        if size and not mine:
            self._read(live, quiet=True)
            print('%s(earlier in this capture: %d decoded, %d heard but not decoded, %d positions - showing new ones)%s'
                  % (GREY, self.decoded, self.heard, len(self.fixes), RESET))
            # the satellites still tracked get their lines from now on; ones already lost are dropped quietly
            self.gnss_last = {s: (t, 0) for s, (t, _) in self.gnss_last.items() if self.last_t - t <= GNSS_LOST_S}

    def tick(self):
        self.keys()
        if self.rx.busy:        # receiving from here: follow our own capture once it has frames
            target = self.rx.live_path       # (its decoder status shows before any frame is decoded)
        else:                   # watching: the capture another receiver (the tracker's) is receiving right now
            act = rx.active_capture()
            target = os.path.join(paths.CAPTURES, act[0] + '.live') if act else None
        if target and (self.cap is None or os.path.basename(target)[:-5] != self.cap):
            self.follow(target)
        if self.cap:
            path = os.path.join(paths.CAPTURES, self.cap + '.live')
            self._read(path)
            self._read_log(path[:-5] + '.log')
            self._gnss_lost()
            if not self.rx.busy and target is None and not self.waiting_shown:
                print('%s--- the receiver stopped ---%s' % (YELLOW, RESET))
                self.summary()
                self.cap = None
        if self.cap is None and not self.rx.busy and not self.waiting_shown:
            print('%sWaiting for a receiver: press "Start receiver" in the tracker, or R to receive here on %s '
                  '(1-7 to choose another band).%s' % (CYAN, self.band['label'], RESET))
            self.waiting_shown = True
        if self.cap:
            self.waiting_shown = False
        set_title('%s - %s - %s - %d decoded, %d not decoded, %d positions, %d satellites'
                  % (TITLE, (self.cap_band or self.band)['label'] if self.cap else 'waiting (R: %s)' % self.band['label'],
                     self.cap or '', self.decoded, self.heard, len(self.fixes), len(self.sats)))

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

    @staticmethod
    def _bar(value, system_key):
        lo, hi, _ = systems.get(system_key).get('signal', (10.0, 30.0, 'dB'))
        q = (value - lo) / (hi - lo)
        return '#' * max(1, min(24, int(round(2 + q * 20))))

    def _seen(self, t):
        self.first_t = self.first_t or t
        self.last_t = t

    def frame(self, line, quiet=False):
        kind = line.split(':', 1)[0]
        if kind in ('GCH', 'GEPH', 'GPOS'):
            return self._gnss(kind, line.split()[1:], quiet)
        if kind in ('OEP', 'OSY', 'OPK', 'OBAD'):
            return self._orbcomm(kind, line.split()[1:], quiet)
        if kind in ('HFR', 'HSIG'):
            return self._ham(kind, line.split()[1:], quiet)
        if kind == 'MSIG':
            return self._meteor(line.split()[1:], quiet)
        return self._iridium(line, quiet)

    def _damaged(self, when, value, unit, bar, what, quiet):
        self.heard += 1
        if not quiet and self.show_damaged:
            print('%s[%s]  heard    %5.1f %-5s %-24s %s%s' % (GREY, when, value, unit, bar, what, RESET))
            self.beep('damaged')

    def _iridium(self, line, quiet):
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
        self._seen(t)
        bar = self._bar(snr, 'iridium')
        when = datetime.datetime.fromtimestamp(t).strftime('%H:%M:%S')
        if kind in ('RAW', 'ERR'):
            return self._damaged(when, snr, 'dB', bar, 'damaged burst - could not decode', quiet)
        self.decoded += 1
        self.kinds[kind] += 1
        what = IRIDIUM_KINDS.get(kind, 'other')
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
                self.fixes.append((t, name or 'net ID %03d' % net, lat, lon, '%d km up' % alt))
                extra = '%s(sat %03d, beam %s) at %+.2f, %+.2f, %d km up%s' % (
                    BOLD + (name + ' ' if name else ''), net, m.group(2), lat, lon, alt, RESET)
                colour = CYAN
            else:
                extra = '(no position in this one)'
        if not quiet:
            print('%s[%s]  DECODED  %5.1f dB    %-24s %s %-22s %s%s  %s%.4f MHz%s' % (
                colour, when, snr, bar, kind, what, extra, colour, GREY, hz / 1e6, RESET))
            self.beep('ira' if colour == CYAN else 'ok')

    def _orbcomm(self, kind, f, quiet):
        """OEP t hz sat snr x y z vx vy vz (its own position) / OSY t hz sat snr / OPK t hz sat snr type / OBAD t hz - snr"""
        try:
            t, hz, sid, snr = float(f[0]), float(f[1]), f[2], float(f[3])
        except (IndexError, ValueError):
            return
        self._seen(t)
        when = datetime.datetime.fromtimestamp(t).strftime('%H:%M:%S')
        bar = self._bar(snr, 'orbcomm')
        if kind == 'OBAD':
            return self._damaged(when, snr, 'dB', bar, 'Orbcomm packet - failed its checksum  %s%.4f MHz' % (GREY, hz / 1e6), quiet)
        self.decoded += 1
        self.kinds[kind] += 1
        name = self.names.orbcomm.get(sid)
        who = '%s(satellite ID %s)' % (name + ' ' if name else '', sid) if sid != '-' else '(satellite not known yet)'
        colour, extra = GREEN, ''
        if kind == 'OEP' and len(f) >= 7:
            lat, lon, alt = catalog.ecef_to_lla(float(f[4]), float(f[5]), float(f[6]))
            if name:
                self.sats.add(name)
            self.fixes.append((t, name or 'ID ' + sid, lat, lon, '%.0f km up' % alt))
            colour = CYAN
            extra = '%sephemeris %s at %+.2f, %+.2f, %.0f km up%s' % (BOLD, who, lat, lon, alt, RESET)
        elif kind == 'OSY':
            extra = 'sync %s' % who
        else:
            extra = '%s %s' % ((f[4] if len(f) > 4 else 'packet').replace('_', ' '), who)
        if not quiet:
            print('%s[%s]  DECODED  %5.1f dB    %-24s %s  %s%s  %s%.4f MHz%s'
                  % (colour, when, snr, bar, kind, extra, colour, GREY, hz / 1e6, RESET))
            self.beep('ira' if colour == CYAN else 'ok')

    def _ham(self, kind, f, quiet):
        """HFR t hz norad name snr bytes callsign candidates / HSIG t hz - snr seconds (the frames' contents are never shown)"""
        try:
            t, hz = float(f[0]), float(f[1])
        except (IndexError, ValueError):
            return
        self._seen(t)
        when = datetime.datetime.fromtimestamp(t).strftime('%H:%M:%S')
        if kind == 'HSIG':
            snr = float(f[3]) if len(f) > 3 else 0.0
            return self._damaged(when, snr, 'dB', self._bar(snr, 'ham'), 'a signal (%ss) - no frame decoded  %s%.4f MHz'
                                 % (f[4] if len(f) > 4 else '?', GREY, hz / 1e6), quiet)
        try:
            name, snr, size, call = f[3].replace('_', ' '), float(f[4]), int(f[5]), f[6]
        except (IndexError, ValueError):
            return
        self.decoded += 1
        self.kinds[kind] += 1
        if name != '?':
            self.sats.add(name)
        who = name if name != '?' else 'unidentified (a shared frequency)'
        if not quiet:
            print('%s[%s]  DECODED  %5.1f dB    %-24s frame from %s%s%s - callsign %s, %d bytes  %s%.4f MHz%s'
                  % (GREEN, when, snr, self._bar(snr, 'ham'), BOLD, who, RESET + GREEN, call, size, GREY, hz / 1e6, RESET))
            self.beep('ok')

    def _meteor(self, f, quiet):
        """MSIG t snr_db every 2 s: the Meteor signal over the noise beside it (shown every 10 s; pictures after Stop)"""
        try:
            t, snr = float(f[0]), float(f[1])
        except (IndexError, ValueError):
            return
        self._seen(t)
        self.kinds['signal'] += 1
        best = getattr(self, 'meteor_best', None)
        self.meteor_best = snr if best is None else max(best, snr)
        if quiet or t - getattr(self, 'meteor_printed', 0) < 10:
            return
        self.meteor_printed = t
        colour = CYAN if snr >= 6 else GREEN if snr >= 2 else GREY
        what = ('strong - good pictures likely' if snr >= 6 else 'there - pictures possible' if snr >= 2
                else 'nothing yet (before the pass, or too weak)')
        print('%s[%s]  signal   %+5.1f dB    %-24s Meteor LRPT 137.9 MHz: %s%s'
              % (colour, datetime.datetime.fromtimestamp(t).strftime('%H:%M:%S'), snr,
                 self._bar(snr, 'meteor'), what, RESET))

    def _gnss(self, kind, f, quiet):
        """GCH t sat sig cn0 doppler lock [sync ...] every second per tracked satellite / GEPH t sat fields (its
        broadcast orbit) / GPOS t lat lon height nsat (a position fix of this receiver)"""
        try:
            t = float(f[0])
        except (IndexError, ValueError):
            return
        self._seen(t)
        when = datetime.datetime.fromtimestamp(t).strftime('%H:%M:%S')
        if kind == 'GCH' and len(f) >= 6:
            sat, sig, cn0, fd, lock = f[1], f[2], float(f[3]), float(f[4]), float(f[5])
            name = catalog.gnss_name(sat)
            sync = f[6] if len(f) > 6 else ''
            last, printed = self.gnss_last.get(sat, (None, 0))
            new = last is None
            self.gnss_last[sat] = (t, printed)
            if new:
                self.decoded += 1
                self.kinds['tracked'] += 1
                self.sats.add(name)
            if quiet or not (new or t - printed >= GNSS_EVERY_S):
                return
            self.gnss_last[sat] = (t, t)
            nav = ('reading its navigation message' if 'F' in sync else 'finding the bits of its message' if 'B' in sync
                   else 'locking on') if sync else ''
            print('%s[%s]  %s %5.1f dB-Hz %-24s %-12s %-5s Doppler %+7.0f Hz  locked %4.0f s  %s%s'
                  % (BOLD + GREEN if new else GREEN, when, 'ACQUIRED' if new else 'tracking', cn0, self._bar(cn0, 'gnss'),
                     name, sig, fd, lock, GREY + nav if nav else '', RESET))
            if new:
                self.beep('ok')
        elif kind == 'GEPH' and len(f) >= 3:
            sat, fields = f[1], f[2]
            name = catalog.gnss_name(sat)
            what = ('its position (WAAS message type 9)' if fields.startswith('SBAS') else
                    'its broadcast orbit (GLONASS ephemeris)' if fields.startswith('GLO') else
                    'its broadcast orbit (ephemeris)')
            first = sat not in self.gnss_orbits
            self.gnss_orbits.add(sat)
            self.kinds['orbit'] += 1
            if not quiet:
                print('%s[%s]  ORBIT    %s%-12s%s %s%s%s' % (MAGENTA, when, BOLD, name, RESET + MAGENTA, what,
                                                           '' if first else ' - updated', RESET))
                self.beep('ok')
        elif kind == 'GPOS' and len(f) >= 5:
            lat, lon, h, n = float(f[1]), float(f[2]), float(f[3]), int(float(f[4]))
            self.fixes.append((t, 'this receiver', lat, lon, '%.0f m height' % h))
            self.kinds['position fix'] += 1
            home = ''
            if catalog.HOME:
                dn = (lat - catalog.HOME[0]) * 111.2e3
                de = (lon - catalog.HOME[1]) * 111.2e3 * math.cos(math.radians(lat))
                home = ' - %.0f m from your set location' % math.hypot(dn, de)
            if not quiet:
                print('%s[%s]  POSITION %sfix from %d satellites: %+.5f, %+.5f, %.0f m%s%s' % (
                    CYAN, when, BOLD, n, lat, lon, h, home, RESET))
                self.beep('ira')

    def _gnss_lost(self):
        if not self.gnss_last or self.last_t is None:
            return
        for sat, (t, _) in list(self.gnss_last.items()):
            if self.last_t - t > GNSS_LOST_S:
                print('%s[%s]  lost     %s - no signal for %d s%s' % (
                    GREY, datetime.datetime.fromtimestamp(self.last_t).strftime('%H:%M:%S'), catalog.gnss_name(sat),
                    GNSS_LOST_S, RESET))
                del self.gnss_last[sat]

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
            if m:       # Iridium's decoder totals
                if time.time() - self.last_status >= 10:
                    self.last_status = time.time()
                    print('%s[decoder] %s bursts/s in  ·  %s%% decodable on average  ·  %s bursts so far%s'
                          % (YELLOW, m.group(2), m.group(3), m.group(4), RESET))
                continue
            m = re.match(r'^\d{10} \| (.*)', line)          # the other receivers' status (every ~10 s)
            if m:
                print('%s[receiver] %s%s' % (YELLOW, m.group(1).strip(), RESET))
            elif line.strip() and not re.search(r'DeprecationWarning|setDaemon|^\s|^gr-osmosdr|^built-in source', line):
                print('%s[receiver] %s%s' % (YELLOW, line.strip(), RESET))

    # ---- receiver run from here
    def _rx_running(self, on):
        if not on and self.cap and self.cap == self.rx.capname:
            path = os.path.join(paths.CAPTURES, self.cap + '.live')
            self._read(path)
            self.summary()
            self.cap = None
            self.waiting_shown = False

    def _rx_status(self, text):
        if not text.startswith('Receiving'):
            print('%s[receiver] %s%s' % (YELLOW, text, RESET))

    def start_here(self):
        if catalog.HOME is None:
            print('%sSet your location in the tracker first (click it on the map).%s' % (RED, RESET))
            return
        print('%sStarting the receiver here: %s%s' % (BOLD, band_text(self.band), RESET))
        self.rx.start(self.band['key'])

    def keys(self):
        while msvcrt.kbhit():
            k = msvcrt.getwch().lower()
            if k == 'r':
                if self.rx.state == 'running':
                    self.rx.stop()
                elif self.rx.busy:
                    print('%s[receiver] still finishing the last capture...%s' % (YELLOW, RESET))
                elif rx.active_capture():
                    other = rx.active_capture()[0]
                    print('%sAnother receiver is running (%s, %s) - stop it in the tracker first (only one program '
                          'can use the SDR).%s' % (RED, systems.of_capture(other)['label'], other, RESET))
                else:
                    self.start_here()
            elif k in '123456789' and int(k) <= len(BANDS):
                self.choose_band(int(k) - 1)
            elif k == 'b':
                self.beep.on = not self.beep.on
                print('%sbeeps %s%s' % (GREY, 'on' if self.beep.on else 'off', RESET))
            elif k == 'd':
                self.show_damaged = not self.show_damaged
                print('%sdamaged / undecoded %s%s' % (GREY, 'shown' if self.show_damaged else 'hidden', RESET))
            elif k == 'q':
                self.quit()

    def summary(self):
        mins = (self.last_t - self.first_t) / 60 if self.first_t else 0
        gnss = self.cap_band is not None and self.cap_band.get('system') == 'gnss'
        if self.cap_band is not None and self.cap_band.get('system') == 'meteor':
            best = getattr(self, 'meteor_best', None)
            print('%s--- %s: %.0f min recorded, best signal %s - the pictures are made after Stop (Images section '
                  'in the tracker)%s' % (BOLD, self.cap, mins, '%+.1f dB' % best if best is not None else '-', RESET))
            return
        if gnss:
            print('%s--- %s: %d satellites tracked, %d broadcast orbits, %d position fixes in %.0f min%s'
                  % (BOLD, self.cap, len(self.sats), len(self.gnss_orbits), len(self.fixes), mins, RESET))
        else:
            print('%s--- %s: %d decoded, %d heard but not decoded in %.0f min%s'
                  % (BOLD, self.cap, self.decoded, self.heard, mins, RESET))
            if self.kinds:
                print('    by type: ' + ', '.join('%s %d' % kv for kv in self.kinds.most_common()))
        if self.sats:
            print('    satellites: ' + ', '.join(sorted(self.sats, key=lambda n: (re.sub(r'[\d ]', '', n),
                                                                                 int(re.sub(r'\D', '', n) or 0)))))
        for t, name, lat, lon, alt in self.fixes[-10:]:
            print('    position  %s  %-14s %+.4f, %+.4f  %s' % (datetime.datetime.fromtimestamp(t).strftime('%H:%M:%S'),
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
        print('(only the frames that matter for the satellites\' orbits are kept for this capture)')
        lines = storage.read_reduced(path)[1]
    else:
        lines = open(path, errors='replace')
    app = QtCore.QCoreApplication(sys.argv)  # noqa: F841 (the receiver object needs one)
    c = Console(argparse.Namespace(beep=False, no_damaged=True, band=None))
    c.timer.stop()
    c.cap = name
    c.cap_band = systems.of_capture(name)
    print('%s=== %s - %s ===%s' % (BOLD, name, c.cap_band['label'], RESET))
    for line in lines:
        c.frame(line.strip())
    c.summary()


def main():
    ap = argparse.ArgumentParser(description='%s - detailed live log of the receiver, on every band' % TITLE)
    ap.add_argument('--receive', action='store_true', help='run the receiver from here (not from the tracker)')
    ap.add_argument('--band', choices=[b['key'] for b in BANDS],
                    help="the band to receive (default: the tracker's last one); keys 1-7 change it while running")
    ap.add_argument('--capture', help='print the frames of a finished capture')
    ap.add_argument('--beep', action='store_true', help='start with beeps on (short = not decoded, long = decoded, '
                                                        'extra long = a position)')
    ap.add_argument('--no-damaged', action='store_true', help="don't list damaged / undecoded signals")
    args = ap.parse_args()
    enable_colours()
    set_title(TITLE)
    if args.capture:
        print_capture(args.capture)
        return
    app = QtCore.QCoreApplication(sys.argv)
    print('%sSATELLITE CONSOLE%s  -  R receive here  ·  1-7 band  ·  B beeps  ·  D damaged  ·  Q quit' % (BOLD, RESET))
    print('%sGreen = decoded / tracked, cyan = a position, magenta = a GNSS orbit, grey = heard but not decoded.%s'
          % (GREY, RESET))
    c = Console(args)
    c.band_menu()
    signal.signal(signal.SIGINT, lambda *_: c.quit())     # Ctrl+C stops cleanly too
    if args.receive:
        QtCore.QTimer.singleShot(300, lambda: c.keys() or c.start_here())
    app.exec_()


if __name__ == '__main__':
    main()
