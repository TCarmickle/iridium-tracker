"""GNSS receiver (GPS L1 C/A and Galileo E1B at 1575.42 MHz): streams the slice into PocketSDR's
pocket_trk, which tracks the satellites live. receiver.py starts it and saves its lines to the capture's
.live file.

  python gnss_rx.py --device ARGS --rate R --center F [--gain DB | --auto-gain] [--ppm X] [--no-dc]
                    [--start-file PATH] [--spectrum-port N] [--audio-port N] [--mode live|chunks]

GNSS signals are under the noise: they can't be seen in the spectrum, only found by correlating with each
satellite's code. pocket_trk (T. Takasu, BSD-2-Clause, in app/pocketsdr) does that, tracks the satellites,
decodes the navigation messages - including each satellite's own orbit (its ephemeris) - and computes a
position fix.
  live    (default) the samples (8-bit IQ) go through a Windows named pipe into one pocket_trk that runs
          the whole time: satellites stay locked, and its log and orbits are read as they are written, so
          the tracker hears about them within a second or two.
  chunks  the samples are written in CHUNK_S files and pocket_trk runs on each one afterwards (each chunk
          starts from scratch; results every CHUNK_S seconds). Kept as a fallback.
(pocket_trk can also drive an RTL-SDR itself through SoapySDR, but the RTL-SDR's IQ is the mirror of what
pocket_trk expects and it has no way to swap it, so the samples come from here.)
Lines (from its log):
  GCH: t sat sig cn0 doppler_hz lock_s     a tracked satellite, once a second (sat: G05, E11, C23 ...)
  GEPH: t sat <pocket_trk $EPH fields>     a satellite's decoded broadcast ephemeris (when it changes)
  GPOS: t lat lon height_m nsat            a position fix from the satellites
"""
import argparse
import ctypes
import datetime
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np
import osmosdr
from gnuradio import gr

import gnss
import rtcm3
import sdr_taps

HERE = os.path.dirname(os.path.abspath(__file__))
POCKET_TRK = os.path.join(HERE, 'pocketsdr', 'pocket_trk.exe')
# A satellite's orbit (ephemeris) arrives in pieces over a 30 s frame; after locking on and syncing to the frames
# it takes up to ~60 s of unbroken tracking, and each chunk starts from scratch - so 60 s chunks cut the locks
# off just short of it (seen on live data). 3 minutes leaves room for it (the chunk files are ~740 MB, temporary).
CHUNK_S = 180.0
# GPS and Galileo. BeiDou's B1C signal is at the same frequency, but pocket_trk decodes no BeiDou orbit from it,
# and searching its 45 codes took over a third of the processing time - so it is left out.
SIGNALS = ['-sig', 'L1CA', '-prn', '1-32', '-sig', 'E1B', '-prn', '1-36']
GPS_UTC_S = 18                  # GPS time runs this far ahead of UTC (leap seconds)
GPS_EPOCH = datetime.datetime(1980, 1, 6, tzinfo=datetime.timezone.utc)

ap = argparse.ArgumentParser()
ap.add_argument('--device', default='rtl=0')
ap.add_argument('--rate', type=float, default=2048000)
ap.add_argument('--center', type=float, default=1575.42e6)
ap.add_argument('--gain', type=float, default=40.0)
ap.add_argument('--auto-gain', action='store_true')
ap.add_argument('--ppm', type=float, default=0.0)
ap.add_argument('--no-dc', action='store_true')
ap.add_argument('--start-file', default='')
ap.add_argument('--spectrum-port', type=int, default=0)
ap.add_argument('--audio-port', type=int, default=0)
ap.add_argument('--parent-pid', type=int, default=0)   # stop when the tracker that started us is gone
ap.add_argument('--chunk-s', type=float, default=CHUNK_S)
ap.add_argument('--keep-chunks', default='')    # for tests: keep the recorded chunks in this folder
ap.add_argument('--mode', choices=('live', 'chunks'), default='live')
opts = ap.parse_args()


def log(text):
    print(text, file=sys.stderr, flush=True)


