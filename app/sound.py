"""Plays the receiver's sound: the raw radio, like SDR++ - static, with a click or "zzt" for each burst.

The decoder process (sdr_source.py) makes the audio from the same samples it decodes and sends it here
over a local UDP socket; this plays it and tells the decoder which sound to make:
  off    nothing
  wide   every burst in the received slice
  tuned  one channel (click the spectrum to choose it), demodulated like SDR++: NFM, WFM, AM, USB, LSB or
         CW, with a bandwidth; each band starts in its system's mode, and the last choice per system is kept
Audio that arrives faster than the speakers play is dropped here, never in the decoder.
Optional low-pass / high-pass filters and a squelch are applied here too (settings.json 'audio').
"""
import json
import os
import time

import numpy as np
from PyQt5 import QtCore, QtMultimedia, QtNetwork
from scipy import signal

import paths
import systems

RATE = 48000
LOW_PASS = [0, 2000, 3000, 4000, 6000, 8000]       # Hz, 0 = off
HIGH_PASS = [0, 100, 300, 600, 1000]
SQUELCH_OPEN_S, SQUELCH_CLOSE_S = 0.002, 0.15      # fade in fast, fade out gently (no clicks)
SQUELCH_STEP = RATE // 200                         # the squelch decides every 5 ms
DEMODS = ['nfm', 'wfm', 'am', 'usb', 'lsb', 'cw']    # the tuned sound's modes (sdr_taps.DEMODS)
MODE_BW = {'nfm': 12.5e3, 'wfm': 180e3, 'am': 10e3, 'usb': 2.8e3, 'lsb': 2.8e3, 'cw': 500.0}  # a mode's own width


def demod_name(demod):
    """a system's 'demod' key -> one of DEMODS ('fm' is narrow FM)"""
    d = (demod or 'am').lower()
    return 'nfm' if d == 'fm' else d if d in DEMODS else 'am'


def band_default(system):
    """the tuned sound a system starts with: (demod, bandwidth Hz)"""
    demod = demod_name(system.get('demod'))
    return demod, float(system.get('channel_hz') or MODE_BW[demod])


def passband(demod, bandwidth):
    """the frequencies heard, relative to the tuned frequency (Hz): one side of it for USB / LSB"""
    if demod == 'usb':
        return 0.0, bandwidth
    if demod == 'lsb':
        return -bandwidth, 0.0
    return -bandwidth / 2, bandwidth / 2


def load_audio():
    try:
        a = json.load(open(os.path.join(paths.DATA, 'settings.json'))).get('audio', {})
    except (OSError, ValueError, AttributeError):
        a = {}
    return {'low_pass': int(a.get('low_pass', 0)), 'high_pass': int(a.get('high_pass', 0)),
            'squelch_db': float(a.get('squelch_db', 0)), 'volume': float(a.get('volume', 0.6)),
            'modes': {k: v for k, v in (a.get('modes') or {}).items()   # system key -> {'demod', 'bandwidth'}
                      if isinstance(v, dict) and v.get('demod') in DEMODS}}


def save_audio(a):
    path = os.path.join(paths.DATA, 'settings.json')
    try:
        allset = json.load(open(path))
    except (OSError, ValueError):
        allset = {}
    allset['audio'] = a
    os.makedirs(paths.DATA, exist_ok=True)
    json.dump(allset, open(path, 'w'), indent=1)


