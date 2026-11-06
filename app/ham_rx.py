"""Ham satellite receiver (ISS and CubeSats, 2 m or 70 cm): listens to the whole received slice, and when a
signal appears, measures its exact frequency (its Doppler shift) and tries the decoders of the satellites
that transmit near there. receiver.py starts it and saves its lines to the capture's .live file.

  python ham_rx.py --band ham2m|ham70 --device ARGS --rate R --center F [--gain DB | --auto-gain] [--ppm X]
                   [--no-dc] [--start-file PATH] [--spectrum-port N] [--audio-port N]

How it works, like an operator watching a waterfall:
  1. The slice is split into ~50 kHz channels twice over, the second grid offset by half a channel, so every
     signal lies well inside a channel of one grid.
  2. Each channel's power is watched against its own noise floor. When a signal rises above it, that stretch
     of the channel (plus a moment before) is kept until the signal ends.
  3. The kept signal's carrier frequency is measured from its spectrum (this is the Doppler measurement),
     the signal is moved to 0 Hz, and gr-satellites' decoders of every satellite transmitting within
     +-SEARCH_HZ of it are tried. A frame that passes its checksum names the satellite. No orbit data is
     used to choose or tune decoders.
  4. ISS: its APRS digipeater (145.825 MHz, AFSK 1200, AX.25). A packet counts only if the ISS itself
     repeated it (RS0ISS / NA1SS in the path, marked as repeated) - ground stations use the same frequency.
Lines (frame contents are never written out):
  HFR: t Hz norad name snr bytes callsign   a decoded frame (name: gr-satellites' name, spaces as _)
  HSIG: t Hz - snr seconds                  a signal heard but not decoded
"""
import argparse
import datetime
import glob
import os
import queue
import sys
import tempfile
import threading
import time
from collections import deque

import numpy as np
import osmosdr
import pmt
import yaml
from gnuradio import blocks, gr
from gnuradio.filter import firdes, pfb

import satellites
from satellites.core.gr_satellites_flowgraph import gr_satellites_flowgraph

import sdr_taps

SPACING_HZ = 50e3          # channel spacing (each channel passes +-45% of it)
OVERSAMPLE = 2
WINDOW_S = 0.01            # power is measured over windows this long
DETECT_DB = 7.0            # a signal: this far over the channel's noise floor
FLOOR_S = 6.0              # the noise floor is a low percentile of the last few seconds
PRE_S, POST_S = 0.4, 0.25  # kept before a signal starts / quiet time that ends it
MIN_S, MAX_S = 0.06, 8.0   # shorter blips are ignored; long signals are cut into pieces
SEARCH_HZ = 15e3           # decoders tried: transmitters this close to the measured carrier
ISS_CALLS = ('RS0ISS', 'NA1SS', 'ARISS')
ISS = {'name': 'ISS', 'norad': 25544, 'data': {},
       'transmitters': {'APRS digipeater': {'frequency': 145.825e6, 'modulation': 'AFSK', 'baudrate': 1200,
                                            'af_carrier': 1700, 'deviation': 500, 'framing': 'AX.25'}}}

ap = argparse.ArgumentParser()
ap.add_argument('--band', default='ham70')
ap.add_argument('--device', default='rtl=0')
ap.add_argument('--rate', type=float, default=2.4e6)
ap.add_argument('--center', type=float, default=436.725e6)
ap.add_argument('--gain', type=float, default=35.0)
ap.add_argument('--auto-gain', action='store_true')
ap.add_argument('--if-gain', type=float, default=60.0)   # E4000 IF gain (sdr_settings.if_gain; others ignore it)
ap.add_argument('--ppm', type=float, default=0.0)
ap.add_argument('--no-dc', action='store_true')
ap.add_argument('--start-file', default='')
ap.add_argument('--spectrum-port', type=int, default=0)
ap.add_argument('--audio-port', type=int, default=0)
ap.add_argument('--parent-pid', type=int, default=0)   # stop when the tracker that started us is gone
ap.add_argument('--yaml-dir', default='')       # for tests: a folder of satellite definitions instead of gr-satellites'
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


# ---------------------------------------------------------------- the satellites we can decode
def transmitters(lo, hi):
    """[(frequency, satellite dict, transmitter key)] of every gr-satellites transmitter between lo and hi Hz"""
    folder = opts.yaml_dir or os.path.join(os.path.dirname(satellites.__file__), 'satyaml')
    out = []
    for f in sorted(glob.glob(os.path.join(folder, '*.yml'))):
        try:
            sat = yaml.safe_load(open(f, encoding='utf-8'))
        except Exception:
            continue
        for key, tx in (sat.get('transmitters') or {}).items():
            fr = tx.get('frequency') or 0
            if lo <= fr <= hi and tx.get('modulation') in ('FSK', 'AFSK', 'BPSK', 'DBPSK'):
                out.append((float(fr), sat, key))
    return out