def emit(line):
    sys.stdout.write(line + '\n')
    sys.stdout.flush()


def _try(what, fn, *a):
    try:
        fn(*a)
        log(what)
    except Exception as e:      # not every device supports every setting
        log('%s: not supported by this device (%s)' % (what.split(':')[0], e))


# ---------------------------------------------------------------- samples -> pocket_trk's 8-bit IQ
class Quantizer:
    """complex samples -> interleaved 8-bit IQ the way pocket_trk wants them"""

    def __init__(self):
        self.rms = None

    def __call__(self, x):
        # 8 bits are plenty for signals under the noise: scale the noise to ~+-24 so it is neither clipped
        # nor coarse (the scale follows the noise level slowly)
        r = float(np.sqrt(np.mean(np.abs(x) ** 2) / 2)) + 1e-12
        self.rms = r if self.rms is None else 0.98 * self.rms + 0.02 * r
        k = 24.0 / self.rms
        iq = np.empty(2 * len(x), np.int8)
        iq[0::2] = np.clip(np.round(x.real * k), -127, 127)
        # Q negated: pocket_trk's IQ convention is the mirror of gr-osmosdr's, and without this every Doppler
        # came out with the wrong sign (checked against the satellites' real geometry on live GPS data)
        iq[1::2] = np.clip(np.round(-x.imag * k), -127, 127)
        return iq.tobytes()


# ---------------------------------------------------------------- recording chunks
class ChunkWriter(gr.sync_block):
    """writes the samples as 8-bit IQ files of CHUNK_S seconds; each finished one goes to the tracker"""

    def __init__(self, rate, folder, done):
        gr.sync_block.__init__(self, 'gnss_chunks', in_sig=[np.complex64], out_sig=None)
        self.rate, self.folder, self.done = rate, folder, done
        self.f = None
        self.n = 0
        self.t0 = None
        self.quantize = Quantizer()

    def _open(self, t):
        self.path = os.path.join(self.folder, 'chunk-%d.bin' % int(t * 1000))
        self.f = open(self.path, 'wb')
        self.t0, self.n = t, 0

    def work(self, input_items, output_items):
        x = input_items[0]
        now = time.time() - len(x) / self.rate
        if self.f is None:
            self._open(now)
            started(now)
        self.f.write(self.quantize(x))
        self.n += len(x)
        if self.n >= self.rate * opts.chunk_s:
            self.f.close()
            self.done.put((self.path, self.t0, self.n / self.rate))
            self._open(self.t0 + self.n / self.rate)
        return len(x)


_started = threading.Event()


def started(t):
    if _started.is_set():
        return
    _started.set()
    if opts.start_file:
        with open(opts.start_file, 'w') as f:
            f.write(datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] + 'Z\n')


# ---------------------------------------------------------------- tracking a chunk with pocket_trk
def gpst_to_unix(ep):
    """a pocket_trk calendar epoch (GPS time: y, m, d, h, min, s) -> unix time (UTC)"""
    y, mo, d, h, mi, s = ep
    t = datetime.datetime(int(y), int(mo), int(d), int(h), int(mi), tzinfo=datetime.timezone.utc).timestamp() + s
    return t - GPS_UTC_S


def track(path, t0, rate):
    """run pocket_trk on one chunk; returns (its receiver log lines, the ephemerides it decoded).
    The ephemerides come from its RTCM output: it doesn't write them to its log (its $EPH lines are masked off)."""
    logf = path[:-4] + '.log'
    rtcmf = path[:-4] + '.rtcm'
    cmd = [POCKET_TRK] + SIGNALS + ['-fmt', 'INT8X2', '-f', '%.6f' % (rate / 1e6),
                                    '-log', logf, '-rtcm', rtcmf, path]
    try:
        subprocess.run(cmd, cwd=os.path.dirname(POCKET_TRK), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=max(120.0, opts.chunk_s * 4), creationflags=0x08000000)
    except (OSError, subprocess.TimeoutExpired) as e:
        log('GNSS: pocket_trk could not process %s (%s)' % (os.path.basename(path), e))
    lines, ephs = [], []
    try:
        with open(logf, errors='replace') as f:
            lines = f.read().splitlines()
    except OSError:
        pass
    try:
        with open(rtcmf, 'rb') as f:
            ephs = rtcm3.ephemerides(f.read(), now=t0)
    except OSError:
        pass
    for f in (logf, rtcmf):
        try:
            os.remove(f)
        except OSError:
            pass
    return lines, ephs


