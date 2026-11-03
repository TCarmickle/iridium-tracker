"""Streaming Orbcomm downlink decoder (SDPSK, 4800 bit/s) for one channel.

Vendored from fbieberly/ORBCOMM-receiver realtime_decoder.py (MIT, see LICENSE.txt).
Changes from the original:
  - decode_samples_to_packets returns the packets (it never appended to its list);
    no printing; Ephemeris packets carry position, velocity, GPS time and lat/lon/alt.
  - The first-call carrier estimate runs (frequency_offset started at 0.0 but was
    tested with == None) and searches +-4 kHz at a higher sample rate (the 4th-power
    line sits at 4x the offset), so the decoder tunes itself without a TLE Doppler.
    The 4th-power line repeats every 1200 Hz (baud/4); the plain spectrum picks which.
    Whatever the Costas loop tracks is folded back into the mixer every chunk, so a
    whole pass of Doppler (about +-3.3 kHz) stays inside the channel filter, and the
    measured carrier offset is reported with every packet.
  - Empty and short chunks no longer crash; samples are buffered until there is
    enough for the carrier estimate.
  - Channel filter and matched filter are vectorised, the channel filter is sized for
    the input rate (the 181-tap one let neighbouring channels alias in at 1.2288 MHz).
  - Timing recovery wraps its fractional delay by whole samples (it ran off over a
    long pass; tested to +-1000 ppm clock error) and symbols are picked by a global
    sample parity (per-chunk slicing lost the symbol phase whenever a chunk had an
    odd length), chosen by how close the steps are to +-90 degrees (the original
    took the lower-energy parity, which was the wrong one on the sample recordings).
numpy + scipy only.
"""
import math

import numpy as np
from scipy.signal import firwin, welch

from .helpers import complex_mix, rrcosfilter
from .packets import packet_dict, parse_packet

BAUD_RATE = 4800.0
SAMPLES_PER_SYMBOL = 2
PACKET_BITS = 12 * 8
EPHEMERIS_BITS = 24 * 8


