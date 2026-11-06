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
  GCH: t sat sig cn0 doppler_hz lock_s [sync nav err lol]
                                           a tracked satellite, once a second (sat: G05, E11, C23 ...);
                                           sync = its data message's symbol / bit / frame sync and polarity
                                           (e.g. 'SBF-'), then navigation frames decoded, frame errors and
                                           times lock was lost (pocket_trk's counters)
  GEPH: t sat <pocket_trk $EPH fields>     a satellite's decoded broadcast ephemeris (when it changes);
                                           WAAS (sat S31 = PRN 131): 'SBAS,t0,x,y,z,vx,vy,vz,ax,ay,az' - its own
                                           position from its message type 9 (m, m/s, m/s2)
  GPOS: t lat lon height_m nsat            a position fix from the satellites
  GTIME: t offset_s nsat spread_s          GPS time minus this PC's clock (live mode, every 10 s): the median
                                           over the last fixes, each fix's GPS time against the PC time its
                                           samples were taken at (from the SDR's sample count)
  GSPP: t lat lon height_m nsat hdop n_waas_iono n_fast n_longterm n_smoothed
                                           our own position fix (spp.py, GPS band only): pocket_trk's raw
                                           ranges, carrier-smoothed, with the WAAS corrections (how many satellites
                                           got the WAAS ionosphere / fast / long-term corrections; how many were
                                           smoothed 30 s or more); only fixes that pass its quality gate
  GNOISE: t power_dbfs rise_db clip_pct peak_db peak_mhz floor_db learning rms_pct
                                           the band's noise, once a second (NoiseMeter): its power (dB of full
                                           scale) and how far it is above the quiet baseline, the samples clipped,
                                           the strongest narrowband peak above the baseline's shape (dB, where),
                                           the median rise across the band; learning = 1 while the baseline is
                                           first learned (then the comparisons are 0)
  GSPEC: t f_lo_mhz f_hi_mhz v1,v2,...     with each GNOISE (once learned): the spectrum against the baseline
                                           in 128 bins from f_lo to f_hi, tenths of a dB (each bin the max of
                                           its group)
  GGAP: t gap_ms source                    samples went missing upstream (source usb: the SDR / USB path stalled;
                                           queue: pocket_trk fell behind) - that many zero samples were fed in
                                           their place so pocket_trk's sample clock stays on GPS time. Measured
                                           2026-11-06: the USB path loses 100-140 ms a few times a day; left as a
                                           cut, every satellite is lost and a loss over ~90 ms stops pocket_trk's
                                           positioning for good (every range 'out of range'); as zeros, nothing is lost
  GLOG: t EVENT [sat] [values]             pocket_trk's own events: FOUND sat cn0 dop, LOST sat cn0 pli, FALSELOCK
                                           sat, EPOCH dt_s (its clock stepped), SYNC COARSE|FINE, START run,
                                           RESTART reason (this receiver restarted it: clock-lost after a gap that
                                           got past the zero filling, no-fix, reader-died)
With --snap-trigger, the last 20 s of raw samples are kept in memory and saved, with the next 20 s, when the
tracker asks (IQRecorder: an Integrity event began).
"""
import argparse
import collections
import ctypes
import datetime
import json
import math
import os
import queue
import re
import shutil
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
import spp

HERE = os.path.dirname(os.path.abspath(__file__))
POCKET_TRK = os.path.join(HERE, 'pocketsdr', 'pocket_trk.exe')
# A satellite's orbit (ephemeris) arrives in pieces over a 30 s frame; after locking on and syncing to the frames
# it takes up to ~60 s of unbroken tracking, and each chunk starts from scratch - so 60 s chunks cut the locks
# off just short of it (seen on live data). 3 minutes leaves room for it (the chunk files are ~740 MB, temporary).
CHUNK_S = 180.0
# GPS and Galileo. BeiDou's B1C signal is at the same frequency, but pocket_trk decodes no BeiDou orbit from it,
# and searching its 45 codes took over a third of the processing time - so it is left out.
# WAAS: the SBAS satellites serving North America send a GPS-like signal on the same frequency (PRN 131, 133,
# 135; 138 was in service until 2022) - tracked alongside, and their own position decoded from their messages
WAAS_PRNS = '131,133,135,138'
SIGNALS = ['-sig', 'L1CA', '-prn', '1-32,' + WAAS_PRNS, '-sig', 'E1B', '-prn', '1-36']
NOISE = None                    # the band's noise meter (main)
POCKET_OPTS = []                # extra pocket_trk arguments (main: -opt FILE)
SNAP = None                     # the raw-sample recorder for Integrity events (main)
GPS_UTC_S = 18                  # GPS time runs this far ahead of UTC (leap seconds)
GPS_EPOCH = datetime.datetime(1980, 1, 6, tzinfo=datetime.timezone.utc)

ap = argparse.ArgumentParser()
ap.add_argument('--device', default='rtl=0')
ap.add_argument('--rate', type=float, default=2048000)
ap.add_argument('--center', type=float, default=1575.42e6)
ap.add_argument('--gain', type=float, default=40.0)
ap.add_argument('--auto-gain', action='store_true')
# E4000 tuner: its IF gain left at the driver's default leaves the samples at ~0.6% of the ADC's range (under
# one 8-bit step), losing weak signals in the rounding; 60 dB lifts them to ~10% (measured on GPS: 5 satellites
# at ~37 dB-Hz instead of 0-2 at ~33). Other tuners ignore it.
ap.add_argument('--if-gain', type=float, default=60.0)
ap.add_argument('--ppm', type=float, default=0.0)
ap.add_argument('--no-dc', action='store_true')
ap.add_argument('--start-file', default='')
ap.add_argument('--spectrum-port', type=int, default=0)
ap.add_argument('--audio-port', type=int, default=0)
ap.add_argument('--parent-pid', type=int, default=0)   # stop when the tracker that started us is gone
ap.add_argument('--chunk-s', type=float, default=CHUNK_S)
ap.add_argument('--keep-chunks', default='')    # for tests: keep the recorded chunks in this folder
ap.add_argument('--mode', choices=('live', 'chunks'), default='live')
ap.add_argument('--noise-baseline', default='')     # the quiet band's noise, learned and kept here (.npz)
ap.add_argument('--no-noise', action='store_true')  # no GNOISE lines
ap.add_argument('--snap-trigger', default='')       # raw samples around events: IQRecorder's trigger file
ap.add_argument('--spp-state', default='')          # our position solver's kept state (GPS ionosphere model, .json)
ap.add_argument('--no-spp', action='store_true')    # no GSPP lines (our own WAAS-corrected position)
ap.add_argument('--home', default='')               # lat,lon,alt_km: which satellites are up (warm start)
ap.add_argument('--narrow-search', action='store_true')     # search only the satellites up (from saved orbits)
ap.add_argument('--pocket-opts', default='')        # pocket_trk options file (-opt), e.g. its lock thresholds
ap.add_argument('--work-stem', default='')          # pocket_trk's log and RTCM as STEM.trkN.log / .rtcm (receiver.py:
#                                                     beside the capture, so they are kept and trimmed with it)
ap.add_argument('--constellation', choices=('gps', 'glonass', 'beidou'), default='gps')   # gps = GPS + Galileo + WAAS
opts = ap.parse_args()
if opts.constellation == 'glonass':     # every frequency channel (the SDR's slice holds the middle ones);
    # pocket_trk needs the tuning (-fo) to place each channel
    SIGNALS = ['-sig', 'G1CA', '-prn', ','.join(str(k) for k in range(-7, 7)), '-fo', '%.6f' % (opts.center / 1e6)]
elif opts.constellation == 'beidou':    # B1I at 1561.098 MHz: every BeiDou satellite (PRN 1-63)
    SIGNALS = ['-sig', 'B1I', '-prn', '1-63', '-fo', '%.6f' % (opts.center / 1e6)]


def log(text):
    print(text, file=sys.stderr, flush=True)


_emit_lock = threading.Lock()


def emit(line):
    with _emit_lock:        # the live tracker's reader and the sample thread both write lines
        sys.stdout.write(line + '\n')
        sys.stdout.flush()


_NUM = re.compile(r'-?\d+\.?\d*(?:[eE][-+]?\d+)?')
_RECORD = re.compile(r'(?<!^)(?=\$(?:CH|OBS|NAV|EPH|POS|TIME|SAT|LOG),)')    # pocket_trk writes from several threads:
# about 1 % of its lines are two records with the newline missing between them - split, so neither is lost


def _log_sat(sig, prn):
    """the satellite a pocket_trk $LOG line is about (its signal name and PRN) -> G05 / E11 / S31 / R-3 / C23"""
    if sig.startswith('L1') or sig.startswith('L2') or sig.startswith('L5'):    # GPS, with WAAS and QZSS PRNs
        if 120 <= prn <= 158:
            return 'S%02d' % (prn - 100)
        if prn > 192:
            return 'J%02d' % (prn - 192)
        return 'G%02d' % prn
    if sig.startswith('E'):
        return 'E%02d' % prn
    if sig.startswith('G1') or sig.startswith('G2'):        # GLONASS: by frequency channel until its slot is known
        return 'R%+d' % prn
    if sig.startswith('B'):
        return 'C%02d' % prn
    return '%s%d' % (sig[:1], prn)


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
        for watch in (NOISE, SNAP):
            if watch:
                try:
                    watch(x)
                except Exception as e:      # the watch must never stop the tracking
                    log('GNSS watch: %r' % e)
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


# ---------------------------------------------------------------- the band's noise (jamming watch)
class NoiseMeter:
    """once a second, a GNOISE line: the band's total power and its spectrum against a baseline learned when the
    band was quiet (saved in --noise-baseline, so the next session starts from it). GNSS signals are under the
    noise, so with the gain fixed the band's power is the noise itself: a jammer raises it (broadband), or shows as
    a peak above the baseline's own shape (a narrowband or swept carrier). Known local spurs are part of the
    baseline, so they don't count."""
    NFFT = 512
    LEARN_S = 30            # no baseline yet (or the gains changed): the first 30 s become it
    ADAPT = 0.002           # quiet seconds then nudge the baseline (~8 min time constant): it follows slow drifts
    SAVE_S = 300
    SPEC_BINS = 128

    def __init__(self, rate, center, path):
        self.rate, self.center, self.path = rate, center, path
        self.win = np.blackman(self.NFFT).astype(np.float32)
        self.win /= np.sqrt(np.mean(self.win ** 2))
        n = self.NFFT
        use = np.ones(n, bool)
        use[:n // 10] = use[-n // 10:] = False          # the filter's roll-off at the slice's edges
        use[n // 2 - 3:n // 2 + 4] = False              # the DC spike
        self.use = use
        self.freqs = center + (np.arange(n) - n // 2) * rate / n
        self.key = np.array([rate, center, opts.gain, opts.if_gain, float(opts.auto_gain)])
        self.base_spec = self.base_power = None
        self.learn = []
        self.on_disk = False        # the baseline is in its file (deleting the file = relearn now)
        self.rand = np.random.default_rng()
        try:
            z = np.load(path)
            if z['key'].shape == self.key.shape and np.allclose(z['key'], self.key):
                self.base_spec, self.base_power = z['spec'], float(z['power'])
                self.on_disk = True
        except Exception:       # no baseline yet, another band's or other gains
            pass
        self._reset()
        self.t_last = time.time()
        self.t_saved = time.time()

    def _reset(self):
        self.p_sum, self.p_n, self.clip, self.acc, self.nacc = 0.0, 0, 0, np.zeros(self.NFFT), 0

    def __call__(self, x):
        self.p_sum += float(np.sum(x.real * x.real + x.imag * x.imag))
        self.p_n += len(x)
        self.clip += int(np.count_nonzero((np.abs(x.real) > 0.99) | (np.abs(x.imag) > 0.99)))
        n = self.NFFT
        # a spectrum every ~4096 samples is plenty; at random offsets, so a jammer that repeats (a sweep every 1 or
        # 2 ms) isn't always caught at the same point of its cycle - that made a sweep look like a carrier
        k = int(self.rand.integers(0, 8 * n))
        while k + n <= len(x):
            self.acc += np.abs(np.fft.fftshift(np.fft.fft(x[k:k + n] * self.win))) ** 2
            self.nacc += 1
            k += 4 * n + int(self.rand.integers(0, 8 * n))
        now = time.time()
        if now - self.t_last >= 1.0 and self.nacc:
            self._report(now)

    def _report(self, now):
        self.t_last = now
        power = 10 * np.log10(self.p_sum / max(self.p_n, 1) + 1e-20)        # dB of full scale
        spec = 10 * np.log10(self.acc / self.nacc / self.NFFT + 1e-20)
        clip = 100.0 * self.clip / max(self.p_n, 1)
        rms_pct = 100 * np.sqrt(self.p_sum / max(self.p_n, 1) / 2)
        self._reset()
        if self.on_disk and self.path and not os.path.exists(self.path):    # 'Relearn quiet noise' in the tracker
            self.base_spec = self.base_power = None
            self.on_disk = False
            self.learn = []
            log('GNSS noise: baseline deleted - learning it again')
        learning = self.base_spec is None
        if learning:
            self.learn.append((power, spec))
            if len(self.learn) >= self.LEARN_S:
                self.base_power = float(np.median([p for p, _ in self.learn]))
                self.base_spec = np.median([s for _, s in self.learn], axis=0)
                self.learn = []
                self._save()
                log('GNSS noise: baseline learned (band power %.1f dBFS)' % self.base_power)
            emit('GNOISE: %.3f %.2f 0 %.4f 0 0 0 1 %.2f' % (now, power, clip, rms_pct))
            return
        rise = power - self.base_power
        d = (spec - self.base_spec)[self.use]
        floor = float(np.median(d))
        i = int(np.argmax(d))
        peak = float(d[i] - floor)          # above the band's own (possibly raised) level
        peak_mhz = self.freqs[self.use][i] / 1e6
        emit('GNOISE: %.3f %.2f %.2f %.4f %.2f %.4f %.2f 0 %.2f' % (now, power, rise, clip, peak, peak_mhz, floor,
                                                                    rms_pct))
        # the whole spectrum against the baseline, coarsely (each of SPEC_BINS the max of its group, so a carrier
        # keeps its height): the Integrity section keeps it around events as a waterfall
        f = self.freqs[self.use]
        groups = np.array_split(np.arange(len(d)), self.SPEC_BINS)
        emit('GSPEC: %.3f %.4f %.4f %s' % (now, f[0] / 1e6, f[-1] / 1e6,
                                           ','.join('%d' % round(10 * d[g].max()) for g in groups)))
        if abs(rise) < 1.0 and peak < 6.0 and clip < 0.01:     # quiet: follow slow drifts (temperature)
            self.base_power += self.ADAPT * rise
            self.base_spec += self.ADAPT * (spec - self.base_spec)
            if now - self.t_saved > self.SAVE_S:
                self._save()

    def _save(self):
        self.t_saved = time.time()
        if not self.path:
            return
        try:
            with open(self.path + '.tmp', 'wb') as f:
                np.savez(f, key=self.key, spec=self.base_spec, power=self.base_power)
            os.replace(self.path + '.tmp', self.path)
            self.on_disk = True
        except OSError as e:
            log('GNSS noise: could not save the baseline (%s)' % e)


# ---------------------------------------------------------------- raw samples around an event
class IQRecorder:
    """keeps the last PRE_S seconds of raw samples in memory; when the tracker writes a file name into the trigger
    file (--snap-trigger: an Integrity event began), saves them plus the next POST_S seconds as <name>.cs8 - signed
    8-bit I,Q pairs, the SDR's own ADC values (fixed scale, so a jammer's real level is kept; SDR++ / inspectrum /
    numpy open it) - with <name>.json saying the rate, frequency and time of the first sample"""
    PRE_S, POST_S = 20.0, 20.0

    def __init__(self, rate, center, trigger):
        self.rate, self.center, self.trigger = rate, center, trigger
        self.n = int(self.PRE_S * rate)
        self.ring = np.zeros(2 * self.n, np.int8)
        self.pos = self.filled = 0
        self.rec = None
        self.t_check = 0.0
        try:
            os.remove(trigger)          # (a request left over from an earlier session)
        except OSError:
            pass

    def __call__(self, x):
        b = np.empty(2 * len(x), np.int8)
        b[0::2] = np.clip(np.round(x.real * 128), -128, 127)
        b[1::2] = np.clip(np.round(x.imag * 128), -128, 127)
        if self.rec:
            self.rec['q'].put(b.tobytes())
            self.rec['left'] -= len(x)
            if self.rec['left'] <= 0:
                self.rec['q'].put(None)
                self.rec = None
        k = len(b)
        if k >= len(self.ring):
            self.ring[:] = b[-len(self.ring):]
            self.pos, self.filled = 0, self.n
        else:
            end = self.pos * 2 + k
            if end <= len(self.ring):
                self.ring[self.pos * 2:end] = b
            else:
                cut = len(self.ring) - self.pos * 2
                self.ring[self.pos * 2:] = b[:cut]
                self.ring[:k - cut] = b[cut:]
            self.pos = (self.pos + len(x)) % self.n
            self.filled = min(self.n, self.filled + len(x))
        now = time.time()
        if now - self.t_check >= 1.0:
            self.t_check = now
            self._poll(now)

    def _poll(self, now):
        if not os.path.exists(self.trigger):
            return
        try:
            with open(self.trigger) as f:
                stem = f.read().strip()
            os.remove(self.trigger)
        except OSError:
            return
        if self.rec or not stem:        # already recording one: that one covers this too
            return
        pre = np.concatenate([self.ring[self.pos * 2:], self.ring[:self.pos * 2]])[-2 * self.filled:] \
            if self.filled else np.zeros(0, np.int8)
        t_first = now - self.filled / self.rate - usb_latency()
        q = queue.Queue()
        self.rec = {'q': q, 'left': int(self.POST_S * self.rate)}
        threading.Thread(target=self._write, args=(stem, pre, q, t_first), daemon=True).start()

    def _write(self, stem, pre, q, t_first):
        n = len(pre) // 2
        try:
            os.makedirs(os.path.dirname(stem), exist_ok=True)
            with open(stem + '.cs8', 'wb') as f:
                f.write(pre.tobytes())
                while True:
                    b = q.get()
                    if b is None:
                        break
                    f.write(b)
                    n += len(b) // 2
            with open(stem + '.json', 'w') as f:
                json.dump({'format': 'cs8: signed 8-bit I,Q pairs (the SDR ADC values; divide by 128 for -1..1)',
                           'sample_rate': self.rate, 'center_hz': self.center, 'samples': n,
                           'seconds': n / self.rate, 'first_sample_unix': t_first,
                           'first_sample_utc': datetime.datetime.fromtimestamp(t_first, datetime.timezone.utc)
                           .strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] + 'Z',
                           'event_at_s': self.PRE_S, 'gain_db': opts.gain, 'if_gain_db': opts.if_gain,
                           'auto_gain': opts.auto_gain, 'ppm': opts.ppm}, f, indent=1)
            log('GNSS: saved %.0f s of raw samples around the event (%s.cs8)' % (n / self.rate, stem))
        except Exception as e:
            log('GNSS: could not save the raw samples (%r)' % e)


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
_started_at = [None]            # the stream's first sample (unix), for how long the first fix took


def started(t):
    if _started.is_set():
        return
    _started.set()
    _started_at[0] = t
    if opts.start_file:
        with open(opts.start_file, 'w') as f:
            f.write(datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] + 'Z\n')


# ---------------------------------------------------------------- what is kept between sessions (warm start)
# --spp-state (gnss-solver.json): GPS's ionosphere model and the newest broadcast orbit of each GPS / Galileo
# satellite. Like a handheld's memory, it makes the next start warm: our solver has orbits at once, and pocket_trk
# is told which satellites are up (visible_prns) instead of searching for all of them.
STATE = {}
QUICK_RES_M = 15.0              # quick (not yet smoothed) fixes: ranges must agree this well
SEARCH_HOURS = 4.0              # the search list covers satellites up at any time in the next few hours
EPH_KEEP_DAYS = 7.0
_state_saved = [0.0]


def load_state():
    try:
        with open(opts.spp_state) as f:
            st = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    if time.time() - st.get('t', 0) > EPH_KEEP_DAYS * 86400:
        st.pop('ion', None)
    st['ephs'] = {s: v for s, v in st.get('ephs', {}).items() if time.time() - v[0] < EPH_KEEP_DAYS * 86400}
    return st


def keep_eph(sat, t, text):
    STATE.setdefault('ephs', {})[sat] = [t, text]
    save_state()


def save_state(force=False):
    """written at most once a minute (or at once with force), to a temporary file swapped in"""
    if not opts.spp_state or (not force and time.time() - _state_saved[0] < 60):
        return
    _state_saved[0] = time.time()
    STATE['t'] = time.time()
    try:
        with open(opts.spp_state + '.tmp', 'w') as f:
            json.dump(STATE, f)
        os.replace(opts.spp_state + '.tmp', opts.spp_state)
    except OSError as e:
        log('GNSS: could not save the solver state (%s)' % e)


def visible_prns(sys_letter, n_prn, hours=SEARCH_HOURS):
    """the PRNs of one system worth searching for: those whose saved orbit (up to a week old - plenty for knowing
    whether a satellite is up) puts them above the horizon here at some time in the next `hours`, plus any we have
    no orbit for (can't tell). None when too few orbits are known to judge, or no home location."""
    if not opts.home:
        return None
    try:
        lat, lon, alt_km = (float(v) for v in opts.home.split(','))
    except ValueError:
        return None
    lla = (math.radians(lat), math.radians(lon), alt_km * 1000.0)
    rx = spp.lla_to_ecef(*lla)
    known, up = set(), set()
    now = time.time()
    for sat, (t, text) in STATE.get('ephs', {}).items():
        if sat[0] != sys_letter:
            continue
        e = gnss.parse_eph('$EPH,%.3f,%s,%s' % (t, sat, text))
        if not e:
            continue
        known.add(e['prn'])
        ts = np.arange(now, now + hours * 3600 + 1, 600.0)
        pos = gnss._kepler(e, ts)[0]
        for p in pos:
            if spp.azel(rx, p, lla)[1] > math.radians(-2.0):
                up.add(e['prn'])
                break
    if len(known) < (12 if sys_letter == 'G' else 6):
        return None
    return sorted(up | (set(range(1, n_prn + 1)) - known))


def live_signals():
    """pocket_trk's signal list for this session: the satellites that are up (or unknown), else all of them"""
    # (off unless --narrow-search: on a recording, pocket_trk with the shorter list locked 4-5 satellites in 40 s
    # where the full list locked 6 - it already searches every satellite in parallel on its 72 channels)
    if opts.constellation != 'gps' or not opts.narrow_search:
        return SIGNALS
    g = visible_prns('G', 32)
    e = visible_prns('E', 36)
    if g is None and e is None:
        return SIGNALS
    gl = ','.join(str(p) for p in g) if g else '1-32'
    el = ','.join(str(p) for p in e) if e else '1-36'
    log('GNSS: searching for %s GPS and %s Galileo satellites (the ones up in the next %.0f h, or not known yet)' % (
        len(g) if g else 32, len(e) if e else 36, SEARCH_HOURS))
    return ['-sig', 'L1CA', '-prn', gl + ',' + WAAS_PRNS, '-sig', 'E1B', '-prn', el]


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


class BeidouNames:
    """pocket_trk's B1I channel number is not always the satellite's PRN: on live data its 'C38' carried an orbit
    that was the satellite the public catalog lists as C43 (to 11 km; the real C38 was below the horizon), and its
    'C13' had C49's Doppler. So each BeiDou satellite is named by its own decoded orbit: the public catalog
    satellite (names only) within 300 km of where its ephemeris puts it. Its tracking lines wait for that (up to
    WAIT_S; without an orbit by then they go out under the channel's number)."""
    WAIT_S = 300.0

    def __init__(self):
        self.alias = {}             # channel 'C38' -> the satellite 'C43'
        self.waiting = {}           # channel -> [first seen, [lines held back]]
        self.sats = []
        try:
            import catalog
            lines = open(catalog.tle_file('gnss')).read().splitlines()
            self.sats = [catalog.Sat(lines[i].strip(), lines[i + 1], lines[i + 2]) for i in range(len(lines) - 2)
                         if lines[i].startswith('BEIDOU') and lines[i + 1].startswith('1 ') and '(C' in lines[i]]
            self.match = catalog.match_ecef
        except Exception as e:
            log('BeiDou: no name list (%s) - satellites go by their channel numbers' % e)

    def identify(self, chan, fields, t):
        """a channel's decoded ephemeris: which satellite it is"""
        if chan in self.alias or not self.sats:
            return self.alias.get(chan, chan)
        try:
            eph = gnss.finish_eph(dict(fields), 'C', int(chan[1:]), 'B1I')
            pos = gnss.sat_ecef(eph, t)[0]
            name, km = self.match(self.sats, datetime.datetime.fromtimestamp(t, datetime.timezone.utc), pos)
        except Exception as e:
            log('BeiDou: could not check %s (%s)' % (chan, e))
            name, km = None, None
        sat = 'C%02d' % int(name[name.rindex('(C') + 2:].rstrip(')').strip()) if name else chan
        self.alias[chan] = sat
        if sat != chan:
            log('BeiDou: channel %s is satellite %s (%s, its orbit %.0f km from the catalog\'s)' % (chan, sat, name, km))
        elif name is None:
            log('BeiDou: %s - no catalog satellite near its orbit (%s km); keeping its channel number'
                % (chan, '%.0f' % km if km is not None else '?'))
        return sat

    def hold(self, chan, line):
        """True if this channel's line must wait for its identity (kept to send later)"""
        if chan in self.alias or not self.sats:
            return False
        w = self.waiting.setdefault(chan, [time.time(), []])
        if time.time() - w[0] > self.WAIT_S:
            self.alias[chan] = chan
            self.release(chan)
            return False
        w[1].append(line)
        return True

    def release(self, chan):
        sat = self.alias.get(chan, chan)
        for line in self.waiting.pop(chan, [None, []])[1]:
            emit(line.replace(' %s ' % chan, ' %s ' % sat, 1))


class Lines:
    """pocket_trk's log lines and decoded orbits -> this receiver's GCH / GEPH / GPOS lines"""

    def __init__(self):
        self.eph = {}           # satellite -> its last ephemeris fields (sent again only when they change)
        self.last_ch = {}       # satellite -> receiver time of its last GCH line (one a second is plenty)
        self.seen = set()
        self.fixes = 0
        self.sbas_types = {}    # WAAS message type -> how many decoded (for the status line)
        self.glo_slot = {}      # GLONASS frequency channel -> the satellite's slot ('R16')
        self.clock = False      # live mode: when() is the PC time a sample was taken, so fixes time the PC clock
        self.bds = BeidouNames() if opts.constellation == 'beidou' else None
        self.offsets = collections.deque(maxlen=10)
        self.last_gtime = 0.0
        self.last_epoch = {}    # satellite -> the receiver second of its last GCH line
        self.errors = {}        # exception text -> when it was last logged (a bad line must not kill the reader)
        self.reset_run()
        # our own position solution (spp.py: WAAS corrections + carrier smoothing) from pocket_trk's raw measurements,
        # GPS L1 band only; GPS's ionosphere model is kept between sessions (it takes up to 12.5 min to arrive)
        self.spp = None
        self.spp_fixes = 0
        self.first_fix = None
        if opts.constellation == 'gps' and not opts.no_spp:
            self.spp = spp.Solver(iono='sbas', sbas_orbit=True, smooth=True)
            # a warm start, like a handheld's: the orbits decoded in earlier sessions (good for hours), so a position
            # comes as soon as 4 satellites are locked, without waiting ~a minute for each orbit to be sent again
            self.spp.ion = STATE.get('ion')
            n = 0
            for sat, (t, text) in STATE.get('ephs', {}).items():
                e = gnss.parse_eph('$EPH,%.3f,%s,%s' % (t, sat, text))
                if e and abs(time.time() - e['toe']) < 4 * 3600:
                    self.spp.eph(e)
                    n += 1
            log('GNSS position solver: warm start with %d saved orbits%s' % (
                n, ' and the ionosphere model' if self.spp.ion else ''))

    def reset_run(self):
        """a new pocket_trk run: its receiver time starts again, and so does the watchdog's memory of it"""
        self.oor = collections.deque()      # receiver times of 'PSEUDORANGE OUT OF RANGE' for GPS / Galileo
        self.clock_lost = None              # set to a reason when pocket_trk must be restarted
        self.rt_now = 0.0                   # the latest receiver time seen
        self.last_pos_rt = None             # receiver time of its last position fix
        self.fsync = {}                     # satellite -> receiver time it last had frame sync

    def watchdog(self):
        """why pocket_trk should be restarted now, or None.
        After a sample loss of ~90 ms or more that got past the zero filling (or happened before it existed),
        pocket_trk's clock is that much early: every GPS range falls below its accepted window and it logs
        'PSEUDORANGE OUT OF RANGE' every epoch for ever - it only re-aligns its clock when it has a fix, and it can't
        get one (seen 2026-11-03 and 2026-11-06: 6 and 8 hours of locked satellites without a position). A restart
        costs under a minute; our own solver keeps its orbits and state."""
        if self.clock_lost:
            return self.clock_lost
        # no fix for a long time although plenty of satellites have their frames: its clock is lost some other way
        synced = sum(1 for s, rt in self.fsync.items() if s[0] == 'G' and self.rt_now - rt < 5)
        if self.last_pos_rt is not None and synced >= 5 and self.rt_now - self.last_pos_rt > 300:
            return 'no-fix'
        return None

    def _event(self, f, when):
        """pocket_trk's own log events ($LOG,rt,sig,prn,text) -> GLOG lines, and the clock-lost watchdog"""
        # two layouts: '$LOG,rt,sig,prn,TEXT' (SIGNAL FOUND / LOST, FALSE LOCK) and '$LOG,rt,sat,sig,prn,TEXT'
        # (PSEUDORANGE OUT OF RANGE and the other navigation messages); receiver-wide ones have no satellite at all
        rt = float(f[1])
        if re.match(r'^[GERCJS][-+]?\d+$', f[2].strip()):
            sat, text = f[2].strip(), ','.join(f[5:]).strip()
        else:
            sig = f[2].strip()
            try:
                prn = int(f[3])
            except ValueError:
                prn = 0
            sat = _log_sat(sig, prn) if sig and prn else ''
            text = ','.join(f[4 if prn or sig else 2:]).strip()
        t = when(rt)
        nums = _NUM.findall(text.split('(', 1)[1]) if '(' in text else []
        if text.startswith('SIGNAL FOUND'):
            emit('GLOG: %.3f FOUND %s %s' % (t, sat, ' '.join(nums[:2])))
        elif text.startswith('SIGNAL LOST'):
            emit('GLOG: %.3f LOST %s %s' % (t, sat, ' '.join(nums[:2])))
        elif text.startswith('FALSE LOCK'):
            emit('GLOG: %.3f FALSELOCK %s' % (t, sat))
        elif text.startswith('PVT EPOCH ADJUSTED'):
            emit('GLOG: %.3f EPOCH %s' % (t, nums[0] if nums else '?'))
        elif text.startswith('RECEIVER SYNC'):
            emit('GLOG: %.3f SYNC %s' % (t, text.split()[-1]))
        elif text.startswith('PSEUDORANGE OUT OF RANGE') and sat[:1] in 'GE':
            self.oor.append(rt)
            while self.oor and self.oor[0] < rt - 10:
                self.oor.popleft()
            if len(self.oor) >= 20 and not self.clock_lost:    # most satellites, every epoch, for 10 s: it is stuck
                self.clock_lost = 'clock-lost'

    def orbits(self, ephs, t):
        for sat, sig, fields in ephs:      # in the field order of a $EPH line, so gnss.parse_eph reads it
            if self.bds and sat.startswith('C'):
                chan = sat
                sat = self.bds.identify(chan, fields, t)
                self.bds.release(chan)
            if fields.get('glo'):           # GLONASS: 'GLO,' + its position / velocity / acceleration at tb
                text = same = 'GLO,' + ','.join(repr(float(fields[k])) for k in gnss.GLO_FIELDS)
            else:
                text = ','.join([sig] + [str(fields[k]) for k in gnss.FIELDS])
                same = ','.join([sig] + [str(fields[k]) for k in gnss.FIELDS if k != 'ttr_gpst'])   # (when received)
            if self.eph.get(sat) != same:
                self.eph[sat] = same
                emit('GEPH: %.3f %s %s' % (t, sat, text))
                if not fields.get('glo') and sat[:1] in 'GE':
                    keep_eph(sat, t, text)
                if self.spp and not fields.get('glo'):
                    self.spp.eph(gnss.parse_eph('$EPH,%.3f,%s,%s' % (t, sat, text)))

    def _spp(self, f, line):
        """pocket_trk's raw measurements and navigation messages into our own solver; a GSPP line per good fix"""
        if f[0] == '$NAV':
            self.spp.nav(line)
            if self.spp.ion and self.spp.ion != STATE.get('ion'):
                STATE['ion'] = self.spp.ion
                save_state(force=True)
        elif f[0] == '$EPH':
            e = gnss.parse_eph(line)
            self.spp.eph(e)
            if e and e['sys'] in 'GE':
                keep_eph(f[2], float(f[1]), ','.join(f[3:]))
        elif f[0] == '$OBS':
            try:
                s = self.spp.obs(line)
            except Exception as e:      # our solver must never stop the tracking
                log('GNSS position solver: %r' % e)
                return
            if not s:
                return
            # good fixes; and before smoothing has settled, quick ones (good geometry, ranges that agree) so the
            # first position comes as early as possible - n_smoothed says which is which
            quick = (not s['ok'] and s['nsat'] >= 4 and s['hdop'] <= spp.HDOP_MAX and s['res_rms'] is not None
                     and s['res_rms'] < QUICK_RES_M)
            if s['ok'] or quick:
                self.spp_fixes += 1
                t = float(gnss.gpst_to_unix(s['t']))
                if self.first_fix is None:
                    self.first_fix = t
                    log('GNSS position solver: first fix %.0f s after the start (%d satellites)' % (
                        t - (_started_at[0] or t), s['nsat']))
                emit('GSPP: %.3f %.9f %.9f %.3f %d %.2f %d %d %d %d' % (
                    t, s['lat'], s['lon'], s['h'], s['nsat'], s['hdop'],
                    s['iono'].get('sbas', 0), s['fast'], s['lt'], s['smoothed']))

    def line(self, line, when):
        """one log line; when(receiver time) -> unix time. Nothing a line can contain may stop the reader: a dead
        reader would leave pocket_trk running and the tracker saying 'Receiving' with no output at all."""
        try:
            self._line(line, when)
        except Exception as e:      # (ValueError and IndexError are the usual ones: a cut or odd line)
            key = '%s: %s' % (type(e).__name__, e)
            if time.time() - self.errors.get(key, 0) > 60:
                self.errors[key] = time.time()
                log('GNSS: a pocket_trk line was not understood (%s): %s' % (key, line.strip()[:120]))

    def _line(self, line, when):
        f = line.strip().split(',')
        if not f or not f[0].startswith('$'):
            return
        if len(f) > 1:
            try:
                self.rt_now = max(self.rt_now, float(f[1]))
            except ValueError:
                pass
        if self.spp and f[0] in ('$NAV', '$OBS', '$EPH'):
            try:
                self._spp(f, line)
            except Exception as e:      # our solver must never stop the tracking
                key = 'solver %s: %s' % (type(e).__name__, e)
                if time.time() - self.errors.get(key, 0) > 60:
                    self.errors[key] = time.time()
                    log('GNSS position solver: %r on %s' % (e, line.strip()[:80]))
        if f[0] == '$CH' and len(f) > 10:
            rt, sat, sig, lock, cn0, fd = float(f[1]), f[4], f[5], float(f[7]), float(f[8]), float(f[10])
            if sat.startswith('R'):         # GLONASS: by its slot number, once that has been decoded
                if sat[1:].isdigit():
                    self.glo_slot[f[6]] = sat
                elif f[6] in self.glo_slot:
                    sat = self.glo_slot[f[6]]
                else:
                    return
            chan = sat
            if self.bds and sat.startswith('C'):    # BeiDou: by the satellite its orbit says it is
                sat = self.bds.alias.get(chan, chan)
            # one line a second per satellite - by the receiver second, not 'rt - last < 1.0' on floats, which
            # skipped a whole second for every satellite about twice an hour
            epoch = int(math.floor(rt))
            if lock < 1.0 or self.last_epoch.get(sat) == epoch:
                return
            self.last_epoch[sat] = epoch
            self.last_ch[sat] = rt
            self.seen.add(sat)
            extra = ''
            if len(f) > 25:     # sync flags and counters ($CH fields 12-15 and 23-25)
                sync = ''.join(c if f[12 + k].strip() not in ('', '0') else '-' for k, c in enumerate('SBFR'))
                extra = ' %s %d %d %d' % (sync, int(f[23]), int(f[24]), int(f[25]))
                if 'F' in sync:
                    self.fsync[sat] = rt
            text = 'GCH: %.3f %s %s %.1f %.1f %.1f%s' % (when(rt), sat, sig, cn0, fd, lock, extra)
            if not (self.bds and chan.startswith('C') and self.bds.hold(chan, text)):
                emit(text)
        elif f[0] == '$NAV' and len(f) > 7 and f[2].startswith('S'):
            self.sbas(f, when)
        elif f[0] == '$EPH' and len(f) > 4:
            sat, fields = f[2], ','.join(f[3:])
            if self.eph.get(sat) != fields:
                self.eph[sat] = fields
                emit('GEPH: %.3f %s %s' % (when(float(f[1])), sat, fields))
        elif f[0] == '$POS' and len(f) > 12:
            rt = float(f[1])
            t = gpst_to_unix([float(v) for v in f[2:8]])
            emit('GPOS: %.3f %s %s %s %s' % (t, f[8], f[9], f[10], f[12]))
            self.fixes += 1
            self.last_pos_rt = rt
            if self.clock and int(f[12]) >= 4:
                self.offsets.append(t - when(rt))
                if len(self.offsets) >= 5 and time.time() - self.last_gtime >= 10:
                    self.last_gtime = time.time()
                    o = np.array(self.offsets)
                    emit('GTIME: %.3f %.4f %s %.4f' % (time.time(), float(np.median(o)), f[12],
                                                      float(o.max() - o.min())))
        elif f[0] == '$LOG' and len(f) > 4:
            self._event(f, when)


def _bits(hexstr, n):
    return [int(c) for c in bin(int(hexstr, 16))[2:].zfill(len(hexstr) * 4)][:n]


def _sbas(self, f, when):
    """a WAAS satellite's message: type 9 (its own position) becomes a GEPH line"""
    try:
        msg = gnss.sbas_message(_bits(f[7], int(f[6])))
    except ValueError:
        return
    if not msg:
        return
    mtype, data = msg
    self.sbas_types[mtype] = self.sbas_types.get(mtype, 0) + 1
    if mtype != 9:
        return
    t = when(float(f[1]))
    nav = gnss.sbas_geo_nav(data, t)
    text = 'SBAS,' + ','.join('%.4f' % nav[k] if k != 't0' else '%.1f' % nav[k] for k in gnss.SBAS_FIELDS)
    if self.eph.get(f[2]) != text:
        self.eph[f[2]] = text
        emit('GEPH: %.3f %s %s' % (t, f[2], text))


Lines.sbas = _sbas


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

    def connect(self, proc, timeout=15.0):
        """wait for pocket_trk to open it - but not for ever: a pocket_trk that never opens the pipe (it refused an
        option, it crashed at start) used to hang the receiver here with nothing logged"""
        done = []

        def wait():
            done.append(bool(self.k32.ConnectNamedPipe(self.h, None)) or ctypes.get_last_error() == 535)  # 535: connected already

        th = threading.Thread(target=wait, daemon=True)
        th.start()
        t_end = time.time() + timeout
        while th.is_alive() and time.time() < t_end and proc.poll() is None:
            th.join(0.25)
        if th.is_alive():       # still waiting: cancel the call, or CloseHandle would wait for it too
            h = self.k32.OpenThread(0x0001, False, th.native_id)        # THREAD_TERMINATE is what CancelSynchronousIo needs
            if h:
                self.k32.CancelSynchronousIo(h)
                self.k32.CloseHandle(h)
            th.join(2.0)
        return bool(done and done[0])

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


def usb_latency():
    """seconds between a sample being taken and it reaching the flowgraph, beyond what the arrival times show:
    gr-osmosdr's RTL-SDR source keeps two USB buffers in hand (default 256 KiB = 64 ms at 2.048 MS/s each).
    Measured against internet time (NTP): 133 ms with the default buffers, 38 ms with 64 KiB ones - two buffers
    plus ~5 ms both times. Other devices: unknown (0)."""
    dev = opts.device.lower()
    if not dev.startswith('rtl'):
        return 0.0
    buflen = 262144
    for part in dev.split(','):
        if part.strip().startswith('buflen='):
            try:
                buflen = int(part.split('=', 1)[1])
            except ValueError:
                pass
    return 2 * (buflen / 2) / opts.rate + 0.005


class LiveTracker(threading.Thread):
    """runs pocket_trk on the pipe for the whole session (restarting it if it stops), feeds it, and reads its
    log and decoded orbits as they grow"""
    QUEUE_S = 4.0       # samples waiting for pocket_trk; beyond this they are dropped (it is falling behind)
    BLOCK = 4096        # GNU Radio hands the feeder blocks of this size (2 ms); the queue is sized in samples
    # A stall in the SDR / USB path loses samples before they ever reach this program (measured 2026-11-06: 96-143 ms
    # a few times a day, never a buffer's worth, no overflow message anywhere). Every block after it arrives that much
    # 'late' against the stream's start time. Jitter of the arrival times is 1-5 ms, so a steady excess of 20 ms is a
    # gap; a burst of blocks after a scheduling stall is told apart because its excess shrinks by 2 ms per block.
    GAP_MIN_S = 0.020
    GAP_BLOCKS = 30         # 60 ms of samples with a steady excess before it counts

    def __init__(self, rate):
        super().__init__(daemon=True)
        self.rate = rate
        self.q = queue.Queue(maxsize=max(100, int(self.QUEUE_S * rate / self.BLOCK)))
        self.out = Lines()
        self.folder = None if opts.work_stem else tempfile.mkdtemp(prefix='gnss_live_')
        self.dropped = 0            # samples dropped because pocket_trk fell behind (filled with zeros too)
        self.gaps = []              # (PC time, seconds, source) of every gap filled
        self.runs = 0
        self.out.clock = True
        # The PC time of the stream's first sample: each block arrives a little after its last sample was taken
        # (USB buffering, scheduling), so the earliest 'arrival - samples so far / rate' over the last 10 s is the
        # best estimate (sliding, so the SDR's crystal error can't build up)
        self.received = 0
        self.t0_cands = collections.deque()
        self.base_min = None        # that minimum over the candidates 1-10 s old (updated once a second)
        self.base_t = 0.0
        self.pend = collections.deque(maxlen=self.GAP_BLOCKS)     # recent blocks' start-time estimates, all 'late'

    def feed(self, data, n):
        now = time.time()
        c = now - (self.received + n) / self.rate
        if now - self.base_t >= 1.0:
            old = [v for t, v in self.t0_cands if t < now - 1.0]
            if len(old) >= 100:
                self.base_min = min(old)
            self.base_t = now
        if self.base_min is not None and c - self.base_min > self.GAP_MIN_S:
            self.pend.append(c)
            if len(self.pend) == self.GAP_BLOCKS and max(self.pend) - min(self.pend) < 0.010:
                gap = min(self.pend) - self.base_min
                n_gap = int(round(gap * self.rate))
                # the lost samples take their place as zeros: the pipe writer puts them in before this block
                try:
                    self.q.put_nowait((None, n_gap, self.received, 'usb'))
                except queue.Full:          # pocket_trk is behind as well: the block itself is dropped below and the
                    self.pend.clear()       # queue drop handles the count; try again with the next blocks
                else:
                    self.received += n_gap
                    self.gaps.append((now, gap, 'usb'))
                    c = now - (self.received + n) / self.rate
                    self.t0_cands.clear()
                    self.base_min = c
                    self.pend.clear()
        else:
            self.pend.clear()
        k = self.received
        self.received += n
        self.t0_cands.append((now, c))
        while self.t0_cands[0][0] < now - 10:
            self.t0_cands.popleft()
        try:
            self.q.put_nowait((data, n, k, None))
        except queue.Full:
            self.dropped += n

    def t_stream0(self):
        return min(c for _, c in list(self.t0_cands)) - usb_latency()

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
        if self.folder:
            logf = os.path.join(self.folder, 'trk-%d.log' % self.runs)
            rtcmf = os.path.join(self.folder, 'trk-%d.rtcm' % self.runs)
        else:       # beside the capture: the raw measurements ($OBS), navigation bits and RTCM observables are the
            # only complete record of a session - kept and trimmed like the capture's other raw files
            logf = '%s.trk%d.log' % (opts.work_stem, self.runs)
            rtcmf = '%s.trk%d.rtcm' % (opts.work_stem, self.runs)
        signals = live_signals()
        # a narrowed search list covers SEARCH_HOURS: before it runs out, pocket_trk is restarted with a fresh one
        # (the satellites are picked up again within a minute; our solver keeps its state)
        renew_at = time.time() + (SEARCH_HOURS - 0.5) * 3600 if signals is not SIGNALS else None
        cmd = [POCKET_TRK] + signals + ['-fmt', 'INT8X2', '-f', '%.6f' % (self.rate / 1e6),
                                        '-log', logf, '-rtcm', rtcmf, pipe.path] + POCKET_OPTS
        proc = subprocess.Popen(cmd, cwd=os.path.dirname(POCKET_TRK), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=0x08000000)
        if self.runs == 1:      # (what this session ran with: option changes can then be judged capture by capture)
            log('GNSS: pocket_trk command: ' + ' '.join(cmd[1:]))
            if POCKET_OPTS:
                try:
                    text = ' | '.join(l.split('#')[0].strip() for l in open(POCKET_OPTS[1]) if l.split('#')[0].strip())
                    log('GNSS: pocket_trk options: ' + (text or '(none)'))
                except OSError:
                    pass
        if not pipe.connect(proc):
            pipe.close()
            if proc.poll() is None:
                proc.kill()
            raise OSError('pocket_trk did not open the pipe (exit code %s)' % proc.poll())
        self.out.reset_run()
        while not self.q.empty():   # start from fresh samples
            self.q.get_nowait()
        written = 0                 # samples given to pocket_trk (zeros for gaps included)
        # (receiver time, the stream's sample number there): pocket_trk's clock counts the samples it was given,
        # which now include zeros for every lost one, so the stream time stays continuous; an anchor at the start
        anchors = []

        def when(rt):
            """pocket_trk receiver time -> the PC (unix) time that sample was taken"""
            a = [x for x in anchors if x[0] <= rt]
            art, k = a[-1] if a else (anchors[0] if anchors else (0.0, 0))
            return self.t_stream0() + k / self.rate + (rt - art)

        log('GNSS: live tracking started (pocket_trk run %d)' % self.runs)
        emit('GLOG: %.3f START %d' % (time.time(), self.runs))
        reader = threading.Thread(target=self.read, args=(proc, logf, rtcmf, when), daemon=True)
        next_k = None
        started_reader = False
        why = ''
        while proc.poll() is None:
            if renew_at and time.time() > renew_at and live_signals() != signals:
                log('GNSS: renewing the satellite search list')
                break
            reason = self.out.watchdog()
            if reason:
                why = reason
                break
            if started_reader and not reader.is_alive():
                why = 'reader-died'
                break
            try:
                data, n, k, source = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            if data is None:                # a gap found by feed(): zeros in its place
                if not self._zeros(pipe, n):
                    break
                written += n
                emit('GGAP: %.3f %.1f %s' % (time.time(), 1000.0 * n / self.rate, source))
                log('GNSS: %.0f ms of samples lost (%s) - filled with zeros' % (1000.0 * n / self.rate, source))
                next_k = k + n
                continue
            if next_k is None:              # the first block
                anchors.append((written / self.rate, k))
            elif k > next_k:                # blocks were dropped from the queue (pocket_trk fell behind): zeros too
                lost = min(k - next_k, int(self.QUEUE_S * self.rate))
                if not self._zeros(pipe, lost):
                    break
                written += lost
                self.gaps.append((time.time(), lost / self.rate, 'queue'))
                emit('GGAP: %.3f %.1f queue' % (time.time(), 1000.0 * lost / self.rate))
                if k - next_k > lost:       # more than the zeros cover: the stream time must start again from here
                    anchors.append((written / self.rate, k))
            next_k = k + n
            if not started_reader:
                reader.start()
                started_reader = True
            if not pipe.write(data):
                break
            written += n
        pipe.close()
        if proc.poll() is None:
            proc.kill()
        if started_reader:
            reader.join(timeout=5)
        if why:
            text = {'clock-lost': 'pocket_trk lost its clock (a sample gap got past the zero filling): every range '
                                  'out of range, no fix possible', 'no-fix': 'no fix for 5 min with 5+ satellites synced',
                    'reader-died': 'its log reader stopped'}.get(why, why)
            log('GNSS: restarting pocket_trk - %s' % text)
            emit('GLOG: %.3f RESTART %s' % (time.time(), why))
        log('GNSS: pocket_trk run %d stopped after %.0f s' % (self.runs, written / self.rate))

    def _zeros(self, pipe, n):
        """n zero samples into the pipe (in pieces: a long queue drop could be seconds)"""
        step = 1 << 19
        while n > 0:
            m = min(n, step)
            if not pipe.write(bytes(2 * m)):
                return False
            n -= m
        return True

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
                for rec in _RECORD.split(line) if line.count('$') > 1 else (line,):
                    if rec.strip():
                        self.out.line(rec, when)
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
            except Exception as e:          # a bad RTCM frame must not stop the reader either
                log('GNSS: RTCM not understood (%r)' % e)
            if time.time() - last_note > 60:
                last_note = time.time()
                locked = sorted(s for s, rt in self.out.last_ch.items() if when(rt) > time.time() - 5)
                log('%d | GNSS live: %d satellites locked now (%s), %d broadcast orbits, %d position fixes '
                    '(%d WAAS-corrected), %d stream gaps (%.2f s filled with zeros), %.2f s dropped from the queue%s' % (
                        time.time(), len(locked), ' '.join(locked) or 'none', len(self.out.eph), self.out.fixes,
                        self.out.spp_fixes, len(self.gaps), sum(g[1] for g in self.gaps), self.dropped / self.rate,
                        ', WAAS messages %s' % self.out.sbas_types if self.out.sbas_types else ''))
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
            if opts.if_gain:
                _try('IF gain: %.1f dB' % opts.if_gain, src.set_if_gain, opts.if_gain, 0)
        _try('DC correction: %s' % ('off' if opts.no_dc else 'on'), src.set_dc_offset_mode, 0 if opts.no_dc else 2, 0)
        self.writer = sink
        self.connect(src, self.writer)
        self.taps = []
        if opts.spectrum_port:      # (every 8th FFT frame: the signals are under the noise, and every frame cost 0.2 core)
            self.taps.append(sdr_taps.add_spectrum(self, src, opts.spectrum_port, center, rate, every=8))
        if opts.audio_port:
            self.taps.append(sdr_taps.add_audio(self, src, opts.audio_port, rate, channel_hz=36e3, demod='am'))


def clean_temp():
    """earlier versions left pocket_trk's working folders in %TEMP% for ever (13 GB on one PC, with position fixes
    in them); now the files live beside the capture. Old leftovers go: the chunk-mode sample files (gnss_rx_*, up to
    740 MB each, no use once processed) after a day, the live logs (gnss_live_*, small, worth keeping a while for
    analysis) after 30 days - never a folder still being written to"""
    root = tempfile.gettempdir()
    now = time.time()
    freed = 0
    for name in os.listdir(root):
        if not name.startswith(('gnss_live_', 'gnss_rx_')):
            continue
        p = os.path.join(root, name)
        keep_s = 30 * 86400 if name.startswith('gnss_live_') else 86400
        try:
            files = [os.path.join(p, f) for f in os.listdir(p)]
            newest = max([os.path.getmtime(f) for f in files] + [os.path.getmtime(p)])
            if now - newest > keep_s:
                freed += sum(os.path.getsize(f) for f in files)
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
    if freed:
        log('GNSS: removed %.0f MB of old working files from the temporary folder' % (freed / 2 ** 20))


def main():
    if not os.path.exists(POCKET_TRK):
        log('GNSS: %s is missing - see README (PocketSDR)' % POCKET_TRK)
        sys.exit(2)
    global NOISE, SNAP
    STATE.update(load_state())
    try:
        clean_temp()
    except Exception as e:
        log('GNSS: could not tidy the temporary folder (%r)' % e)
    if opts.pocket_opts and os.path.exists(opts.pocket_opts):
        POCKET_OPTS[:] = ['-opt', opts.pocket_opts]
        log('GNSS: pocket_trk options from %s' % opts.pocket_opts)
    if not opts.no_noise:
        NOISE = NoiseMeter(opts.rate, opts.center, opts.noise_baseline)
    if opts.snap_trigger:
        SNAP = IQRecorder(opts.rate, opts.center, opts.snap_trigger)
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