class Lines:
    """pocket_trk's log lines and decoded orbits -> this receiver's GCH / GEPH / GPOS lines"""

    def __init__(self):
        self.eph = {}           # satellite -> its last ephemeris fields (sent again only when they change)
        self.last_ch = {}       # satellite -> receiver time of its last GCH line (one a second is plenty)
        self.seen = set()
        self.fixes = 0

    def orbits(self, ephs, t):
        for sat, sig, fields in ephs:      # in the field order of a $EPH line, so gnss.parse_eph reads it
            text = ','.join([sig] + [str(fields[k]) for k in gnss.FIELDS])
            same = ','.join([sig] + [str(fields[k]) for k in gnss.FIELDS if k != 'ttr_gpst'])   # (when received)
            if self.eph.get(sat) != same:
                self.eph[sat] = same
                emit('GEPH: %.3f %s %s' % (t, sat, text))

    def line(self, line, when):
        """one log line; when(receiver time) -> unix time"""
        f = line.strip().split(',')
        if not f or not f[0].startswith('$'):
            return
        try:
            if f[0] == '$CH' and len(f) > 10:
                rt, sat, sig, lock, cn0, fd = float(f[1]), f[4], f[5], float(f[7]), float(f[8]), float(f[10])
                if lock < 1.0 or rt - self.last_ch.get(sat, -9) < 1.0:
                    return
                self.last_ch[sat] = rt
                self.seen.add(sat)
                emit('GCH: %.3f %s %s %.1f %.1f %.1f' % (when(rt), sat, sig, cn0, fd, lock))
            elif f[0] == '$EPH' and len(f) > 4:
                sat, fields = f[2], ','.join(f[3:])
                if self.eph.get(sat) != fields:
                    self.eph[sat] = fields
                    emit('GEPH: %.3f %s %s' % (when(float(f[1])), sat, fields))
            elif f[0] == '$POS' and len(f) > 11:
                t = gpst_to_unix([float(v) for v in f[2:8]])
                emit('GPOS: %.3f %s %s %s %s' % (t, f[8], f[9], f[10], f[12]))
                self.fixes += 1
        except ValueError:
            pass


class Tracker(threading.Thread):
    def __init__(self, chunks, rate):
        super().__init__(daemon=True)
        self.chunks, self.rate = chunks, rate
        self.out = Lines()

    def chunk_s_done(self):
        return opts.chunk_s

    def run(self):
        while True:
            path, t0, dur = self.chunks.get()
            try:
                self.handle(path, t0)
            except Exception as e:
                log('GNSS tracker: %r' % e)
            finally:
                if not opts.keep_chunks:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    def handle(self, path, t0):
        started_at = time.time()
        lines, ephs = track(path, t0, self.rate)
        out = self.out
        out.orbits(ephs, t0 + self.chunk_s_done())
        out.last_ch, out.seen, out.fixes = {}, set(), 0
        for line in lines:
            out.line(line, lambda rt: t0 + rt)
        log('%d | GNSS chunk: %d satellites tracked (%s), %d position fixes, %d ephemerides known, %.0f s to process'
            % (time.time(), len(out.seen), ' '.join(sorted(out.seen)) or 'none', out.fixes, len(out.eph),
               time.time() - started_at))


