"""The live spectrum and the sound, taken from the same samples a decoder is working on - shared by every
system's receiver (sdr_source.py for Iridium, orbcomm_rx.py for Orbcomm).

Spectrum datagrams, FRAME_RATE a second: b'IRS2', centre Hz (float64), sample rate (float64), then
FFT_SIZE float32 dB values of the PEAK over the frame's FFTs, then FFT_SIZE of their AVERAGE (lowest
frequency first). Every sample goes through the FFT (no gaps), so even an 8 ms burst can't fall between frames.

Sound: the raw radio as audio, like SDR++. Modes, set by the tracker:
  wide   the envelope of the whole received slice: every burst in it
  tuned  one channel at an offset from the centre, demodulated as nfm, wfm, am, usb, lsb or cw
Audio datagrams: b'IRAU' + 16-bit 48 kHz mono samples. Once a second a b'IRAH <mode> <demod> <bandwidth Hz>'
hello is sent so the tracker knows where to send commands back:
  CTRL <mode> <offset Hz>                          (keeps the current demod and bandwidth)
  CTRL <mode> <offset Hz> <demod> <bandwidth Hz>
Sending never blocks, so the sound can never hold up the decoder.
"""
import fractions
import socket
import struct
import sys
import time

import numpy as np
from gnuradio import analog, blocks, fft as gr_fft, filter as gr_filter, gr
from gnuradio.fft import window
from gnuradio.filter import firdes

FFT_SIZE = 2048
FRAME_RATE = 15
AUDIO_RATE = 48000
DEMODS = ('nfm', 'wfm', 'am', 'usb', 'lsb', 'cw')
CW_TONE = 700           # Hz: a CW carrier is heard as this tone (the BFO offset)
SSB_LOW = 100           # Hz: USB / LSB pass from here to the bandwidth (no hum next to the carrier)


def demod_name(demod):
    """a system's 'demod' key -> one of DEMODS ('fm' is narrow FM)"""
    d = (demod or 'am').lower()
    return 'nfm' if d == 'fm' else d if d in DEMODS else 'am'


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

    def __init__(self, port, apply, demod, bandwidth):
        gr.sync_block.__init__(self, 'udp_audio', in_sig=[np.float32], out_sig=None)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.addr = ('127.0.0.1', port)
        self.apply = apply
        self.mode, self.offset = 'off', 0.0
        self.demod, self.bandwidth = demod, bandwidth
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
            if len(f) in (3, 5) and f[0] == b'CTRL' and f[1] in (b'off', b'wide', b'tuned'):
                try:
                    offset = float(f[2])
                    demod, bandwidth = self.demod, self.bandwidth
                    if len(f) == 5 and f[3].decode().lower() in DEMODS:
                        demod, bandwidth = f[3].decode().lower(), max(50.0, float(f[4]))
                except ValueError:
                    continue
                self.mode, self.offset = f[1].decode(), offset
                self.demod, self.bandwidth = self.apply(self.mode, self.offset, demod, bandwidth)

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
            self._send(b'IRAH %s %s %d' % (self.mode.encode(), self.demod.encode(), int(round(self.bandwidth))))
        if self.mode != 'off' and len(x):
            # the static's level, from the noise itself (the median ignores bursts), changing over ~3 s so the
            # background stays steady instead of dipping after every burst
            noise = float(np.median(np.abs(x))) / 0.674 + 1e-9
            a = 1 - np.exp(-len(x) / (AUDIO_RATE * 3.0))
            self.rms = (1 - a) * self.rms + a * noise
            # static at about -22 dB; bursts above it, rounded off gently instead of clipped
            y = np.tanh(x / (12 * self.rms)).astype(np.float32)
            self.pending = np.concatenate([self.pending, y])
            while len(self.pending) >= 960:
                self._send(b'IRAU' + (self.pending[:960] * 30000).astype('<i2').tobytes())
                self.pending = self.pending[960:]
        return len(x)


def parent_alive(pid):
    """is the tracker that started this receiver still running? (a receiver left behind would keep the SDR busy)"""
    if not pid:
        return True
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259     # STILL_ACTIVE
    finally:
        k.CloseHandle(h)


def add_spectrum(tb, src, port, center, rate):
    """attach the spectrum tap to a flowgraph's source block; returns its blocks (keep them alive)"""
    win = window.blackmanharris(FFT_SIZE)
    chain = [blocks.stream_to_vector(gr.sizeof_gr_complex, FFT_SIZE),
             gr_fft.fft_vcc(FFT_SIZE, True, win, True),
             blocks.complex_to_mag_squared(FFT_SIZE),
             UdpSpectrum(port, center, rate, win)]
    tb.connect(src, *chain)
    print('Spectrum: %d-point FFT of every sample, peak + average %d times/s' % (FFT_SIZE, FRAME_RATE), file=sys.stderr)
    return chain


def _complex(taps):
    return [complex(t) for t in taps]


def channel_taps(demod, bandwidth, fs):
    """the tuned channel's filter: centred on the carrier, or one sideband of it for USB / LSB"""
    trans = max(bandwidth * 0.15, 100.0)
    if demod == 'usb':
        return firdes.complex_band_pass(1, fs, SSB_LOW, bandwidth, trans)
    if demod == 'lsb':
        return firdes.complex_band_pass(1, fs, -bandwidth, -SSB_LOW, trans)
    return _complex(firdes.low_pass(1, fs, bandwidth / 2, trans))


