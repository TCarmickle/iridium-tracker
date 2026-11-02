"""Plays the receiver's sound: the raw radio, like SDR++ - static, with a click or "zzt" for each burst.

The decoder process (sdr_source.py) makes the audio from the same samples it decodes and sends it here
over a local UDP socket; this plays it and tells the decoder which sound to make:
  off    nothing
  wide   every burst in the received slice
  tuned  one channel (click the spectrum to choose it), AM-demodulated like SDR++
Audio that arrives faster than the speakers play is dropped here, never in the decoder.
Optional low-pass / high-pass filters and a squelch are applied here too (settings.json 'audio').
"""
import json
import os

import numpy as np
from PyQt5 import QtCore, QtMultimedia, QtNetwork
from scipy import signal

import paths

RATE = 48000
LOW_PASS = [0, 2000, 3000, 4000, 6000, 8000]       # Hz, 0 = off
HIGH_PASS = [0, 100, 300, 600, 1000]
SQUELCH_OPEN_S, SQUELCH_CLOSE_S = 0.002, 0.15      # fade in fast, fade out gently (no clicks)
SQUELCH_STEP = RATE // 200                         # the squelch decides every 5 ms


def load_audio():
    try:
        a = json.load(open(os.path.join(paths.DATA, 'settings.json'))).get('audio', {})
    except (OSError, ValueError, AttributeError):
        a = {}
    return {'low_pass': int(a.get('low_pass', 0)), 'high_pass': int(a.get('high_pass', 0)),
            'squelch_db': float(a.get('squelch_db', 0)), 'volume': float(a.get('volume', 0.6))}


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
        self.sock.writeDatagram(b'CTRL %s %d' % (self.mode.encode(), int(round(offset))), *self.decoder)

    def _read(self):
        while self.sock.hasPendingDatagrams():
            dg = self.sock.receiveDatagram()
            data = bytes(dg.data())
            if data.startswith(b'IRAH'):        # the decoder saying hello (once a second): keep it in step
                self.decoder = (dg.senderAddress(), dg.senderPort())
                if data[5:].decode(errors='replace') != self.mode or self.mode == 'tuned':
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
