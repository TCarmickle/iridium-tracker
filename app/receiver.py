"""The Iridium receiver, run from inside the tracker (what receive.ps1 / 1-receive.bat do).

  iridium-extractor (gr-iridium)  SDR samples -> raw bursts      -> output-<stamp>.bits
  iridium-parser (iridium-toolkit) raw bursts -> decoded frames  -> output-<stamp>.live
  output-<stamp>.start            the real UTC moment the burst clock started
After Stop, catalog.py decodes the whole capture into the catalog.

A too-low burst threshold floods the decoder with false bursts and eats all the RAM,
so the decoder is stopped automatically once it uses more than LIMIT_GB.
"""
import ctypes
import datetime
import os
import re
import subprocess
import sys
import time

from PyQt5 import QtCore

import catalog

HERE = os.path.dirname(os.path.abspath(__file__))
RC = os.path.dirname(sys.executable)          # the radioconda (or packaged) Python running us
PY = os.path.join(RC, 'python.exe')
EXTRACTOR = os.path.join(RC, 'Library', 'bin', 'iridium-extractor.py')
THRESHOLD = 16      # dB over noise; never go below ~14 (10 dB crashed the PC)
LIMIT_GB = 2
NO_WINDOW = 0x08000000   # CREATE_NO_WINDOW


def private_bytes(pid):
    """memory a Windows process has committed (what Task Manager calls Commit size)"""
    class PMC(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong)] + \
                   [(n, ctypes.c_size_t) for n in ('PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                                                   'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                                                   'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage',
                                                   'PrivateUsage')]
    h = ctypes.windll.kernel32.OpenProcess(0x1000 | 0x0010, False, pid)
    if not h:
        return 0
    try:
        c = PMC()
        c.cb = ctypes.sizeof(c)
        return c.PrivateUsage if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb) else 0
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