def add_audio(tb, src, port, rate, channel_hz=36e3, demod='am'):
    """attach the sound tap: 'tuned' hears one channel, demodulated as one of DEMODS with a bandwidth that
    the tracker can change while running (channel_hz and demod are the start values: AM for bursts like
    Iridium's, FM for constant-envelope signals like Orbcomm's); 'wide' is the envelope of everything.
    Returns its blocks (keep them alive)."""
    # the channel is cut out at ~250 kHz (room for wide FM), then everything but wide FM goes on to ~50 kHz
    decim1 = max(1, int(rate // 250000))
    mid1 = rate / decim1
    decim2 = max(1, int(round(mid1 / 50000)))
    mid = mid1 / decim2
    xlate = gr_filter.freq_xlating_fir_filter_ccf(decim1, firdes.low_pass(1, rate, 0.4 * mid1, 0.2 * mid1), 0, rate)
    # wide FM: its own channel filter at ~250 kHz, demodulated there, the audio taken down to ~50 kHz
    wfm_chan = gr_filter.fft_filter_ccc(1, channel_taps('wfm', 150e3, mid1))
    wfm_demod = analog.quadrature_demod_cf(1.0)
    audio_cut = min(15e3, 0.4 * mid)
    wfm = [wfm_chan, wfm_demod, gr_filter.fir_filter_fff(decim2, firdes.low_pass(1, mid1, audio_cut, audio_cut / 3)),
           analog.fm_deemph(mid, 75e-6), gr_filter.dc_blocker_ff(64, True), blocks.multiply_const_ff(0.0)]
    # the narrow modes share one channel filter at ~50 kHz
    narrow = gr_filter.fir_filter_ccf(decim2, firdes.low_pass(1, mid1, 0.4 * mid, 0.1 * mid)) if decim2 > 1 else None
    chan = gr_filter.fft_filter_ccc(1, channel_taps('am', 10e3, mid))
    nfm_demod = analog.quadrature_demod_cf(1.0)
    nfm_audio = gr_filter.fir_filter_fff(1, firdes.low_pass(1, mid, 5e3, 1.5e3))       # the "de-emphasis"
    branches = {
        'wfm': wfm,
        'nfm': [nfm_demod, nfm_audio, gr_filter.dc_blocker_ff(64, True), blocks.multiply_const_ff(0.0)],
        'am': [blocks.complex_to_mag(1), gr_filter.dc_blocker_ff(64, True), blocks.multiply_const_ff(0.0)],
        'ssb': [blocks.complex_to_real(1), blocks.multiply_const_ff(0.0)],      # USB and LSB: the filter differs
        'cw': [blocks.rotator_cc(2 * np.pi * CW_TONE / mid), blocks.complex_to_real(1), blocks.multiply_const_ff(0.0)],
    }
    wide = [blocks.complex_to_mag(1), gr_filter.fir_filter_fff(decim1 * decim2, firdes.low_pass(1, rate, 20e3, 10e3)),
            gr_filter.dc_blocker_ff(64, True), blocks.multiply_const_ff(0.0)]
    mix = blocks.add_ff(1)
    ratio = fractions.Fraction(AUDIO_RATE, int(round(mid))).limit_denominator(1000)
    resample = gr_filter.rational_resampler_fff(ratio.numerator, ratio.denominator)      # -> 48 kHz
    state = {}

    def apply(mode, offset, demod, bandwidth):
        """switch the sound; returns the (demod, bandwidth) actually used"""
        if demod == 'wfm':
            bandwidth = min(max(bandwidth, 10e3), 0.8 * mid1)
        elif demod in ('usb', 'lsb'):
            bandwidth = min(max(bandwidth, 2.0 * SSB_LOW), 0.38 * mid)
        else:
            bandwidth = min(max(bandwidth, 50.0), 0.75 * mid)
        if state.get('channel') != (demod, bandwidth):
            state['channel'] = (demod, bandwidth)
            if demod == 'wfm':
                wfm_chan.set_taps(channel_taps('wfm', bandwidth, mid1))
                wfm_demod.set_gain(mid1 / (2 * np.pi * bandwidth / 2))
            else:
                chan.set_taps(channel_taps(demod, bandwidth, mid))
                nfm_demod.set_gain(mid / (2 * np.pi * bandwidth / 2))
                cut = min(max(bandwidth / 2, 2500.0), 0.45 * mid)
                nfm_audio.set_taps(firdes.low_pass(1, mid, cut, cut / 3))
        active = 'ssb' if demod in ('usb', 'lsb') else demod
        for name, chain in branches.items():
            chain[-1].set_k(1.0 if mode == 'tuned' and name == active else 0.0)
        wide[-1].set_k(1.0 if mode == 'wide' else 0.0)
        if mode == 'tuned':
            xlate.set_center_freq(offset)
        return demod, bandwidth

    demod = demod_name(demod)
    demod, channel_hz = apply('off', 0.0, demod, channel_hz)
    sink = UdpAudio(port, apply, demod, channel_hz)
    tb.connect(src, xlate)
    tb.connect(xlate, *wfm)
    if narrow is not None:
        tb.connect(xlate, narrow, chan)
    else:
        tb.connect(xlate, chan)
    for name in ('nfm', 'am', 'ssb', 'cw'):
        tb.connect(chan, *branches[name])
    for i, chain in enumerate(list(branches.values()) + [wide]):
        tb.connect(chain[-1], (mix, i))
    tb.connect(src, *wide)
    tb.connect(mix, resample, sink)     # (filters and squelch are applied in the tracker)
    print('Sound: available (whole band, or one tuned channel: %s, starting %s %.1f kHz)'
          % (' '.join(DEMODS).upper(), demod.upper(), channel_hz / 1e3), file=sys.stderr)
    return [xlate, narrow, chan, wide, mix, resample, sink] + list(branches.values())
