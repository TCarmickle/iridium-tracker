"""Plays the receiver's sound: the raw radio, like SDR++ - static, with a click or "zzt" for each burst.

The decoder process (sdr_source.py) makes the audio from the same samples it decodes and sends it here
over a local UDP socket; this plays it and tells the decoder which sound to make:
  off    nothing
  wide   every burst in the received slice
  tuned  one channel (click the spectrum to choose it), AM-demodulated like SDR++
Audio that arrives faster than the speakers play is dropped here, never in the decoder.
"""
from PyQt5 import QtCore, QtMultimedia, QtNetwork

RATE = 48000


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
        self.volume = 0.6

    def set_mode(self, mode, listen_hz=None):
        self.mode = mode
        if listen_hz is not None:
            self.listen_hz = listen_hz
        if mode == 'off':
            if self.io is not None:
                self.out.stop()
                self.io = None
        elif self.io is None:
            self.io = self.out.start()
            self.out.setVolume(self.volume)
        self._command()

    def set_volume(self, v):
        self.volume = v
        self.out.setVolume(v)

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
                pcm = data[4:]
                if self.out.bytesFree() >= len(pcm):     # otherwise drop it: the speakers are behind
                    self.io.write(pcm)