_yml_dir = tempfile.mkdtemp(prefix='ham_rx_')
_yml = {}


def one_transmitter_yaml(sat, key):
    """a satellite definition with just this transmitter (gr-satellites builds one decoder per transmitter)"""
    k = (sat['name'], key)
    if k not in _yml:
        one = {'name': sat['name'], 'norad': sat.get('norad', 0), 'data': sat.get('data') or {},
               'transmitters': {key: sat['transmitters'][key]}}
        for extra in ('transports',):
            if extra in sat:
                one[extra] = sat[extra]
        path = os.path.join(_yml_dir, '%d.yml' % len(_yml))
        with open(path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(one, f)
        _yml[k] = path
    return _yml[k]


# ---------------------------------------------------------------- AX.25 addresses
def ax25_addresses(frame):
    """[(callsign, ssid, repeated)] of an AX.25 frame's address field (destination, source, digipeaters)"""
    out = []
    for i in range(0, min(len(frame), 70) - 6, 7):
        a = frame[i:i + 7]
        call = ''.join(chr(b >> 1) for b in a[:6]).strip()
        if not call.replace('-', '').isalnum():
            return []
        out.append((call, (a[6] >> 1) & 0x0F, bool(a[6] & 0x80)))
        if a[6] & 1:            # last address
            return out
    return []


def iss_repeated(frame):
    """did the ISS send or digipeat this frame (rather than a ground station transmitting directly)?"""
    addr = ax25_addresses(frame)
    if len(addr) < 2:
        return False
    if addr[1][0] in ISS_CALLS:
        return True
    return any(call in ISS_CALLS and rep for call, _, rep in addr[2:])


# ---------------------------------------------------------------- decoding a kept signal
class Collector(gr.basic_block):
    def __init__(self):
        gr.basic_block.__init__(self, 'collector', in_sig=None, out_sig=None)
        self.frames = []
        self.message_port_register_in(pmt.intern('in'))
        self.set_msg_handler(pmt.intern('in'), self._got)

    def _got(self, msg):
        try:
            self.frames.append(bytes(pmt.u8vector_elements(pmt.cdr(msg))))
        except Exception:
            pass


def decode(x, rate, sat, key):
    """frames gr-satellites gets from samples x (at 0 Hz) with one transmitter's decoder"""
    tb = gr.top_block()
    src = blocks.vector_source_c(np.concatenate([x, np.zeros(int(rate * 0.3), np.complex64)]), False)
    fg = gr_satellites_flowgraph(file=one_transmitter_yaml(sat, key), samp_rate=rate, iq=True, grc_block=True,
                                 options='')
    sink = Collector()
    tb.connect(src, fg)
    tb.msg_connect((fg, 'out'), (sink, 'in'))
    tb.run()
    time.sleep(0.05)            # let the last messages through
    return sink.frames


def carrier_offset(x, rate, symmetric=False):
    """(the signal's carrier offset from the channel centre (Hz), its SNR (dB)): the power-weighted centre of
    its spectrum above the noise (FSK and AFSK are symmetric about their carrier); with symmetric=True,
    refined to the frequency the spectrum is most mirror-symmetric about.
    Measured on synthetic frames (bias ~0 either way; spread of the error):
      1200-baud FSK, AFSK 1200 on FM   centroid 6-160 Hz, symmetric ~3 Hz      -> symmetric
      9600-baud FSK                    centroid ~50 Hz,   symmetric ~80 Hz     -> centroid"""
    n = 2048
    if len(x) < n:
        x = np.concatenate([x, np.zeros(n - len(x), np.complex64)])
    segs = x[:len(x) // n * n].reshape(-1, n) * np.hanning(n)
    p = np.fft.fftshift(np.mean(np.abs(np.fft.fft(segs, axis=1)) ** 2, axis=0))
    f = np.fft.fftshift(np.fft.fftfreq(n, 1 / rate))
    inside = np.abs(f) <= SPACING_HZ * 0.45
    noise = np.median(p[inside])
    w = np.where(inside, np.maximum(p - 2 * noise, 0), 0)
    if w.sum() <= 0:
        return 0.0, 0.0
    centroid = float((f * w).sum() / w.sum())
    snr = float(10 * np.log10(p[inside].max() / max(noise, 1e-20)))
    if not symmetric:
        return centroid, snr
    spread = np.sqrt((((f - centroid) ** 2) * w).sum() / w.sum())
    s = np.sqrt(w)
    df = rate / n
    half = max(1500.0, 2 * spread)
    d = np.arange(0, half, df / 4)
    best = (None, centroid)
    for c in np.arange(centroid - 1500, centroid + 1500, 10.0):
        e = np.sum((np.interp(c + d, f, s) - np.interp(c - d, f, s)) ** 2)
        if best[0] is None or e < best[0]:
            best = (e, c)
    return float(best[1]), snr


class Decoder(threading.Thread):
    def __init__(self, signals, band_lo, band_hi):
        super().__init__(daemon=True)
        self.signals = signals
        tx = transmitters(band_lo, band_hi)
        if opts.band == 'ham2m':
            tx.append((ISS['transmitters']['APRS digipeater']['frequency'], ISS, 'APRS digipeater'))
        # transmitters with the same frequency and signal are decoded once, as a group; a frame from a group
        # of several satellites can't say which one sent it (unless it is the ISS digipeater's)
        self.groups = {}
        for fr, sat, key in tx:
            t = sat['transmitters'][key]
            g = (round(fr / 5e3), t.get('modulation'), t.get('baudrate'), t.get('framing'))
            self.groups.setdefault(g, []).append((fr, sat, key))
        self.recent = deque(maxlen=50)      # (time, frame bytes): the same frame caught by both grids
        self.heard = deque(maxlen=50)       # (time, Hz): the same undecoded signal caught by both grids
        shared = sum(1 for g in self.groups.values() if len({s['name'] for _, s, _ in g}) > 1)
        log('Ham: %d satellite transmitters in %.3f - %.3f MHz, %d decoders (%d shared by several satellites)'
            % (len(tx), band_lo / 1e6, band_hi / 1e6, len(self.groups), shared))

    def run(self):
        while True:
            t, centre, rate, x, spacing = self.signals.get()
            try:
                self.handle(t, centre, rate, x, spacing)
            except Exception as e:
                log('Ham decoder: %r' % e)

    def handle(self, t, centre, rate, x, spacing):
        off, snr = carrier_offset(x, rate)
        if abs(off) > spacing / 4 + 1e3:
            return              # the other grid has this signal nearer a channel centre
        hz = centre + off
        base = (x * np.exp(-2j * np.pi * off * np.arange(len(x)) / rate)).astype(np.complex64)
        decoded = False
        near = sorted((g for g in self.groups.values() if abs(g[0][0] - hz) <= SEARCH_HZ), key=lambda g: abs(g[0][0] - hz))
        for group in near:
            names = sorted({s['name'] for _, s, _ in group})
            variants = {}           # one decoder per distinct setting in the group (e.g. AFSK tones)
            for fr, s, k in group:
                t_ = s['transmitters'][k]
                variants.setdefault((t_.get('af_carrier'), t_.get('deviation'), t_.get('scrambler'),
                                     t_.get('frame size'), t_.get('precoding')), (s, k))
            frames = []
            for sat, key in variants.values():
                frames += [f for f in decode(base, rate, sat, key) if f not in frames]
            if frames and (sat['transmitters'][key].get('baudrate') or 9600) < 4800:
                fine, _ = carrier_offset(x, rate, symmetric=True)      # slow signals: the precise measurement
                hz = centre + fine
            for frame in frames:
                if any(abs(t - t0) < 3 and frame == f0 for t0, f0 in self.recent):
                    decoded = True
                    continue
                self.recent.append((t, frame))
                ax25 = 'AX.25' in sat['transmitters'][key].get('framing', '')
                addr = ax25_addresses(frame) if ax25 else []
                call = addr[1][0] if len(addr) > 1 else '-'
                who = None
                if any(s is ISS for _, s, _ in group) and ax25 and iss_repeated(frame):
                    who = ISS
                elif len(names) == 1 and names[0] != 'ISS':
                    who = sat
                # a frame from a shared frequency (or a ground station on the ISS's): unidentified, '?'
                emit('HFR: %.3f %.0f %d %s %.1f %d %s %s'
                     % (t, hz, who.get('norad') or 0 if who else 0, who['name'].replace(' ', '_') if who else '?',
                        snr, len(frame), call, '|'.join(n.replace(' ', '_') for n in names)))
                decoded = True
            if decoded:
                break
        if not decoded and not any(abs(t - t0) < 1 and abs(hz - h0) < 5e3 for t0, h0 in self.heard):
            self.heard.append((t, hz))
            emit('HSIG: %.3f %.0f - %.1f %.2f' % (t, hz, snr, len(x) / rate))


# ---------------------------------------------------------------- watching the channels
_started = threading.Event()


def started(t):
    if _started.is_set():
        return
    _started.set()
    if opts.start_file:
        with open(opts.start_file, 'w') as f:
            f.write(datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] + 'Z\n')


class Watch(gr.sync_block):
    """one channel grid: finds signals in its channels and hands each one (with a little before it) to the decoder"""

    def __init__(self, n, rate, centres, spacing, signals):
        gr.sync_block.__init__(self, 'ham_watch', in_sig=[np.complex64] * n, out_sig=None)
        self.n, self.rate, self.centres, self.spacing, self.signals = n, rate, centres, spacing, signals
        self.w = int(rate * WINDOW_S)
        self.pend = np.zeros((n, 0), np.complex64)
        self.hist = deque(maxlen=int(PRE_S / WINDOW_S))
        self.powers = deque(maxlen=int(FLOOR_S / WINDOW_S))
        self.floor = None
        self.active = {}            # channel -> [start time, [windows], quiet windows, kept windows]
        self.t = None

    def work(self, input_items, output_items):
        k = len(input_items[0])
        if self.t is None:
            self.t = time.time() - k / self.rate
            started(self.t)
        self.pend = np.concatenate([self.pend, np.stack([np.asarray(a) for a in input_items])], axis=1)
        while self.pend.shape[1] >= self.w:
            win, self.pend = self.pend[:, :self.w], self.pend[:, self.w:]
            self.window(win)
            self.t += WINDOW_S
        return k

    def window(self, win):
        p = np.mean(np.abs(win) ** 2, axis=1)
        self.powers.append(p)
        if self.floor is None or len(self.powers) % 50 == 0:
            self.floor = np.percentile(np.array(self.powers), 20, axis=0) + 1e-20
        loud = p > self.floor * 10 ** (DETECT_DB / 10)
        for ch in range(self.n):
            a = self.active.get(ch)
            if loud[ch]:
                if a is None:
                    before = [h[ch] for h in self.hist]
                    self.active[ch] = [self.t - len(before) * WINDOW_S, before + [win[ch]], 0, 1]
                else:
                    a[1].append(win[ch])
                    a[2] = 0
                    a[3] += 1
                    if len(a[1]) * WINDOW_S >= MAX_S:
                        self.finish(ch)
            elif a is not None:
                a[1].append(win[ch])
                a[2] += 1
                if a[2] * WINDOW_S >= POST_S:
                    self.finish(ch)
        self.hist.append(win)

    def finish(self, ch):
        t, wins, _, loud = self.active.pop(ch)
        if loud * WINDOW_S < MIN_S:
            return
        try:
            self.signals.put_nowait((t + PRE_S, self.centres[ch], self.rate, np.concatenate(wins), self.spacing))
        except queue.Full:          # the decoder is far behind: drop rather than hold up the radio
            pass


class Receiver(gr.top_block):
    def __init__(self, signals):
        gr.top_block.__init__(self, 'ham_rx')
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
        n = int(round(rate / SPACING_HZ / OVERSAMPLE)) * OVERSAMPLE     # the channelizer needs n divisible by it
        spacing = rate / n
        ch_rate = rate / n * OVERSAMPLE
        taps = firdes.low_pass(1.0, rate, 0.45 * spacing, 0.1 * spacing)
        order = [i if i < n / 2 else i - n for i in range(n)]       # the channelizer's outputs, in FFT order
        self.keep = []
        for shift in (0.0, spacing / 2):
            chan = pfb.channelizer_ccf(n, taps, OVERSAMPLE)
            centres = [center + shift + k * spacing for k in order]
            watch = Watch(n, ch_rate, centres, spacing, signals)
            if shift:
                rot = blocks.rotator_cc(-2 * np.pi * shift / rate)
                self.connect(src, rot, chan)
                self.keep.append(rot)
            else:
                self.connect(src, chan)
            for i in range(n):
                self.connect((chan, i), (watch, i))
            self.keep += [chan, watch]
        log('Ham: %d channels of %.1f kHz, twice over (offset half a channel), %.1f kS/s each'
            % (n, spacing / 1e3, ch_rate / 1e3))
        self.band = (center - rate / 2 + spacing / 2, center + rate / 2 - spacing / 2)
        if opts.spectrum_port:
            self.keep.append(sdr_taps.add_spectrum(self, src, opts.spectrum_port, center, rate))
        if opts.audio_port:
            self.keep.append(sdr_taps.add_audio(self, src, opts.audio_port, rate, channel_hz=12.5e3, demod='fm'))


def main():
    signals = queue.Queue(maxsize=200)
    tb = Receiver(signals)
    dec = Decoder(signals, *tb.band)
    dec.start()
    tb.start()
    log('Receiving %s at %.4f MHz, %.4f MS/s' % (opts.band, opts.center / 1e6, opts.rate / 1e6))
    try:
        while sdr_taps.parent_alive(opts.parent_pid):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    tb.stop()
    tb.wait()


if __name__ == '__main__':
    main()
