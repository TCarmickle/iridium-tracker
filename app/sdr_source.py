r"""Runs gr-iridium's iridium-extractor with the extras its command line doesn't offer:
frequency correction (PPM), automatic gain, IQ / DC correction, and a live spectrum (FFT) of the
same samples the decoder sees, sent to the tracker over a local UDP socket.

Usage (receiver.py starts it):
  python sdr_source.py CONF THRESHOLD_DB [--spectrum-port N] [--ppm X] [--auto-gain] [--no-iq] [--no-dc]
Every sample goes through the FFT (no gaps), so even an 8 ms Iridium burst can't fall between frames.
Spectrum datagrams, FRAME_RATE a second: b'IRS2', centre Hz (float64), sample rate (float64), then
FFT_SIZE float32 dB values of the PEAK over the frame's FFTs, then FFT_SIZE of their AVERAGE (lowest
frequency first).

Sound (--audio-port): the raw radio as audio, like SDR++ - every burst is a click or "zzt" over the
static. Nothing is decoded into speech (Iridium voice is digital). Modes, set by the tracker:
  wide   the envelope of the whole received slice: every burst in it
  tuned  one ~36 kHz channel at an offset from the centre, AM-demodulated
Audio datagrams: b'IRAU' + 16-bit 48 kHz mono samples. Once a second a b'IRAH' hello is sent so the
tracker knows where to send b'CTRL <mode> <offset Hz>' commands back. Sending never blocks, so the
sound can never hold up the decoder.
"""
import argparse
import os
import runpy
import socket
import struct
import sys
import time

import numpy as np
import osmosdr
from gnuradio import blocks, fft as gr_fft, filter as gr_filter, gr
from gnuradio.fft import window
from gnuradio.filter import firdes

import iridium.iridium_extractor_flowgraph as fg

FFT_SIZE = 2048
FRAME_RATE = 15
EXTRACTOR = os.path.join(os.path.dirname(sys.executable), 'Library', 'bin', 'iridium-extractor.py')

ap = argparse.ArgumentParser()
ap.add_argument('conf')
ap.add_argument('threshold', type=float)
ap.add_argument('--spectrum-port', type=int, default=0)
ap.add_argument('--ppm', type=float, default=0.0)
ap.add_argument('--auto-gain', action='store_true')
ap.add_argument('--no-iq', action='store_true')
ap.add_argument('--no-dc', action='store_true')
ap.add_argument('--audio-port', type=int, default=0)
opts = ap.parse_args()

# keep hold of the SDR source the extractor creates
_captured = {}
_osmosdr_source = osmosdr.source


def _source(*a, **k):
    src = _osmosdr_source(*a, **k)
    _captured['src'] = src
    return src


osmosdr.source = _source


class UdpSpectrum(gr.sync_block):
    """collects the power spectra of all samples and sends their peak and average FRAME_RATE times a
    second; a closed or missing tracker never stops the decoder"""

    def __init__(self, port, center, rate, win):
        gr.sync_block.__init__(self, 'udp_spectrum', in_sig=[(np.float32, FFT_SIZE)], out_sig=None)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addr = ('127.0.0.1', port)
        self.head = struct.pack('<4sdd', b'IRS2', float(center), float(rate))
        self.per_frame = max(1, int(rate / FFT_SIZE / FRAME_RATE))
        self.norm = 10 * np.log10(np.sum(win) ** 2)     # a full-scale tone reads 0 dB
        self._clear()

    def _clear(self):
        self.peak = np.zeros(FFT_SIZE, np.float32)
        self.total = np.zeros(FFT_SIZE, np.float64)
        self.count = 0

    def work(self, input_items, output_items):
        x = input_items[0]
        i = 0
        while i < len(x):
            take = min(len(x) - i, self.per_frame - self.count)
            chunk = x[i:i + take]
            np.maximum(self.peak, chunk.max(axis=0), out=self.peak)
            self.total += chunk.sum(axis=0)
            self.count += take
            i += take
            if self.count >= self.per_frame:
                db = 10 * np.log10(np.stack([self.peak, self.total / self.count]) + 1e-20) - self.norm
                try:
                    self.sock.sendto(self.head + db.astype(np.float32).tobytes(), self.addr)
                except OSError:
                    pass
                self._clear()
        return len(x)


