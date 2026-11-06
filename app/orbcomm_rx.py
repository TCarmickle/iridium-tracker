"""Orbcomm receiver: tunes the SDR to the Orbcomm downlink (137.2-137.8 MHz), decodes every channel at
once and prints one line per packet. receiver.py starts it and saves the lines to the capture's .live file.

  python orbcomm_rx.py --device ARGS --rate 1228800 --center 137500000 [--gain DB | --auto-gain]
                       [--ppm X] [--no-dc] [--start-file PATH] [--spectrum-port N] [--audio-port N]

Each downlink channel is cut out of the received slice (38.4 kS/s, 8 samples per symbol) and fed to its own
decoder (orbcomm/, which finds the carrier and its Doppler shift by itself - no orbit data needed). Lines:
  OEP: t Hz satid snr x y z vx vy vz   ephemeris: the satellite's own position (km) and velocity (km/s)
  OSY: t Hz satid snr                  sync packet
  OPK: t Hz satid snr type             any other good packet (satid: the satellite last heard on that channel)
  OBAD: t Hz - snr                     a packet that failed its checksum
t is unix time, Hz the measured carrier (channel + Doppler + the SDR's own error), snr the carrier's dB.
The spectrum and sound taps are the same as Iridium's (sdr_taps.py).
"""
import argparse
import datetime
import queue
import sys
import threading
import time

import numpy as np
import osmosdr
from gnuradio import blocks, fft, filter as gr_filter, gr
from gnuradio.fft import window
from gnuradio.filter import firdes

import sdr_taps
from orbcomm import OrbcommDecoder, all_frequencies

CHANNEL_RATE = 38400        # each channel's samples: 8 per 4800-baud symbol
CHUNK_S = 0.1               # decode this much of a channel at a time
SAT_HOLD_S = 30             # a packet without a satellite ID belongs to the one heard on its channel this recently

ap = argparse.ArgumentParser()
ap.add_argument('--device', default='rtl=0')
ap.add_argument('--rate', type=float, default=1228800)
ap.add_argument('--center', type=float, default=137.5e6)
ap.add_argument('--gain', type=float, default=35.0)
ap.add_argument('--auto-gain', action='store_true')
ap.add_argument('--if-gain', type=float, default=60.0)   # E4000 IF gain (sdr_settings.if_gain; others ignore it)
ap.add_argument('--ppm', type=float, default=0.0)
ap.add_argument('--no-dc', action='store_true')
ap.add_argument('--start-file', default='')
ap.add_argument('--spectrum-port', type=int, default=0)
ap.add_argument('--audio-port', type=int, default=0)
ap.add_argument('--parent-pid', type=int, default=0)   # stop when the tracker that started us is gone
opts = ap.parse_args()


def log(text):
    print(text, file=sys.stderr, flush=True)


def _try(what, fn, *a):
    try:
        fn(*a)
        log(what)
    except Exception as e:      # not every device supports every setting
        log('%s: not supported by this device (%s)' % (what.split(':')[0], e))


class BandScan(gr.sync_block):
    """keeps a running average of the whole slice's spectrum, so the status line can name the strongest
    narrow signals anywhere in it - including any satellite that isn't on a channel we decode"""
    N = 8192

    def __init__(self, rate, center):
        gr.sync_block.__init__(self, 'orbcomm_scan', in_sig=[(np.float32, self.N)], out_sig=None)
        self.rate, self.center = rate, center
        self.avg = None
        self.lock = threading.Lock()

    def work(self, input_items, output_items):
        x = np.asarray(input_items[0], np.float64).mean(axis=0)
        with self.lock:
            self.avg = x if self.avg is None else 0.97 * self.avg + 0.03 * x
        return len(input_items[0])

    def peaks(self, k=5):
        """[(MHz, dB over the noise)] of the k strongest narrow signals between 137.0 and 138.0 MHz"""
        with self.lock:
            if self.avg is None:
                return []
            p = np.fft.fftshift(self.avg)
        f = self.center + (np.arange(self.N) - self.N / 2) * self.rate / self.N
        sel = (f > 137.0e6) & (f < 138.0e6) & (np.abs(f - self.center) > 3e3)      # (not the centre spike)
        noise = np.median(p[sel])
        db = 10 * np.log10(p / noise)
        out = []
        for i in np.argsort(np.where(sel, db, -99))[::-1]:
            if db[i] < 3 or len(out) >= k:
                break
            if all(abs(f[i] - g) > 5e3 for g, _ in out):
                out.append((f[i], db[i]))
        return [(g / 1e6, d) for g, d in out]