class Receiver(QtCore.QObject):
    """start() / stop() the SDR receiver. Signals:
       status(text)      one-line state for the UI
       running(bool)     receiver started / fully stopped
       decoded()         the capture has been added to the catalog"""
    status = QtCore.pyqtSignal(str)
    running = QtCore.pyqtSignal(bool)
    decoded = QtCore.pyqtSignal()

    def __init__(self, replay=None):
        super().__init__()
        self.replay = replay          # for testing: feed an old .bits file instead of the SDR
        self.extractor = self.parser = self.decoder = None
        self.stamp = None
        self.state = 'idle'           # idle / running / stopping / decoding
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.env = QtCore.QProcessEnvironment.systemEnvironment()
        path = os.pathsep.join([RC] + [os.path.join(RC, *p) for p in (('Library', 'mingw-w64', 'bin'), ('Library', 'usr', 'bin'),
                                                                     ('Library', 'bin'), ('Scripts',), ('bin',))])
        self.env.insert('PATH', path + os.pathsep + self.env.value('PATH'))
        self.env.insert('PYTHONUNBUFFERED', '1')
        # OpenBLAS (the packaged NumPy's maths library) otherwise reserves a buffer per CPU core - about
        # 2.5 GB on a 20-core PC - which the memory watchdog would mistake for a runaway decoder
        self.env.insert('OPENBLAS_NUM_THREADS', '1')

    def _proc(self):
        p = QtCore.QProcess(self)
        p.setProcessEnvironment(self.env)
        p.setWorkingDirectory(HERE)
        return p

    @property
    def busy(self):
        return self.state != 'idle'

    # ---- start
    def start(self):
        if self.busy:
            return
        self.stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        stem = os.path.join(HERE, 'output-' + self.stamp)
        self.bits_path, self.live_path, self.start_path = stem + '.bits', stem + '.live', stem + '.start'
        self.stream_start = None
        self.heard = self.decoded_n = self.fixes = 0
        self.t_start = time.time()
        self.stop_reason = ''
        self.last_status = ''

        self.parser = self._proc()                       # decoded frames, as they arrive
        self.parser.readyReadStandardOutput.connect(self._parser_output)
        self.parser.setStandardErrorFile(QtCore.QProcess.nullDevice())
        self.parser.start(PY, ['-u', catalog.PARSER, '--no-stats'])
        if not self.parser.waitForStarted(5000):
            self.status.emit('Could not start the decoder (iridium-parser): %s' % self.parser.errorString())
            return

        self.extractor = self._proc()                    # SDR -> raw bursts
        self.extractor.setStandardOutputFile(self.bits_path)
        self.extractor.setStandardErrorFile(stem + '.log')
        self.extractor.finished.connect(self._extractor_finished)
        if self.replay:
            self.extractor.start(PY, ['-c', REPLAY_CODE, self.replay])
        else:
            self.extractor.start(PY, [EXTRACTOR, '-d', str(THRESHOLD), os.path.join(HERE, 'rtl-sdr.conf')])
        if not self.extractor.waitForStarted(5000):
            self.status.emit('Could not start iridium-extractor: %s' % self.extractor.errorString())
            self.parser.kill()
            return
        self.bits_pos = self.log_pos = 0
        self.bits_rest = ''
        self.state = 'running'
        self.timer.start(250)
        self.running.emit(True)
        self.status.emit('Starting the SDR...')

    # ---- while running
    def _tick(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        self._feed_bits(now)
        self._read_log()
        if self.state != 'running':
            return
        if not self.replay and private_bytes(self.extractor.processId()) > LIMIT_GB * 2 ** 30:
            self.stop('decoder used over %d GB of memory (too many false bursts) - raise the threshold' % LIMIT_GB)
            return
        e = int(time.time() - self.t_start)
        self.status.emit('Receiving %d:%02d:%02d  ·  %d decoded  ·  %d damaged  ·  %d positions%s'
                         % (e // 3600, e // 60 % 60, e % 60, self.decoded_n, self.heard, self.fixes,
                            '  ·  ' + self.last_status if self.last_status else ''))

    def _feed_bits(self, now):
        try:
            with open(self.bits_path, errors='replace') as f:
                f.seek(self.bits_pos)
                text = f.read()
                self.bits_pos = f.tell()
        except OSError:
            return
        text = self.bits_rest + text
        lines = text.split('\n')
        self.bits_rest = lines.pop()          # an unfinished last line waits for the next read
        for line in lines:
            if not line.startswith('RAW:'):
                continue
            if self.parser and self.parser.state() == QtCore.QProcess.Running:
                self.parser.write((line.rstrip('\r') + '\n').encode())
            # burst times count in ms from when the decoder started reading samples; the earliest
            # (now - burst time) seen is the best estimate of that real moment
            m = re.match(r'RAW: \S+ (\d+\.\d+) ', line)
            if m and not self.replay:
                zero = now - datetime.timedelta(milliseconds=float(m.group(1)))
                if self.stream_start is None or zero < self.stream_start:
                    self.stream_start = zero
                    with open(self.start_path, 'w') as f:
                        f.write(zero.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] + 'Z\n')

    def _read_log(self):
        try:
            with open(self.bits_path[:-5] + '.log', errors='replace') as f:
                f.seek(self.log_pos)
                text = f.read()
                self.log_pos = f.tell()
        except OSError:
            return
        for line in text.splitlines():
            if re.match(r'^\d{10} \|', line):
                self.last_status = ''          # the decoder's per-second totals: it is running fine
            elif line.strip() and not re.search(r'DeprecationWarning|setDaemon', line):
                self.last_status = line.strip()[:120]

    def _parser_output(self):
        data = bytes(self.parser.readAllStandardOutput()).decode(errors='replace')
        if not data:
            return
        with open(self.live_path, 'a', newline='') as f:
            f.write(data.replace('\r\n', '\n').replace('\n', '\r\n'))
        for line in data.splitlines():
            kind = line.split(':', 1)[0]
            if kind in ('RAW', 'ERR'):
                self.heard += 1
            elif kind:
                self.decoded_n += 1
                m = re.search(r' alt=(-?\d+)', line)
                if kind == 'IRA' and m and int(m.group(1)) > 100:     # a real position, not a beam on the ground
                    self.fixes += 1

    # ---- stop
    def stop(self, reason=''):
        if self.state != 'running':
            return
        self.state = 'stopping'
        self.stop_reason = reason
        self.status.emit('Stopping...')
        if self.extractor.state() != QtCore.QProcess.NotRunning:
            # the decoder starts helper processes, so end the whole tree
            subprocess.run(['taskkill', '/PID', str(self.extractor.processId()), '/T', '/F'],
                           capture_output=True, creationflags=NO_WINDOW)
        else:
            self._extractor_finished()

    def _extractor_finished(self, *_):
        if self.state == 'running':           # it ended by itself: the SDR is missing or busy, or a crash
            self.state = 'stopping'
            self._read_log()
            self.stop_reason = self.last_status or 'the decoder stopped by itself'
            if 'Failed to open' in self.stop_reason or 'usb_claim' in self.stop_reason:
                self.stop_reason = ('the SDR could not be opened - is it plugged in, and are SDR++, SatDump '
                                    'and 1-receive.bat closed?')
        if self.state != 'stopping':
            return
        self.timer.stop()
        self._feed_bits(datetime.datetime.now(datetime.timezone.utc))
        self.parser.finished.connect(self._parser_finished)
        self.parser.closeWriteChannel()       # let the parser finish what it was given
        QtCore.QTimer.singleShot(15000, lambda p=self.parser: p.state() != QtCore.QProcess.NotRunning and p.kill())

    def _parser_finished(self, *_):
        self._parser_output()
        size = os.path.getsize(self.bits_path) if os.path.exists(self.bits_path) else 0
        if size < 200:      # nothing was received
            self.state = 'idle'
            self.running.emit(False)
            self.status.emit('Receiver stopped%s - nothing was received' % (': ' + self.stop_reason if self.stop_reason else ''))
            return
        self.state = 'decoding'
        self.status.emit('Stopped%s. Adding the capture to the catalog...' % (' (' + self.stop_reason + ')' if self.stop_reason else ''))
        self.decoder = self._proc()
        self.decoder.finished.connect(self._decoder_finished)
        self.decoder.setStandardOutputFile(QtCore.QProcess.nullDevice())
        self.decoder.setStandardErrorFile(QtCore.QProcess.nullDevice())
        self.decoder.start(PY, [os.path.join(HERE, 'catalog.py'), '--ready=output-' + self.stamp])

    def _decoder_finished(self, *_):
        self.state = 'idle'
        self.running.emit(False)
        ok = self.decoder.exitCode() == 0
        self.status.emit('Capture output-%s %s (%d decoded, %d positions)%s'
                         % (self.stamp, 'added to the catalog' if ok else 'could not be decoded - run 2-decode.bat',
                            self.decoded_n, self.fixes, ' - stopped: ' + self.stop_reason if self.stop_reason else ''))
        if ok:
            self.decoded.emit()

    def shutdown(self):
        """the app is closing: stop the SDR now and decode the capture in the background"""
        if self.state == 'running':
            subprocess.run(['taskkill', '/PID', str(self.extractor.processId()), '/T', '/F'],
                           capture_output=True, creationflags=NO_WINDOW)
            self.timer.stop()
            self._feed_bits(datetime.datetime.now(datetime.timezone.utc))
            self.parser.closeWriteChannel()
            self.parser.waitForFinished(15000)
            self._parser_output()
            self.parser.kill()
            if os.path.exists(self.bits_path) and os.path.getsize(self.bits_path) >= 200:
                subprocess.Popen([PY, os.path.join(HERE, 'catalog.py'), '--ready=output-' + self.stamp],
                                 cwd=HERE, creationflags=NO_WINDOW)


# for testing without the SDR: copies an old capture to stdout at about 200 lines a second
REPLAY_CODE = '''
import sys, time
for i, line in enumerate(open(sys.argv[1], errors="replace")):
    sys.stdout.write(line); sys.stdout.flush()
    if i % 20 == 19: time.sleep(0.1)
time.sleep(3600)
'''