# ---------------------------------------------------------------- live: one pocket_trk fed through a pipe
class Pipe:
    """a Windows named pipe that pocket_trk opens as its input file"""

    def __init__(self, name):
        from ctypes import wintypes
        self.wt = wintypes
        self.k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self.k32.CreateNamedPipeW.restype = wintypes.HANDLE
        self.path = r'\\.\pipe\%s' % name
        # outbound, byte mode, one instance, 4 MB buffers (~0.5 s of samples)
        self.h = self.k32.CreateNamedPipeW(self.path, 0x00000002, 0, 1, 4 << 20, 4 << 20, 0, None)
        if self.h == wintypes.HANDLE(-1).value:
            raise OSError('CreateNamedPipe failed (%d)' % ctypes.get_last_error())

    def connect(self):
        """wait for pocket_trk to open it"""
        return bool(self.k32.ConnectNamedPipe(self.h, None)) or ctypes.get_last_error() == 535   # already connected

    def write(self, data):
        n = self.wt.DWORD()
        return bool(self.k32.WriteFile(self.h, data, len(data), ctypes.byref(n), None))

    def close(self):
        self.k32.CloseHandle(self.h)


class PipeFeeder(gr.sync_block):
    """hands the samples (as 8-bit IQ) to the live tracker; never blocks the radio"""

    def __init__(self, rate, live):
        gr.sync_block.__init__(self, 'gnss_pipe', in_sig=[np.complex64], out_sig=None)
        self.rate, self.live = rate, live
        self.quantize = Quantizer()

    def work(self, input_items, output_items):
        x = input_items[0]
        if not _started.is_set():
            started(time.time() - len(x) / self.rate)
        self.live.feed(self.quantize(x), len(x))
        return len(x)


class LiveTracker(threading.Thread):
    """runs pocket_trk on the pipe for the whole session (restarting it if it stops), feeds it, and reads its
    log and decoded orbits as they grow"""
    QUEUE_S = 4.0       # samples waiting for pocket_trk; beyond this they are dropped (it is falling behind)

    def __init__(self, rate):
        super().__init__(daemon=True)
        self.rate = rate
        self.q = queue.Queue(maxsize=int(self.QUEUE_S * 20))     # ~50 ms blocks
        self.out = Lines()
        self.folder = tempfile.mkdtemp(prefix='gnss_live_')
        self.dropped = 0            # samples dropped because pocket_trk fell behind
        self.runs = 0

    def feed(self, data, n):
        try:
            self.q.put_nowait((data, n))
        except queue.Full:
            self.dropped += n

    def run(self):
        while True:
            try:
                self.session()
            except Exception as e:
                log('GNSS live tracker: %r' % e)
            time.sleep(2)           # pocket_trk stopped: start it again (it picks satellites up in a minute)

    def session(self):
        self.runs += 1
        pipe = Pipe('gnss_rx_%d_%d' % (os.getpid(), self.runs))
        logf = os.path.join(self.folder, 'trk-%d.log' % self.runs)
        rtcmf = os.path.join(self.folder, 'trk-%d.rtcm' % self.runs)
        cmd = [POCKET_TRK] + SIGNALS + ['-fmt', 'INT8X2', '-f', '%.6f' % (self.rate / 1e6),
                                        '-log', logf, '-rtcm', rtcmf, pipe.path]
        proc = subprocess.Popen(cmd, cwd=os.path.dirname(POCKET_TRK), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=0x08000000)
        if not pipe.connect():
            pipe.close()
            proc.kill()
            raise OSError('pocket_trk did not open the pipe')
        while not self.q.empty():   # start from fresh samples
            self.q.get_nowait()
        t0 = time.time()            # unix time of receiver time 0
        written = 0                 # samples given to pocket_trk
        gaps = []                   # (receiver time, seconds dropped there): its clock skips over them
        log('GNSS: live tracking started (pocket_trk run %d)' % self.runs)
        reader = threading.Thread(target=self.read, args=(proc, logf, rtcmf, lambda rt: t0 + rt + sum(
            s for at, s in gaps if at <= rt)), daemon=True)
        reader.start()
        dropped = self.dropped
        while proc.poll() is None:
            try:
                data, n = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            if self.dropped != dropped:     # samples were dropped before this block
                gaps.append((written / self.rate, (self.dropped - dropped) / self.rate))
                dropped = self.dropped
            if not pipe.write(data):
                break
            written += n
        pipe.close()
        if proc.poll() is None:
            proc.kill()
        reader.join(timeout=5)
        log('GNSS: pocket_trk run %d stopped after %.0f s' % (self.runs, written / self.rate))

    def read(self, proc, logf, rtcmf, when):
        """follow pocket_trk's log and RTCM output while it runs"""
        pos, rest, rpos, rrest, last_note = 0, '', 0, b'', time.time()
        while True:
            alive = proc.poll() is None
            try:
                with open(logf, errors='replace') as f:
                    f.seek(pos)
                    text = f.read()
                    pos = f.tell()
            except OSError:
                text = ''
            lines = (rest + text).split('\n')
            rest = lines.pop()              # a line still being written
            for line in lines:
                self.out.line(line, when)
            try:
                with open(rtcmf, 'rb') as f:        # the orbits it decoded since the last look
                    f.seek(rpos)
                    new = f.read()
                    rpos = f.tell()
                payloads, rrest = rtcm3.split(rrest + new)
                now = time.time()
                self.out.orbits([e for e in (rtcm3.decode(pl, now) for pl in payloads) if e], now)
            except OSError:
                pass
            if time.time() - last_note > 60:
                last_note = time.time()
                locked = sorted(s for s, rt in self.out.last_ch.items() if when(rt) > time.time() - 5)
                log('%d | GNSS live: %d satellites locked now (%s), %d broadcast orbits, %d position fixes, '
                    '%.0f s of samples dropped' % (time.time(), len(locked), ' '.join(locked) or 'none',
                                                  len(self.out.eph), self.out.fixes, self.dropped / self.rate))
            if not alive:
                return
            time.sleep(0.5)


