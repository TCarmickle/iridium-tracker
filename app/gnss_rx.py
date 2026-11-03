"""GNSS receiver (GPS L1 C/A and Galileo E1B at 1575.42 MHz): records the slice in chunks and lets
PocketSDR's pocket_trk track the satellites in each one. receiver.py starts it and saves its lines to the
capture's .live file.

  python gnss_rx.py --device ARGS --rate R --center F [--gain DB | --auto-gain] [--ppm X] [--no-dc]
                    [--start-file PATH] [--spectrum-port N] [--audio-port N]

GNSS signals are under the noise: they can't be seen in the spectrum, only found by correlating with each
satellite's code. pocket_trk (T. Takasu, BSD-2-Clause, in app/pocketsdr) does that, tracks the satellites,
decodes the navigation messages - including each satellite's own orbit (its ephemeris) - and computes a
position fix. It works on files, so the samples are written in CHUNK_S chunks (8-bit IQ) and each finished
chunk is handed to it while the next is recorded; a chunk is long enough to decode the ephemeris (~30 s).
Lines (from its log):
  GCH: t sat sig cn0 doppler_hz lock_s     a tracked satellite, once a second (sat: G05, E11, C23 ...)
  GEPH: t sat <pocket_trk $EPH fields>     a satellite's decoded broadcast ephemeris (when it changes)
  GPOS: t lat lon height_m nsat            a position fix from the satellites
"""
import argparse
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


# ---------------------------------------------------------------- recording chunks
class ChunkWriter(gr.sync_block):
    """writes the samples as 8-bit IQ files of CHUNK_S seconds; each finished one goes to the tracker"""

    def __init__(self, rate, folder, done):
        gr.sync_block.__init__(self, 'gnss_chunks', in_sig=[np.complex64], out_sig=None)
        self.rate, self.folder, self.done = rate, folder, done
        self.f = None
        self.n = 0
        self.t0 = None
        self.rms = None

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
        self.f.write(iq.tobytes())
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


class Tracker(threading.Thread):
    def __init__(self, chunks, rate):
        super().__init__(daemon=True)
        self.chunks, self.rate = chunks, rate
        self.eph = {}           # satellite -> its last ephemeris fields (sent again only when they change)

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
        for sat, sig, fields in ephs:      # in the field order of a $EPH line, so gnss.parse_eph reads it
            text = ','.join([sig] + [str(fields[k]) for k in gnss.FIELDS])
            if self.eph.get(sat) != text:
                self.eph[sat] = text
                emit('GEPH: %.3f %s %s' % (t0 + self.chunk_s_done(), sat, text))
        last_ch = {}            # satellite -> receiver time of the last GCH line (one a second is plenty)
        seen, fixes = set(), 0
        for line in lines:
            f = line.strip().split(',')
            if not f or not f[0].startswith('$'):
                continue
            try:
                if f[0] == '$CH' and len(f) > 10:
                    rt, sat, sig, lock, cn0, fd = float(f[1]), f[4], f[5], float(f[7]), float(f[8]), float(f[10])
                    if lock < 1.0 or rt - last_ch.get(sat, -9) < 1.0:
                        continue
                    last_ch[sat] = rt
                    seen.add(sat)
                    emit('GCH: %.3f %s %s %.1f %.1f %.1f' % (t0 + rt, sat, sig, cn0, fd, lock))
                elif f[0] == '$EPH' and len(f) > 4:
                    sat, fields = f[2], ','.join(f[3:])
                    if self.eph.get(sat) != fields:
                        self.eph[sat] = fields
                        emit('GEPH: %.3f %s %s' % (t0 + float(f[1]), sat, fields))
                elif f[0] == '$POS' and len(f) > 11:
                    t = gpst_to_unix([float(v) for v in f[2:8]])
                    emit('GPOS: %.3f %s %s %s %s' % (t, f[8], f[9], f[10], f[12]))
                    fixes += 1
            except ValueError:
                continue
        log('%d | GNSS chunk: %d satellites tracked (%s), %d position fixes, %d ephemerides known, %.0f s to process'
            % (time.time(), len(seen), ' '.join(sorted(seen)) or 'none', fixes, len(self.eph), time.time() - started_at))


# ---------------------------------------------------------------- the flowgraph
class Receiver(gr.top_block):
    def __init__(self, chunks, folder):
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
        self.writer = ChunkWriter(rate, folder, chunks)
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
    folder = opts.keep_chunks or tempfile.mkdtemp(prefix='gnss_rx_')
    chunks = queue.Queue()
    Tracker(chunks, opts.rate).start()
    tb = Receiver(chunks, folder)
    tb.start()
    log('Receiving GNSS at %.4f MHz, %.4f MS/s - satellites appear after each %.0f s chunk is processed'
        % (opts.center / 1e6, opts.rate / 1e6, opts.chunk_s))
    try:
        while sdr_taps.parent_alive(opts.parent_pid):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    tb.stop()
    tb.wait()


if __name__ == '__main__':
    main()
