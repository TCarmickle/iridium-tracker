r"""Runs gr-iridium's iridium-extractor with the extras its command line doesn't offer:
frequency correction (PPM), automatic gain, IQ / DC correction, and a live spectrum (FFT) of the
same samples the decoder sees, sent to the tracker over a local UDP socket.

Usage (receiver.py starts it):
  python sdr_source.py CONF THRESHOLD_DB [--spectrum-port N] [--ppm X] [--auto-gain] [--no-iq] [--no-dc]
Spectrum datagrams: b'IRSP', centre Hz (float64), sample rate (float64), then FFT_SIZE float32 dB values
(lowest frequency first).
"""
import argparse
import os
import runpy
import socket
import struct
import sys

import numpy as np
import osmosdr
from gnuradio import gr
from gnuradio.fft import logpwrfft

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
    """sends each FFT frame to the tracker; a closed or missing tracker never stops the decoder"""

    def __init__(self, port, center, rate):
        gr.sync_block.__init__(self, 'udp_spectrum', in_sig=[(np.float32, FFT_SIZE)], out_sig=None)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addr = ('127.0.0.1', port)
        self.head = struct.pack('<4sdd', b'IRSP', float(center), float(rate))

    def work(self, input_items, output_items):
        for v in input_items[0]:
            try:
                self.sock.sendto(self.head + np.asarray(v, np.float32).tobytes(), self.addr)
            except OSError:
                pass
        return len(input_items[0])


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
        fft = logpwrfft.logpwrfft_c(sample_rate=self._input_sample_rate, fft_size=FFT_SIZE, ref_scale=2.0,
                                     frame_rate=FRAME_RATE, avg_alpha=1.0, average=False, shift=True)
        sink = UdpSpectrum(opts.spectrum_port, self._center_frequency, self._input_sample_rate)
        self.connect(src, fft, sink)
        self._spectrum_blocks = (fft, sink)     # keep them alive
        print('Spectrum: %d-point FFT, %d frames/s' % (FFT_SIZE, FRAME_RATE), file=sys.stderr)


fg.FlowGraph.__init__ = _init

sys.argv = [EXTRACTOR, '-d', '%g' % opts.threshold, opts.conf]
runpy.run_path(EXTRACTOR, run_name='__main__')
