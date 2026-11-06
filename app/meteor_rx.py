"""Meteor-M weather-picture receiver (LRPT, 137.9 MHz): records the pass for SatDump, which turns it into pictures
after Stop (images.py). receiver.py starts it like the other bands and keeps its lines in the capture's .live file.

  python meteor_rx.py --device ARGS --rate R --center F --baseband PATH [--gain DB | --auto-gain] [--ppm X]
                      [--no-dc] [--start-file PATH] [--spectrum-port N] [--audio-port N] [--parent-pid N]

The SDR is tuned below the signal (systems.METEOR: its centre spike stays off it); the 137.9 MHz channel is
shifted to the middle, filtered and cut down to BASEBAND_RATE, and written as 16-bit IQ (SatDump's "s16"):
about 1 MB a second, ~1 GB for a whole pass - deleted once the pictures are made.
Lines:
  MSIG: t snr_db      every 2 s: the signal's strength over the noise beside it (0 = nothing there; a good
                      pass is several dB) - the only sign of it while receiving, the pictures come after
"""
import argparse
import datetime
import math
import os
import sys
import time

import numpy as np
import osmosdr
from gnuradio import blocks, fft, filter, gr
from gnuradio.fft import window

import sdr_taps

SIGNAL_HZ = 137.9e6
DECIM = 4                     # 1.024 MS/s -> 256 kS/s: room for the 108 kHz wide signal and its Doppler shift
SIG_HALF_HZ = 54e3            # 72k symbols/s with 0.5 roll-off: +-54 kHz
NOISE_HZ = (78e3, 108e3)      # the noise beside it, for the signal-to-noise reading
FFT_N = 1024

ap = argparse.ArgumentParser()
ap.add_argument('--device', default='rtl=0')
ap.add_argument('--rate', type=float, default=1024000)
ap.add_argument('--center', type=float, default=137.7e6)
ap.add_argument('--baseband', required=True)
ap.add_argument('--gain', type=float, default=40.0)
ap.add_argument('--auto-gain', action='store_true')
# E4000 tuner: its IF gain left at the driver's default leaves the samples at ~0.6% of the ADC's range (under
# one 8-bit step), losing weak signals in the rounding; 60 dB lifts them to ~10% (measured on GPS: 5 satellites
# at ~37 dB-Hz instead of 0-2 at ~33). Other tuners ignore it.
ap.add_argument('--if-gain', type=float, default=50.0)
ap.add_argument('--ppm', type=float, default=0.0)
ap.add_argument('--no-dc', action='store_true')
ap.add_argument('--start-file', default='')
ap.add_argument('--spectrum-port', type=int, default=0)
ap.add_argument('--audio-port', type=int, default=0)
ap.add_argument('--parent-pid', type=int, default=0)
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


class Receiver(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, 'meteor_rx')
        rate, center = opts.rate, opts.center
        self.out_rate = rate / DECIM
        src = osmosdr.source(args=opts.device)      # (tests: a 'file=...' recording instead of the SDR)
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
        taps = filter.firdes.low_pass(1.0, rate, 112e3, 16e3)
        chan = filter.freq_xlating_fir_filter_ccf(DECIM, taps, SIGNAL_HZ - center, rate)
        to_s16 = blocks.complex_to_interleaved_short(False, 16384.0)
        sink = blocks.file_sink(gr.sizeof_short, opts.baseband, False)
        sink.set_unbuffered(False)
        self.connect(src, chan, to_s16, sink)
        # the signal-to-noise reading: a spectrum of the channel a few times a second
        s2v = blocks.stream_to_vector(gr.sizeof_gr_complex, FFT_N)
        keep = blocks.keep_one_in_n(gr.sizeof_gr_complex * FFT_N, max(1, int(self.out_rate / FFT_N / 8)))
        ff = fft.fft_vcc(FFT_N, True, window.blackmanharris(FFT_N), True)
        mag = blocks.complex_to_mag_squared(FFT_N)
        avg = filter.single_pole_iir_filter_ff(0.2, FFT_N)
        self.probe = blocks.probe_signal_vf(FFT_N)
        self.connect(chan, s2v, keep, ff, mag, avg, self.probe)
        self.taps = []
        if opts.spectrum_port:
            self.taps.append(sdr_taps.add_spectrum(self, src, opts.spectrum_port, center, rate))
        if opts.audio_port:
            self.taps.append(sdr_taps.add_audio(self, src, opts.audio_port, rate, channel_hz=120e3, demod='fm'))

    def snr_db(self):
        p = np.asarray(self.probe.level(), float)
        if p.size != FFT_N or not p.any():
            return None
        f = (np.arange(FFT_N) - FFT_N // 2) * self.out_rate / FFT_N
        sig = p[(np.abs(f) < SIG_HALF_HZ * 0.8) & (np.abs(f) > 2e3)].mean()
        noise = p[(np.abs(f) > NOISE_HZ[0]) & (np.abs(f) < NOISE_HZ[1])].mean()
        return 10 * math.log10(max(sig / noise, 1e-3)) if noise > 0 else None


def main():
    tb = Receiver()
    tb.start()
    t0 = time.time()
    if opts.start_file:
        with open(opts.start_file, 'w') as f:
            f.write(datetime.datetime.fromtimestamp(t0, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] + 'Z\n')
    log('Receiving Meteor-M LRPT at %.3f MHz (SDR at %.3f MHz, %.3f MS/s) - recording %.0f kS/s for SatDump; '
        'the pictures are made after Stop' % (SIGNAL_HZ / 1e6, opts.center / 1e6, opts.rate / 1e6, tb.out_rate / 1e3))
    last_status = 0
    try:
        while sdr_taps.parent_alive(opts.parent_pid):
            time.sleep(2)
            snr = tb.snr_db()
            if snr is not None:
                emit('MSIG: %.3f %.1f' % (time.time(), snr))
            if time.time() - last_status >= 10:
                last_status = time.time()
                size = os.path.getsize(opts.baseband) if os.path.exists(opts.baseband) else 0
                log('%d | Meteor: %.0f MB recorded (%.0f s), signal %s' % (
                    time.time(), size / 2 ** 20, time.time() - t0,
                    '%+.1f dB over the noise' % snr if snr is not None else 'not measured yet'))
    except KeyboardInterrupt:
        pass
    tb.stop()
    tb.wait()


if __name__ == '__main__':
    main()