class ChannelSink(gr.sync_block):
    """hands a channel's samples, with the time of their first sample, to the decoding thread"""

    def __init__(self, freq, out):
        gr.sync_block.__init__(self, 'orbcomm_channel', in_sig=[np.complex64], out_sig=None)
        self.freq, self.out = freq, out
        self.buf = []
        self.n = 0
        self.t_first = None

    def work(self, input_items, output_items):
        x = input_items[0]
        if self.t_first is None:
            self.t_first = time.time() - len(x) / CHANNEL_RATE
            started(self.t_first)
        self.buf.append(x.copy())
        self.n += len(x)
        if self.n >= CHANNEL_RATE * CHUNK_S:
            chunk = np.concatenate(self.buf)
            self.out.put((self.freq, self.t_first, chunk))
            self.t_first += len(chunk) / CHANNEL_RATE
            self.buf, self.n = [], 0
        return len(x)


_started = threading.Event()


def started(t):
    """the first samples arrived: note when (the capture's clock start)"""
    if _started.is_set():
        return
    _started.set()
    if opts.start_file:
        with open(opts.start_file, 'w') as f:
            f.write(datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] + 'Z\n')


def emit(line):
    sys.stdout.write(line + '\n')
    sys.stdout.flush()


SCAN = []


def decode_loop(chunks):
    decoders = {f: OrbcommDecoder(f, center_freq=f, sample_rate=CHANNEL_RATE) for f in all_frequencies()}
    last_sat = {}           # channel -> (sat id, time)
    bad_seen = {f: 0 for f in decoders}
    power = {f: None for f in decoders}     # each channel's average power, for the status line
    good = 0
    status = time.time()
    while True:
        f, t, chunk = chunks.get()
        dec = decoders[f]
        pw = float(np.mean(np.abs(chunk) ** 2))
        power[f] = pw if power[f] is None else 0.9 * power[f] + 0.1 * pw
        for pk in dec.decode_samples_to_packets(chunk, timestamp=t):
            hz = pk['channel_hz'] + pk['carrier_offset_hz']
            when = pk.get('time') or t
            snr = dec.carrier_snr_db
            kind = pk['packet_type']
            if pk.get('sat_id'):
                last_sat[f] = (pk['sat_id'], when)
            good += 1
            if kind == 'Ephemeris' and pk.get('pos_ecef_km') is not None:
                x, y, z = pk['pos_ecef_km']
                vx, vy, vz = pk['vel_ecef_kms']
                emit('OEP: %.3f %.0f %s %.1f %.3f %.3f %.3f %.4f %.4f %.4f'
                     % (pk.get('unix_time') or when, hz, pk['sat_id'], snr, x, y, z, vx, vy, vz))
            elif kind == 'Sync':
                emit('OSY: %.3f %.0f %s %.1f' % (when, hz, pk['sat_id'], snr))
            else:
                sid, seen = last_sat.get(f, (None, 0))
                emit('OPK: %.3f %.0f %s %.1f %s' % (when, hz, sid if sid and when - seen < SAT_HOLD_S else '-', snr,
                                                    kind.replace(' ', '_')))
        while dec.bad_packets > bad_seen[f]:
            bad_seen[f] += 1
            emit('OBAD: %.3f %.0f - %.1f' % (t, f + dec.carrier_offset, dec.carrier_snr_db))
        if time.time() - status > 10:
            status = time.time()
            locked = [ch for ch, d in decoders.items() if d.locked]
            log('%d | channels locked: %s | good packets: %d | backlog %.1f s'
                % (status, ', '.join('%.4f' % (ch / 1e6) for ch in locked) or 'none', good,
                   chunks.qsize() * CHUNK_S / len(decoders)))
            # each channel: power over the quietest channel, and the decoder's carrier estimate (SNR, offset) -
            # shows whether a satellite is there at all when nothing locks
            quiet = min(p for p in power.values() if p) if any(power.values()) else None
            if quiet:
                log('    ' + '  '.join('%.4f: %+.1f dB%s' % (ch / 1e6, 10 * np.log10(power[ch] / quiet) if power[ch] else 0,
                                                            ' (carrier %.0f dB at %+.0f Hz)' % (d.carrier_snr_db, d.carrier_offset)
                                                            if d.carrier_snr_db is not None and d.carrier_offset is not None else '')
                                      for ch, d in decoders.items()))
            if SCAN:
                pk = SCAN[0].peaks()
                log('    strongest narrow signals 137-138 MHz: %s' % ('  '.join('%.4f MHz %+.0f dB' % x for x in pk)
                                                                       or 'none above the noise'))