class UdpAudio(gr.sync_block):
    """normalises the audio, sends it to the tracker in 20 ms packets, and takes mode commands back"""

    def __init__(self, port, apply):
        gr.sync_block.__init__(self, 'udp_audio', in_sig=[np.float32], out_sig=None)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.addr = ('127.0.0.1', port)
        self.apply = apply
        self.mode, self.offset = 'off', 0.0
        self.rms = 1e-3
        self.pending = np.zeros(0, np.float32)
        self.hello = 0.0

    def _commands(self):
        while True:
            try:
                data, _ = self.sock.recvfrom(256)
            except (BlockingIOError, OSError):
                return
            f = data.split()
            if len(f) == 3 and f[0] == b'CTRL' and f[1] in (b'off', b'wide', b'tuned'):
                self.mode, self.offset = f[1].decode(), float(f[2])
                self.apply(self.mode, self.offset)

    def _send(self, data):
        try:
            self.sock.sendto(data, self.addr)
        except OSError:
            pass

    def work(self, input_items, output_items):
        x = input_items[0]
        self._commands()
        now = time.time()
        if now - self.hello > 1:
            self.hello = now
            self._send(b'IRAH ' + self.mode.encode())
        if self.mode != 'off' and len(x):
            r = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
            self.rms = 0.97 * self.rms + 0.03 * max(r, 1e-9)      # slow level: static stays steady, bursts pop
            y = np.clip(x / (4 * self.rms), -1, 1).astype(np.float32)
            self.pending = np.concatenate([self.pending, y])
            while len(self.pending) >= 960:
                self._send(b'IRAU' + (self.pending[:960] * 30000).astype('<i2').tobytes())
                self.pending = self.pending[960:]
        return len(x)


def _try(what, fn, *a):
    try:
        fn(*a)
        print(what, file=sys.stderr)
    except Exception as e:      # not every device supports every setting
        print('%s: not supported by this device (%s)' % (what.split(':')[0], e), file=sys.stderr)


_flowgraph_init = fg.FlowGraph.__init__


def _init(self, *a, **k):
    _flowgraph_init(self, *a, **k)
    src = _captured.get('src')
    if src is None:
        return
    if opts.ppm:
        _try('Frequency correction: %+.1f ppm' % opts.ppm, src.set_freq_corr, opts.ppm, 0)
    if opts.auto_gain:
        _try('Gain: automatic', src.set_gain_mode, True, 0)
    _try('IQ correction: %s' % ('on' if not opts.no_iq else 'off'), src.set_iq_balance_mode, 0 if opts.no_iq else 2, 0)
    _try('DC correction: %s' % ('on' if not opts.no_dc else 'off'), src.set_dc_offset_mode, 0 if opts.no_dc else 2, 0)
    if opts.spectrum_port:
        win = window.blackmanharris(FFT_SIZE)
        chain = [blocks.stream_to_vector(gr.sizeof_gr_complex, FFT_SIZE),
                 gr_fft.fft_vcc(FFT_SIZE, True, win, True),
                 blocks.complex_to_mag_squared(FFT_SIZE),
                 UdpSpectrum(opts.spectrum_port, self._center_frequency, self._input_sample_rate, win)]
        self.connect(src, *chain)
        self._spectrum_blocks = chain       # keep them alive
        print('Spectrum: %d-point FFT of every sample, peak + average %d times/s' % (FFT_SIZE, FRAME_RATE),
              file=sys.stderr)
    if opts.audio_port:
        rate = self._input_sample_rate
        decim = int(rate // 50000)          # every RATE is a multiple of 50 kHz
        mid = rate / decim
        # tuned: one channel, AM-demodulated
        xlate = gr_filter.freq_xlating_fir_filter_ccf(decim, firdes.low_pass(1, rate, 18e3, 8e3), 0, rate)
        tuned = [xlate, blocks.complex_to_mag(1), gr_filter.dc_blocker_ff(64, True), blocks.multiply_const_ff(0.0)]
        # wide: the envelope of the whole slice
        wide = [blocks.complex_to_mag(1), gr_filter.fir_filter_fff(decim, firdes.low_pass(1, rate, 20e3, 10e3)),
                gr_filter.dc_blocker_ff(64, True), blocks.multiply_const_ff(0.0)]
        mix = blocks.add_ff(1)
        resample = gr_filter.rational_resampler_fff(48, int(mid // 1000))      # -> 48 kHz

        def apply(mode, offset):
            tuned[-1].set_k(1.0 if mode == 'tuned' else 0.0)
            wide[-1].set_k(1.0 if mode == 'wide' else 0.0)
            if mode == 'tuned':
                xlate.set_center_freq(offset)
        sink = UdpAudio(opts.audio_port, apply)
        self.connect(src, *tuned)
        self.connect(tuned[-1], (mix, 0))
        self.connect(src, *wide)
        self.connect(wide[-1], (mix, 1))
        self.connect(mix, resample, sink)
        self._audio_blocks = (tuned, wide, mix, resample, sink)
        print('Sound: available (whole band or one tuned channel)', file=sys.stderr)


fg.FlowGraph.__init__ = _init

sys.argv = [EXTRACTOR, '-d', '%g' % opts.threshold, opts.conf]
runpy.run_path(EXTRACTOR, run_name='__main__')