def _fir_design(numtaps, cutoff, fs, dec):
    """Low-pass taps zero-padded to a multiple of dec, reshaped (m, dec) for _fir_decimate."""
    taps = firwin(numtaps, cutoff, fs=fs)
    m = -(-len(taps) // dec)
    taps = np.concatenate([taps, np.zeros(m * dec - len(taps))])
    return taps.reshape(m, dec).T.astype(np.complex64), (numtaps - 1) // 2


def _fir_decimate(buf, taps_mat, dec):
    """Output k = dot(taps, buf[k*dec : k*dec + m*dec]). Returns (outputs, unused tail)."""
    m = taps_mat.shape[1]
    rows = len(buf) // dec
    n_out = rows - m + 1
    if n_out <= 0:
        return np.zeros(0, np.complex64), buf
    prod = buf[:rows * dec].reshape(rows, dec) @ taps_mat   # (rows, m)
    out = np.zeros(n_out, np.complex64)
    for p in range(m):
        out += prod[p:p + n_out, p]
    return out, buf[n_out * dec:]


class OrbcommDecoder:
    """Decode one Orbcomm downlink channel from consecutive complex64 chunks.

    sat_freq     nominal downlink frequency (Hz) of the channel
    center_freq  frequency (Hz) the samples are centred on
    sample_rate  input rate (Hz); best an integer multiple of 9600
    """

    def __init__(self, sat_freq, center_freq=137.5e6, sample_rate=1.2288e6,
                 search_hz=4200.0, est_seconds=0.25):
        self.sample_rate = float(sample_rate)
        self.center_frequency = float(center_freq)
        self.sat_center_frequency = float(sat_freq)
        self.search_hz = search_hz
        self.est_seconds = est_seconds

        self.decimation = max(1, int(round(self.sample_rate / (SAMPLES_PER_SYMBOL * BAUD_RATE))))
        self.decimated_rate = self.sample_rate / self.decimation

        # Channel filter: passes the 4800 baud RRC signal (+-3.4 kHz), rejects
        # the neighbouring 12.5 kHz channels before decimating to 2 samples/symbol
        numtaps = max(181, int(3.3 * self.sample_rate / 1600.0) | 1)
        self.lpf_taps, self.lpf_delay = _fir_design(numtaps, 4200.0, self.sample_rate, self.decimation)

        # Carrier search: a wider filter and a rate that holds 4 x search_hz
        self.est_decimation = max(1, int(self.sample_rate // 76800))
        est_numtaps = max(63, int(3.3 * self.sample_rate / 3000.0) | 1)
        self.est_taps, _ = _fir_design(est_numtaps, search_hz + 3600.0, self.sample_rate, self.est_decimation)

        _, rrc = rrcosfilter(SAMPLES_PER_SYMBOL * 8 * 2 + 1, 0.4, 1., SAMPLES_PER_SYMBOL)
        self.rrc_taps = rrc.astype(np.complex64)

        self.packet_headers = [packet_dict[t]['header'] for t in packet_dict]
        self.good_packets = 0
        self.bad_packets = 0
        self.resets = 0
        self.t0 = None
        self.init_default_values()

    def init_default_values(self):
        """Forget the carrier, timing and bit alignment (e.g. for a new pass)."""
        self.doppler = 0.0
        self.residual_offset = None     # carrier offset not explained by doppler, Hz
        self.carrier_snr_db = None
        self.pending = np.zeros(0, np.complex64)
        self.mix_phase = 0.0
        self.samples_in = 0             # input samples through the chain since reset

        self.bit_offset = -1
        self.bit_string = ''
        self.bits_dropped = 0           # bits removed from the front of bit_string
        self.reverse = False
        self.symbol_offset = -1

        # Timing recovery
        self.tr_tau = 0.
        self.tr_dtau = 0.
        self.tr_buf = [0j] * 5
        self.tr_w = [float(v) for v in np.sqrt(np.hamming(5))]
        self.tr_alpha = 0.0005
        self.tr_beta = 2 * math.sqrt(self.tr_alpha)
        self.tr_counter = 0
        self.tr_dz = [0j, 0j, 0j]
        self.tr_last_sample = 0j
        self.out_count = 0              # timing-recovery outputs since reset
        self.tr_hold = False
        self.last_symbol = 0j

        # Costas loop
        self.cl_alpha = 0.05
        self.cl_beta = 0.2 * self.cl_alpha ** 2
        self.cl_frequency_out = 0.0
        self.cl_phase_est = 0.0

        self.lpf_remaining_samples = np.zeros(0, np.complex64)
        self.rrc_remaining_samples = np.zeros(0, np.complex64)
        self.sat_id = ''

    clean = init_default_values

    @property
    def carrier_offset(self):
        """Measured carrier minus the nominal channel frequency, Hz (Doppler + SDR error)."""
        if self.residual_offset is None:
            return None
        return self.doppler + self.residual_offset

    @property
    def locked(self):
        return self.bit_offset != -1

    def _estimate_carrier(self, samples):
        x = samples[:int(1.0 * self.sample_rate)]
        x = x / max(np.median(np.abs(x)), 1e-12)
        shift = self.center_frequency - self.sat_center_frequency - self.doppler
        mixed, _ = complex_mix(x, shift, self.sample_rate)
        dec, _ = _fir_decimate(mixed, self.est_taps, self.est_decimation)
        fs = self.sample_rate / self.est_decimation
        n = len(dec)
        f, pxx = welch(dec.astype(np.complex128) ** 4, fs=fs, return_onesided=False,
                       nperseg=n, scaling='density')
        f = np.fft.fftshift(f)
        pxx = np.fft.fftshift(pxx)
        lim = min(4 * self.search_hz, 0.95 * fs / 2)
        sel = np.abs(f) <= lim
        k = np.argmax(pxx[sel])
        fine = float(f[sel][k] / 4.0)
        self.carrier_snr_db = float(10 * np.log10(pxx[sel][k] / max(np.median(pxx[sel]), 1e-30)))

        # The 4th-power line repeats every baud/4 (1200 Hz): a carrier off by 1200 Hz
        # turns the +-90 degree steps into 0/180. Pick the candidate whose
        # raised-cosine spectrum best matches the plain power spectrum.
        f, pxx = welch(dec, fs=fs, return_onesided=False, nperseg=min(n, 2048))
        step = BAUD_RATE / 4
        cands = fine + step * np.arange(-int(self.search_hz // step) - 1, int(self.search_hz // step) + 2)
        cands = cands[np.abs(cands) <= self.search_hz]
        lo, hi = 0.6 * BAUD_RATE / 2, 1.4 * BAUD_RATE / 2
        scores = []
        for c in cands:
            a = np.abs(f - c)
            tmpl = np.where(a <= lo, 1.0, np.where(a >= hi, 0.0, 0.5 * (1 + np.cos(np.pi * (a - lo) / (hi - lo)))))
            scores.append(np.sum(tmpl * pxx))
        self.residual_offset = float(cands[int(np.argmax(scores))]) if len(cands) else fine

    def decode_samples_to_packets(self, samples, doppler=0.0, timestamp=None):
        """Feed the next chunk. doppler: optional predicted Doppler (Hz) to remove;
        timestamp: optional unix time of the chunk's first sample.
        Returns a list of good (checksum-passing) packet dicts, see parse_packet()."""
        samples = np.asarray(samples, dtype=np.complex64).ravel()
        if timestamp is not None:
            self.t0 = timestamp - (self.samples_in + len(self.pending)) / self.sample_rate
        self.doppler = doppler

        if self.residual_offset is None:
            self.pending = np.concatenate([self.pending, samples])
            if len(self.pending) < self.est_seconds * self.sample_rate:
                return []
            samples, self.pending = self.pending, np.zeros(0, np.complex64)
            self._estimate_carrier(samples)
        if len(samples) == 0:
            return []

        freq_shift = self.center_frequency - self.sat_center_frequency - self.doppler - self.residual_offset
        norm = samples / max(np.median(np.abs(samples)), 1e-12)
        mixed, self.mix_phase = complex_mix(norm, freq_shift, self.sample_rate, self.mix_phase)
        self.samples_in += len(samples)

        buf = np.concatenate([self.lpf_remaining_samples, mixed])
        decimated, self.lpf_remaining_samples = _fir_decimate(buf, self.lpf_taps, self.decimation)

        buf = np.concatenate([self.rrc_remaining_samples, decimated])
        ntaps = len(self.rrc_taps)
        n_mf = len(buf) - ntaps
        if n_mf <= 0:
            self.rrc_remaining_samples = buf
            return []
        win = np.lib.stride_tricks.sliding_window_view(buf, ntaps)[:n_mf]
        matched = win @ self.rrc_taps
        self.rrc_remaining_samples = buf[n_mf:]

        out, par = self._timing_and_costas(matched.tolist())

        out = np.asarray(out, np.complex64)
        par = np.asarray(par)
        if self.symbol_offset == -1:
            # The symbol samples are the ones whose steps sit closest to +-90 degrees
            q = [-np.mean(np.cos(2 * np.diff(np.angle(out[par == k])))) for k in (0, 1)]
            self.symbol_offset = int(np.argmax(q))
        symbols = out[par == self.symbol_offset]

        # Fold the frequency the Costas loop tracks back into the mixer
        self.residual_offset += self.cl_frequency_out * self.decimated_rate / (2 * np.pi)
        self.cl_frequency_out = 0.0

        if len(symbols) == 0:
            return []
        ang = np.angle(np.concatenate([[self.last_symbol], symbols]), deg=True)
        d = np.diff(ang)
        d = (d + 180.0) % 360.0 - 180.0
        self.last_symbol = complex(symbols[-1])
        self.bit_string += ''.join('1' if v > 0 else '0' for v in d)

        return self._extract_packets()

    def _timing_and_costas(self, matched):
        b0, b1, b2, b3, b4 = self.tr_buf
        dz0, dz1, dz2 = self.tr_dz
        tau, dtau = self.tr_tau, self.tr_dtau
        alpha, beta = self.tr_alpha, self.tr_beta
        w0, w1, w2, w3, w4 = self.tr_w
        counter = self.tr_counter
        last = self.tr_last_sample
        cnt = self.out_count
        hold = self.tr_hold
        cl_a, cl_b = self.cl_alpha, self.cl_beta
        cl_f, cl_p = self.cl_frequency_out, self.cl_phase_est
        sps = SAMPLES_PER_SYMBOL
        pi = math.pi
        out = []
        par = []
        for x in matched:
            b0, b1, b2, b3, b4 = b1, b2, b3, b4, x
            if hold:
                # tau was wrapped up a sample: this input's output would repeat the last one
                hold = False
                continue
            emit = 1
            while emit:
                emit -= 1
                # 5-tap windowed-sinc interpolator, y = x(t - 2 - tau)
                s = math.sin(pi * tau)
                if abs(tau) < 1e-9:
                    y = b2
                else:
                    # sinc(n - tau) for n = -2..2 is (-1)^(n+1) * s / (pi (n - tau))
                    h_m2 = -s / (pi * (-2 - tau)) * w0
                    h_m1 = s / (pi * (-1 - tau)) * w1
                    h_0 = -s / (pi * (-tau)) * w2
                    h_p1 = s / (pi * (1 - tau)) * w3
                    h_p2 = -s / (pi * (2 - tau)) * w4
                    y = b4 * h_m2 + b3 * h_m1 + b2 * h_0 + b1 * h_p1 + b0 * h_p2
                dz0, dz1, dz2 = dz1, dz2, y
                counter += 1
                if counter >= sps:
                    counter -= sps
                    dz = dz0 - dz2
                    err = math.tanh((dz * last.conjugate()).real)
                    dtau += alpha * err
                    tau += beta * err
                tau += dtau / sps
                last = y

                # Costas loop
                so = y * complex(math.cos(cl_p), -math.sin(cl_p))
                re, im = so.real, so.imag
                pe = math.copysign(1.0, re) * im - math.copysign(1.0, im) * re
                cl_f += cl_b * pe
                cl_p = (cl_p + cl_a * pe + cl_f) % (2 * pi)
                out.append(so)
                par.append(cnt & 1)
                cnt += 1

                # Keep tau inside the interpolator: one sample later -> one extra
                # output from this buffer, one sample earlier -> skip the next input
                if tau > 1.0:
                    tau -= 1.0
                    emit += 1
                elif tau < -1.0:
                    tau += 1.0
                    hold = True

        self.tr_buf = [b0, b1, b2, b3, b4]
        self.tr_dz = [dz0, dz1, dz2]
        self.tr_tau, self.tr_dtau = tau, dtau
        self.tr_counter = counter
        self.tr_last_sample = last
        self.out_count = cnt
        self.tr_hold = hold
        self.cl_frequency_out, self.cl_phase_est = cl_f, cl_p
        return out, par

    def _extract_packets(self):
        num_of_possible_packets = len(self.bit_string) / PACKET_BITS
        if num_of_possible_packets > 20 and self.bit_offset == -1:
            # Score every bit offset by how many valid headers it lines up,
            # also bit-reversed (in case the endianness is reversed)
            bs = self.bit_string
            scores = np.zeros(PACKET_BITS)
            revscores = np.zeros(PACKET_BITS)
            for xx in range(0, PACKET_BITS):
                for yy in range(xx, len(bs) - xx - 8, PACKET_BITS):
                    if bs[yy:yy + 8][::-1] in self.packet_headers:
                        scores[xx] += 1
                    if bs[yy:yy + 8] in self.packet_headers:
                        revscores[xx] += 1
            self.reverse = False
            max_score = max(np.max(scores), np.max(revscores))
            if max_score > 15 and float(max_score) / num_of_possible_packets > 0.5:
                self.reverse = bool(np.max(scores) < np.max(revscores))
                self.bit_offset = int(np.argmax(revscores if self.reverse else scores))
                self.bit_string = self.bit_string[self.bit_offset:]
                self.bits_dropped += self.bit_offset

        raw = []
        bits_to_remove = 0
        if len(self.bit_string) > 4 * PACKET_BITS and self.bit_offset != -1:
            bs = self.bit_string
            skip_next = False
            ephemeris_header = packet_dict['Ephemeris']['hex_header']
            for xx in range(0, len(bs) - 2 * PACKET_BITS, PACKET_BITS):
                if skip_next:
                    skip_next = False
                    continue
                byte = bs[xx:xx + 8] if self.reverse else bs[xx:xx + 8][::-1]
                packet_length = PACKET_BITS
                if '{:02X}'.format(int(byte, 2)) == ephemeris_header:
                    packet_length = EPHEMERIS_BITS
                    skip_next = True
                if len(bs) < xx + packet_length:
                    continue
                packet = ''
                for yy in range(0, packet_length, 8):
                    byte = bs[xx + yy:xx + yy + 8]
                    packet += '{:02X}'.format(int(byte if self.reverse else byte[::-1], 2))
                bits_to_remove += packet_length
                raw.append((xx, packet))

        # If more than 90% fail the checksum keep the bits and look for a new bit offset
        parsed = [(xx, parse_packet(p)) for xx, p in raw]
        if raw:
            bad = sum(1 for _, p in parsed if p is None)
            if bad / len(raw) > 0.9:
                parsed = []
                self.bit_offset = -1
            else:
                self.bit_string = self.bit_string[bits_to_remove:]

        results = []
        for xx, p in parsed:
            if p is None:
                self.bad_packets += 1
                continue
            self.good_packets += 1
            bit_index = self.bits_dropped + xx
            stream_time = ((2 * bit_index + self.symbol_offset) * self.decimation + self.lpf_delay
                           + (len(self.rrc_taps) // 2 + 2) * self.decimation) / self.sample_rate
            p['stream_time'] = stream_time
            p['time'] = None if self.t0 is None else self.t0 + stream_time
            p['carrier_offset_hz'] = self.carrier_offset
            p['channel_hz'] = self.sat_center_frequency
            if p['packet_type'] in ('Sync', 'Ephemeris'):
                self.sat_id = p.get('sat_id', self.sat_id)
            results.append(p)
        if raw and self.bit_offset != -1:
            self.bits_dropped += bits_to_remove

        if num_of_possible_packets > 60 and self.bit_offset == -1:
            self.resets += 1
            self.init_default_values()
        return results


RealtimeDecoder = OrbcommDecoder
