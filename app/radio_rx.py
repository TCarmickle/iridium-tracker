"""Radio receiver: no decoding - the spectrum, waterfall and sound of wherever the Radio section is tuned, like
SDR++. receiver.py starts it like the other bands; the tracker retunes it while it runs (a TUNE command on the
sound's connection, sdr_taps.UdpAudio), so the frequency can go anywhere the SDR reaches.

  python radio_rx.py --device ARGS --rate R --center F [--gain DB | --auto-gain] [--ppm X] [--no-dc]
                     [--start-file PATH] [--spectrum-port N] [--audio-port N] [--parent-pid N]
"""
import argparse
import datetime
import sys
import time

import osmosdr
from gnuradio import gr

import sdr_taps

ap = argparse.ArgumentParser()
ap.add_argument('--device', default='rtl=0')
ap.add_argument('--rate', type=float, default=2048000)
ap.add_argument('--center', type=float, default=100.25e6)
ap.add_argument('--gain', type=float, default=30.0)
ap.add_argument('--auto-gain', action='store_true')
ap.add_argument('--if-gain', type=float, default=60.0)   # E4000 IF gain (sdr_settings.if_gain; others ignore it)
ap.add_argument('--ppm', type=float, default=0.0)
ap.add_argument('--no-dc', action='store_true')
ap.add_argument('--start-file', default='')
ap.add_argument('--spectrum-port', type=int, default=0)
ap.add_argument('--audio-port', type=int, default=0)
ap.add_argument('--parent-pid', type=int, default=0)
opts = ap.parse_args()


def log(text):
    print(text, file=sys.stderr, flush=True)


def _try(what, fn, *a):
    try:
        fn(*a)
        log(what)
    except Exception as e:      # not every device supports every setting
        log('%s: not supported by this device (%s)' % (what.split(':')[0], e))


class Receiver(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, 'radio_rx')
        self.src = src = osmosdr.source(args=opts.device)
        src.set_sample_rate(opts.rate)
        src.set_center_freq(opts.center, 0)
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
        self.spectrum = None
        self.taps = []
        if opts.spectrum_port:
            self.taps.append(sdr_taps.add_spectrum(self, src, opts.spectrum_port, opts.center, opts.rate))
            self.spectrum = self.taps[-1][-1]
        if opts.audio_port:
            self.taps.append(sdr_taps.add_audio(self, src, opts.audio_port, opts.rate, channel_hz=180e3, demod='wfm',
                                                on_tune=self.tune))

    def tune(self, center):
        """the Radio section moved the SDR (its frequency left the slice)"""
        center = min(max(center, 24e6), 2300e6)
        self.src.set_center_freq(center, 0)
        if self.spectrum is not None:
            self.spectrum.set_center(center)
        log('%d | Radio: tuned to %.4f MHz' % (time.time(), center / 1e6))


def main():
    tb = Receiver()
    tb.start()
    if opts.start_file:
        with open(opts.start_file, 'w') as f:
            f.write(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] + 'Z\n')
    log('Radio: listening around %.4f MHz, %.3f MS/s - tune in the Radio section' % (opts.center / 1e6, opts.rate / 1e6))
    try:
        while sdr_taps.parent_alive(opts.parent_pid):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    tb.stop()
    tb.wait()


if __name__ == '__main__':
    main()
