"""Finds the IF gain for a band (Source > Tune gain): opens the SDR as that band's receiver would, steps the
tuner's IF gain and measures how much of the 8-bit converter the samples use, and picks the IF gain that fills
it most without clipping. (Not simply the highest: around 137 MHz, with the main gain at 40+, an E4000 gave a
lower level at IF 60 than at IF 50.)

  python gain_tune.py --device ARGS --rate R --center F --gain DB [--auto-gain] [--ppm X]

Why: at the driver's default IF gain an E4000's samples sat at ~0.6% of full scale - under one 8-bit step - and
weak signals were lost in the rounding (GPS: 0-2 satellites at ~33 dB-Hz; at IF 60 dB, 5 at ~37). Above ~10% of
full scale it stopped mattering, so the goal is: as high as possible while strong signals in the band don't clip.
Prints one line per step, then the result:
  STEP if_db rms_pct clip_pct
  BEST if_db rms_pct
"""
import argparse
import sys
import threading
import time

import numpy as np
import osmosdr
from gnuradio import gr

STEPS = (10, 20, 30, 40, 50, 60)
CLIP_LIMIT = 0.0001         # fraction of samples at full scale allowed (a rare spike is fine)
RMS_LIMIT = 0.30            # beyond this the headroom for strong bursts is gone

ap = argparse.ArgumentParser()
ap.add_argument('--device', default='rtl=0')
ap.add_argument('--rate', type=float, default=2048000)
ap.add_argument('--center', type=float, required=True)
ap.add_argument('--gain', type=float, default=40.0)
ap.add_argument('--auto-gain', action='store_true')
ap.add_argument('--ppm', type=float, default=0.0)
opts = ap.parse_args()


class Grab(gr.sync_block):
    """keeps the last n samples on request"""

    def __init__(self):
        gr.sync_block.__init__(self, 'grab', in_sig=[np.complex64], out_sig=None)
        self.want, self.buf, self.done = 0, [], threading.Event()

    def take(self, n):
        self.buf, self.got = [], 0
        self.done.clear()
        self.want = n
        self.done.wait(10)
        return np.concatenate(self.buf) if self.buf else np.zeros(0, np.complex64)

    def work(self, input_items, output_items):
        x = input_items[0]
        if self.want:
            self.buf.append(x.copy())
            self.got += len(x)
            if self.got >= self.want:
                self.want = 0
                self.done.set()
        return len(x)


def main():
    tb = gr.top_block()
    src = osmosdr.source(args=opts.device)
    src.set_sample_rate(opts.rate)
    src.set_center_freq(opts.center, 0)
    if opts.ppm:
        src.set_freq_corr(opts.ppm, 0)
    src.set_gain_mode(bool(opts.auto_gain), 0)
    if not opts.auto_gain:
        src.set_gain(opts.gain, 0)
    try:
        src.set_dc_offset_mode(2, 0)
    except Exception:
        pass
    grab = Grab()
    tb.connect(src, grab)
    tb.start()
    time.sleep(0.5)
    rows = []
    for ifg in STEPS:
        try:
            src.set_if_gain(ifg, 0)
        except Exception as e:
            print('ERROR this device has no IF gain (%s)' % e, flush=True)
            break
        time.sleep(0.3)                     # (settle; the DC correction catches up)
        grab.take(int(opts.rate * 0.1))
        x = grab.take(int(opts.rate * 0.5))
        if not len(x):
            continue
        rms = float(np.sqrt(np.mean(np.abs(x) ** 2) / 2))
        clip = float(np.mean((np.abs(x.real) > 0.99) | (np.abs(x.imag) > 0.99)))
        rows.append((ifg, rms, clip))
        print('STEP %d %.2f %.4f' % (ifg, rms * 100, clip * 100), flush=True)
    tb.stop()
    tb.wait()
    if not rows:
        print('ERROR no samples', flush=True)
        return 1
    ok = [r for r in rows if r[2] <= CLIP_LIMIT and r[1] <= RMS_LIMIT]
    best = max(ok, key=lambda r: r[1]) if ok else min(rows, key=lambda r: r[0])
    print('BEST %d %.2f' % (best[0], best[1] * 100), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