class SoundPlayer(QtCore.QObject):
    demodChanged = QtCore.pyqtSignal()      # the tuned sound's mode or bandwidth changed

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sock = QtNetwork.QUdpSocket(self)
        self.sock.bind(QtNetwork.QHostAddress.LocalHost, 0)
        self.port = self.sock.localPort()
        self.sock.readyRead.connect(self._read)
        self.decoder = None             # (address, port) the decoder's audio comes from
        self.mode = 'off'
        self.listen_hz = 1626.2708e6    # the ring-alert channel
        self.center = None              # the receiver's centre frequency (from the spectrum)
        self.system = None              # the receive band's system key (from the centre frequency)
        self.demod, self.bandwidth = band_default(systems.IRIDIUM)
        self.changed = 0.0              # when the mode or bandwidth was last chosen
        fmt = QtMultimedia.QAudioFormat()
        fmt.setSampleRate(RATE)
        fmt.setChannelCount(1)
        fmt.setSampleSize(16)
        fmt.setCodec('audio/pcm')
        fmt.setByteOrder(QtMultimedia.QAudioFormat.LittleEndian)
        fmt.setSampleType(QtMultimedia.QAudioFormat.SignedInt)
        self.out = QtMultimedia.QAudioOutput(QtMultimedia.QAudioDeviceInfo.defaultOutputDevice(), fmt, self)
        self.out.setBufferSize(RATE * 2 // 4)       # 250 ms: little delay, few gaps
        self.io = None
        self.settings = load_audio()
        self.volume = self.settings['volume']
        self.sos = self.zi = None
        self.noise = None           # the static's level, for the squelch
        self.gain = 1.0             # the squelch's current gain (fades between 0 and 1)
        self._design()
        # a small jitter buffer: packets arrive a little unevenly, and letting the speakers run dry
        # between them sounds like crackle
        self.queue = bytearray()
        self.primed = False
        self.pump = QtCore.QTimer(self)
        self.pump.timeout.connect(self._pump)
        self.pump.start(10)

    def set_mode(self, mode, listen_hz=None):
        self.mode = mode
        if listen_hz is not None:
            self.listen_hz = listen_hz
        if mode == 'off':
            if self.io is not None:
                self.out.stop()
                self.io = None
            self.queue.clear()
            self.primed = False
        elif self.io is None:
            self.io = self.out.start()
            self.out.setVolume(self.volume)
        self._command()

    force_system = None     # set while the receiver is on Radio: its mode stays, wherever it is tuned

    def set_band(self, center):
        """the receiver is on a (new) band: use its system's last mode, or its own default"""
        self._use_system(systems.get(self.force_system) if self.force_system else systems.of_frequency(center))

    def tune_center(self, center):
        """Radio: move the SDR itself (the tuned frequency left the received slice); False if nobody to tell"""
        if self.decoder is None:
            return False
        self.center = float(center)
        self.sock.writeDatagram(b'TUNE %d' % int(round(center)), *self.decoder)
        self._command()         # the tuned channel's offset from the new centre
        return True

    def _use_system(self, s):
        self.changed = time.time()
        self.system = s['key']
        saved = self.settings['modes'].get(self.system)
        if saved:
            self.demod, self.bandwidth = saved['demod'], float(saved.get('bandwidth') or MODE_BW[saved['demod']])
        else:
            self.demod, self.bandwidth = band_default(s)
        self.demodChanged.emit()

    def set_demod(self, demod=None, bandwidth=None):
        """choose the tuned sound's mode and/or bandwidth (a new mode without a bandwidth gets the band's own
        width if it is the band's mode, else the mode's usual one); remembered for the band's system"""
        self.changed = time.time()
        if demod is not None and demod != self.demod:
            own = band_default(systems.get(self.system)) if self.system else None
            self.demod = demod
            self.bandwidth = own[1] if own and own[0] == demod else MODE_BW[demod]
        if bandwidth is not None:
            self.bandwidth = float(bandwidth)
        if self.system:
            self.settings['modes'][self.system] = {'demod': self.demod, 'bandwidth': self.bandwidth}
            save_audio(self.settings)
        self.demodChanged.emit()
        self._command()

    def reset_demod(self):
        """back to the band's own mode and width (forgets the remembered choice)"""
        if self.system:
            self.settings['modes'].pop(self.system, None)
            save_audio(self.settings)
            self._use_system(systems.get(self.system))
        self._command()

    def set_volume(self, v):
        self.volume = v
        self.out.setVolume(v)
        self.settings['volume'] = v
        save_audio(self.settings)

    def set_filters(self, low_pass=None, high_pass=None, squelch_db=None):
        for key, v in (('low_pass', low_pass), ('high_pass', high_pass), ('squelch_db', squelch_db)):
            if v is not None:
                self.settings[key] = v
        save_audio(self.settings)
        self._design()

    def _design(self):
        parts = []
        if self.settings['high_pass']:
            parts.append(signal.butter(2, self.settings['high_pass'], 'highpass', fs=RATE, output='sos'))
        if self.settings['low_pass']:
            parts.append(signal.butter(4, self.settings['low_pass'], 'lowpass', fs=RATE, output='sos'))
        self.sos = np.vstack(parts) if parts else None
        self.zi = np.zeros((len(self.sos), 2)) if self.sos is not None else None
        if not self.settings['squelch_db']:
            self.gain = 1.0

    def _process(self, pcm):
        """filters, then the squelch: the static is muted, anything squelch_db louder opens it"""
        x = np.frombuffer(pcm, '<i2').astype(np.float64) / 32768
        if self.sos is not None:
            x, self.zi = signal.sosfilt(self.sos, x, zi=self.zi)
        sq = self.settings['squelch_db']
        if sq:
            x = x.copy()
            step = SQUELCH_STEP
            for i in range(0, len(x), step):        # decide every 5 ms, so an 8 ms burst opens it in time
                block = x[i:i + step]
                level = float(np.sqrt(np.mean(block * block))) + 1e-9
                if self.noise is None:
                    self.noise = level
                louder = 20 * np.log10(level / self.noise)
                if louder < sq:     # learn the static's level from the moments that don't open the squelch
                    self.noise = 0.98 * self.noise + 0.02 * level
                target = 1.0 if louder >= sq else 0.0
                # fade towards the target: fully open within SQUELCH_OPEN_S, closed over SQUELCH_CLOSE_S
                rate = 1 / (SQUELCH_OPEN_S if target > self.gain else SQUELCH_CLOSE_S) / RATE
                ramp = self.gain + np.sign(target - self.gain) * rate * np.arange(1, len(block) + 1)
                ramp = np.minimum(ramp, target) if target > self.gain else np.maximum(ramp, target)
                block *= ramp
                self.gain = float(ramp[-1])
        return (np.clip(x, -1, 1) * 32767).astype('<i2').tobytes()

    def _command(self):
        if self.decoder is None:
            return
        offset = self.listen_hz - self.center if (self.center and self.mode == 'tuned') else 0.0
        self.sock.writeDatagram(b'CTRL %s %d %s %d' % (self.mode.encode(), int(round(offset)), self.demod.encode(),
                                                       int(round(self.bandwidth))), *self.decoder)

    def _read(self):
        while self.sock.hasPendingDatagrams():
            dg = self.sock.receiveDatagram()
            data = bytes(dg.data())
            if data.startswith(b'IRAH'):        # the decoder saying hello (once a second): keep it in step
                self.decoder = (dg.senderAddress(), dg.senderPort())
                f = data[5:].decode(errors='replace').split()       # mode [demod bandwidth]
                if len(f) == 3 and f[1] == self.demod:
                    try:
                        bw = float(f[2])
                    except ValueError:
                        bw = self.bandwidth
                    # the receiver can't go that wide/narrow: show what it uses (not a hello sent before our change)
                    if abs(bw - self.bandwidth) > 1 and time.time() - self.changed > 2:
                        self.bandwidth = bw
                        self.demodChanged.emit()
                if not f or f[0] != self.mode or self.mode == 'tuned':
                    self._command()
            elif data.startswith(b'IRAU') and self.io is not None:
                self.queue += self._process(data[4:])
                if len(self.queue) > RATE * 2 // 2:          # over 0.5 s behind: drop the oldest
                    del self.queue[:len(self.queue) - RATE * 2 // 4]
        self._pump()

    def _pump(self):
        """feed the speakers from the queue, starting only once ~100 ms is waiting"""
        if self.io is None:
            return
        if not self.primed:
            if len(self.queue) < RATE * 2 // 10:
                return
            self.primed = True
        n = min(len(self.queue), self.out.bytesFree()) // 2 * 2
        if n:
            self.io.write(bytes(self.queue[:n]))
            del self.queue[:n]
        if not self.queue and self.out.bytesFree() >= self.out.bufferSize():
            self.primed = False                  # the speakers ran dry: build up a little again first


# ---------------------------------------------------------------- the new-satellite chime
CHIME_NOTES = ((880.0, 0.0), (1318.5, 0.13))     # A5 then E6 (a rising fifth), start times in s


def make_chime(path):
    """write the chime: two soft bell notes, a rising fifth (16-bit mono WAV)"""
    import wave
    n = int(RATE * 0.9)
    t = np.arange(n) / RATE
    x = np.zeros(n)
    for f, t0 in CHIME_NOTES:
        tt = np.clip(t - t0, 0, None)
        env = np.where(t >= t0, (1 - np.exp(-tt / 0.004)) * np.exp(-tt / 0.22), 0.0)   # quick attack, ring out
        x += env * (np.sin(2 * np.pi * f * tt) + 0.25 * np.sin(2 * np.pi * 2 * f * tt))
    x *= 0.45 / np.abs(x).max()
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((x * 32767).astype('<i2').tobytes())


class Chime(QtCore.QObject):
    """plays the chime (made once, kept in the data folder)"""

    def __init__(self, parent=None):
        super().__init__(parent)
        path = os.path.join(paths.DATA, 'new-satellite.wav')
        if not os.path.exists(path):
            os.makedirs(paths.DATA, exist_ok=True)
            make_chime(path)
        self.effect = QtMultimedia.QSoundEffect(self)
        self.effect.setSource(QtCore.QUrl.fromLocalFile(path))
        self.effect.setVolume(0.8)

    def play(self):
        self.effect.play()
