r"""Runs gr-iridium's iridium-extractor with the extras its command line doesn't offer:
frequency correction (PPM), automatic gain, IQ / DC correction, and a live spectrum (FFT) of the
same samples the decoder sees, sent to the tracker over a local UDP socket.

Usage (receiver.py starts it):
  python sdr_source.py CONF THRESHOLD_DB [--spectrum-port N] [--ppm X] [--auto-gain] [--no-iq] [--no-dc]
Every sample goes through the FFT (no gaps), so even an 8 ms Iridium burst can't fall between frames.
Spectrum datagrams, FRAME_RATE a second: b'IRS2', centre Hz (float64), sample rate (float64), then
FFT_SIZE float32 dB values of the PEAK over the frame's FFTs, then FFT_SIZE of their AVERAGE (lowest
frequency first).
"""
import argparse
import os
import runpy
import socket
import struct
import sys

import numpy as np
import osmosdr
from gnuradio import blocks, fft as gr_fft, gr
from gnuradio.fft import window

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


fg.FlowGraph.__init__ = _init

sys.argv = [EXTRACTOR, '-d', '%g' % opts.threshold, opts.conf]
runpy.run_path(EXTRACTOR, run_name='__main__')