# ---------------------------------------------------------------- the flowgraph
class Receiver(gr.top_block):
    def __init__(self, sink):
        gr.top_block.__init__(self, 'gnss_rx')
        rate, center = opts.rate, opts.center
        src = osmosdr.source(args=opts.device)
        src.set_sample_rate(rate)
        src.set_center_freq(center, 0)
        if opts.ppm:
            _try('Frequency correction: %+.1f ppm' % opts.ppm, src.set_freq_corr, opts.ppm, 0)
        if opts.auto_gain:
            _try('Gain: automatic', src.set_gain_mode, True, 0)
        else:
            _try('Gain: manual', src.set_gain_mode, False, 0)
            _try('Gain: %.1f dB' % opts.gain, src.set_gain, opts.gain, 0)
        _try('DC correction: %s' % ('off' if opts.no_dc else 'on'), src.set_dc_offset_mode, 0 if opts.no_dc else 2, 0)
        self.writer = sink
        self.connect(src, self.writer)
        self.taps = []
        if opts.spectrum_port:
            self.taps.append(sdr_taps.add_spectrum(self, src, opts.spectrum_port, center, rate))
        if opts.audio_port:
            self.taps.append(sdr_taps.add_audio(self, src, opts.audio_port, rate, channel_hz=36e3, demod='am'))


def main():
    if not os.path.exists(POCKET_TRK):
        log('GNSS: %s is missing - see README (PocketSDR)' % POCKET_TRK)
        sys.exit(2)
    if opts.mode == 'live':
        live = LiveTracker(opts.rate)
        live.start()
        tb = Receiver(PipeFeeder(opts.rate, live))
        how = 'tracking live - satellites appear as they lock (usually within a minute or two)'
    else:
        folder = opts.keep_chunks or tempfile.mkdtemp(prefix='gnss_rx_')
        chunks = queue.Queue()
        Tracker(chunks, opts.rate).start()
        tb = Receiver(ChunkWriter(opts.rate, folder, chunks))
        how = 'satellites appear after each %.0f s chunk is processed' % opts.chunk_s
    tb.start()
    log('Receiving GNSS at %.4f MHz, %.4f MS/s - %s' % (opts.center / 1e6, opts.rate / 1e6, how))
    try:
        while sdr_taps.parent_alive(opts.parent_pid):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    tb.stop()
    tb.wait()


if __name__ == '__main__':
    main()
