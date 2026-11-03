r"""Runs gr-iridium's iridium-extractor with the extras its command line doesn't offer:
frequency correction (PPM), automatic gain, IQ / DC correction, and a live spectrum (FFT) of the
same samples the decoder sees, sent to the tracker over a local UDP socket.

Usage (receiver.py starts it):
  python sdr_source.py CONF THRESHOLD_DB [--spectrum-port N] [--audio-port N] [--ppm X] [--auto-gain] [--no-iq] [--no-dc]
The spectrum and sound taps (sdr_taps.py) are shared with the other systems' receivers. Sound: every
burst is a click or "zzt" over the static; nothing is decoded into speech (Iridium voice is digital).
"""
import argparse
import os
import runpy
import sys

import osmosdr

import iridium.iridium_extractor_flowgraph as fg
import sdr_taps

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
        self._spectrum_blocks = sdr_taps.add_spectrum(self, src, opts.spectrum_port, self._center_frequency,
                                                      self._input_sample_rate)
    if opts.audio_port:
        self._audio_blocks = sdr_taps.add_audio(self, src, opts.audio_port, self._input_sample_rate)


fg.FlowGraph.__init__ = _init

sys.argv = [EXTRACTOR, '-d', '%g' % opts.threshold, opts.conf]
runpy.run_path(EXTRACTOR, run_name='__main__')