class Receiver(gr.top_block):
    def __init__(self, chunks):
        gr.top_block.__init__(self, 'orbcomm_rx')
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
        self.src = src
        decim = int(round(rate / CHANNEL_RATE))
        taps = firdes.low_pass(1, rate, 9e3, 4e3)         # +-4.8 kHz of signal plus up to +-3.5 kHz of Doppler
        self.channels = []
        for f in all_frequencies():
            xlate = gr_filter.freq_xlating_fir_filter_ccf(decim, taps, f - center, rate)
            sink = ChannelSink(f, chunks)
            self.connect(src, xlate, sink)
            self.channels.append((xlate, sink))
        log('Orbcomm: %d downlink channels (%s MHz) at %.1f kS/s each'
            % (len(self.channels), ', '.join('%.4f' % (f / 1e6) for f in all_frequencies()), CHANNEL_RATE / 1e3))
        self.taps = []
        scan = BandScan(rate, center)
        self.taps.append([blocks.stream_to_vector(gr.sizeof_gr_complex, BandScan.N),
                          blocks.keep_one_in_n(gr.sizeof_gr_complex * BandScan.N, 16),     # ~9 FFTs a second is plenty
                          fft.fft_vcc(BandScan.N, True, window.blackmanharris(BandScan.N), False),
                          blocks.complex_to_mag_squared(BandScan.N), scan])
        self.connect(src, *self.taps[-1])
        SCAN.append(scan)
        if opts.spectrum_port:
            self.taps.append(sdr_taps.add_spectrum(self, src, opts.spectrum_port, center, rate))
        if opts.audio_port:
            self.taps.append(sdr_taps.add_audio(self, src, opts.audio_port, rate, channel_hz=10e3, demod='fm'))


def main():
    if abs(opts.rate / CHANNEL_RATE - round(opts.rate / CHANNEL_RATE)) > 1e-6:
        sys.exit('The sample rate must be a multiple of %d S/s (e.g. 1228800)' % CHANNEL_RATE)
    chunks = queue.Queue()
    def run():
        try:
            decode_loop(chunks)
        except Exception:
            import traceback
            log('Orbcomm decoder stopped: ' + traceback.format_exc())
            import os
            os._exit(1)         # the receiver sees the process end and reports the reason
    threading.Thread(target=run, daemon=True).start()
    tb = Receiver(chunks)
    tb.start()
    log('Receiving Orbcomm at %.4f MHz, %.4f MS/s' % (opts.center / 1e6, opts.rate / 1e6))
    try:
        while sdr_taps.parent_alive(opts.parent_pid):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    tb.stop()
    tb.wait()


if __name__ == '__main__':
    main()
