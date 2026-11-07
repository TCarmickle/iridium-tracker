r"""The receiver (every band in systems.ORDER), run from the tracker or the console. Iridium's files go to data\captures:

  iridium-extractor (gr-iridium)  SDR samples -> raw bursts      -> output-<stamp>.bits
  iridium-parser (iridium-toolkit) raw bursts -> decoded frames  -> output-<stamp>.live
  the decoder's messages (its once-a-second totals thinned out)  -> output-<stamp>.log
  output-<stamp>.start            the real UTC moment the burst clock started
After Stop, catalog.py adds the capture to the catalog (see storage.py for what is kept).

A too-low burst threshold floods the decoder with false bursts and eats all the RAM,
so the decoder is stopped automatically once it uses more than LIMIT_GB.
"""
import ctypes
import datetime
import glob
import json
import os
import re
import subprocess
import sys
import time

from PyQt5 import QtCore

import catalog
import paths
import sdr_settings
import storage
import systems

HERE = os.path.dirname(os.path.abspath(__file__))
RC = os.path.dirname(sys.executable)          # the radioconda (or packaged) Python running us
PY = os.path.join(RC, 'python.exe')
EXTRACTOR = os.path.join(RC, 'Library', 'bin', 'iridium-extractor.py')   # gr-iridium's decoder
SOURCE = os.path.join(HERE, 'sdr_source.py')    # runs it with our device options and the spectrum tap
ORBCOMM_RX = os.path.join(HERE, 'orbcomm_rx.py')  # Orbcomm: receiver + decoder in one process
HAM_RX = os.path.join(HERE, 'ham_rx.py')          # ISS and ham CubeSats (2 m or 70 cm): the same
GNSS_RX = os.path.join(HERE, 'gnss_rx.py')        # GPS / Galileo / BeiDou: records chunks for PocketSDR
METEOR_RX = os.path.join(HERE, 'meteor_rx.py')    # Meteor-M pictures: records the pass; SatDump decodes it after
IMAGES_PY = os.path.join(HERE, 'images.py')
RADIO_RX = os.path.join(HERE, 'radio_rx.py')      # Radio: spectrum and sound anywhere, retuned while it runs
DAMAGED = ('RAW', 'ERR', 'OBAD', 'HSIG')        # frame kinds that were heard but could not be decoded
LIMIT_GB = 2
STATUS_EVERY = 10   # the decoder prints its totals every second; the log keeps one line in this many
NO_WINDOW = 0x08000000   # CREATE_NO_WINDOW


RUNNING = '.running'     # <capture>.running: written while a receiver is on, holding its program's process ID


def snap_trigger():
    """the file the Integrity section writes an event's name into, so the GNSS receiver saves the raw samples
    around it"""
    return os.path.join(paths.DATA, 'integrity', 'iq-trigger.txt')


def _alive(pid):
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def active_capture():
    """(capture name, process ID) of the capture some receiver - this program's, the tracker's or the console's - is
    receiving right now, or None. Decided by its .running marker, not by how recently files changed (a capture
    that has just stopped looks fresh for a while); a marker left by a program that ended without removing it
    (a crash) is removed here."""
    for path in sorted(glob.glob(os.path.join(paths.CAPTURES, '*' + RUNNING)), key=os.path.getmtime, reverse=True):
        try:
            pid = int(open(path).read().split()[0])
        except (OSError, ValueError, IndexError):
            pid = 0
        if pid and _alive(pid):
            return os.path.basename(path)[:-len(RUNNING)], pid
        try:
            os.remove(path)
        except OSError:
            pass
    return None


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
       decoded()         the capture has been added to the catalog
       frame(kind, line) each burst as the parser finishes it (kind RAW / ERR = damaged)
       gnss_line(kind, line)  a GNSS receiver's noise (GNOISE) and clock (GTIME) lines"""
    status = QtCore.pyqtSignal(str)
    running = QtCore.pyqtSignal(bool)
    decoded = QtCore.pyqtSignal()
    frame = QtCore.pyqtSignal(str, str)
    gnss_line = QtCore.pyqtSignal(str, str)     # GNSS receivers' GNOISE / GSPEC / GTIME lines (kind, line)

    def __init__(self, replay=None):
        super().__init__()
        self.replay = replay          # for testing: feed an old .bits file instead of the SDR
        self.spectrum_port = 0        # set by the tracker: where to send the live spectrum
        self.audio_port = 0           # ... and the sound
        self.sdr = sdr_settings.load()
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
    def start(self, band=None):
        """band: a systems.ORDER key to receive this time (the console's choice), instead of the tracker's saved one"""
        if self.busy:
            return
        need = storage.settings()['min_free_gb']
        if storage.free_gb() < need:
            self.status.emit('Not starting: less than %.1f GB free on the disk holding %s' % (need, paths.DATA))
            return
        other = active_capture()
        if other and other[1] != os.getpid():
            self.status.emit('Not starting: another receiver is running (%s, %s) - stop it first (only one program '
                             'can use the SDR)' % (other[0], systems.of_capture(other[0])['label']))
            return
        self.stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        self.sdr = sdr_settings.load()
        if band:
            self.sdr['system'] = band
        self.system = systems.get('iridium' if self.replay else self.sdr['system'])
        self.capname = self.system['capture_prefix'] + self.stamp
        os.makedirs(paths.CAPTURES, exist_ok=True)
        stem = os.path.join(paths.CAPTURES, self.capname)
        self.log_path = stem + '.log'
        self.bits_path, self.live_path, self.start_path = stem + '.bits', stem + '.live', stem + '.start'
        self.stream_start = None
        self.heard = self.decoded_n = self.fixes = 0
        self.t_start = time.time()
        self.stop_reason = ''
        self.last_status = ''
        if self.system is not systems.IRIDIUM:
            return self._start_orbcomm()

        self.parser = self._proc()                       # decoded frames, as they arrive
        self.parser.readyReadStandardOutput.connect(self._parser_output)
        self.parser.setStandardErrorFile(QtCore.QProcess.nullDevice())
        self.parser.start(PY, ['-u', catalog.PARSER, '--no-stats'])
        if not self.parser.waitForStarted(5000):
            self.status.emit('Could not start the decoder (iridium-parser): %s' % self.parser.errorString())
            return

        self.extractor = self._proc()                    # SDR -> raw bursts
        self.extractor.setStandardOutputFile(self.bits_path)
        self.extractor.readyReadStandardError.connect(self._extractor_err)
        self.extractor.finished.connect(self._extractor_finished)
        self.err_rest, self.status_lines = '', 0
        if self.replay:
            self.extractor.start(PY, ['-c', REPLAY_CODE, self.replay])
        else:
            s = self.sdr
            conf = sdr_settings.write_conf(s, os.path.join(paths.DATA, 'sdr.conf'))
            args = [SOURCE, conf, '%g' % s['threshold_db'], '--ppm', '%g' % s['ppm']]
            if self.spectrum_port:
                args += ['--spectrum-port', str(self.spectrum_port)]
            if self.audio_port:
                args += ['--audio-port', str(self.audio_port)]
            args += ['--auto-gain'] * s['auto_gain'] + ['--no-iq'] * (not s['iq_correction']) + \
                ['--no-dc'] * (not s['dc_correction'])
            self.extractor.start(PY, args)
        if not self.extractor.waitForStarted(5000):
            self.status.emit('Could not start iridium-extractor: %s' % self.extractor.errorString())
            self.parser.kill()
            return
        self._started()

    def _start_orbcomm(self):
        """Orbcomm and the ham bands: one process tunes the SDR, decodes and prints one line per packet/frame"""
        s = self.sdr
        self.parser = None
        self.extractor = self._proc()
        self.extractor.readyReadStandardOutput.connect(self._orbcomm_output)
        self.extractor.readyReadStandardError.connect(self._extractor_err)
        self.extractor.finished.connect(self._extractor_finished)
        self.err_rest, self.status_lines = '', 0
        g, auto = sdr_settings.gain(s)
        prog = ([ORBCOMM_RX] if self.system is systems.ORBCOMM else [GNSS_RX] if self.system is systems.GNSS_BAND
                else [GNSS_RX, '--constellation', 'glonass'] if self.system is systems.GLONASS_BAND
                else [GNSS_RX, '--constellation', 'beidou'] if self.system is systems.BEIDOU_BAND
                else [METEOR_RX, '--baseband', self.bits_path[:-5] + '.s16'] if self.system is systems.METEOR
                else [RADIO_RX] if self.system is systems.RADIO
                else [HAM_RX, '--band', self.system['key']])
        self.meteor_snr = None
        args = prog + ['--device', sdr_settings.device_args(s), '--rate', str(sdr_settings.sample_rate(s)),
                '--center', str(sdr_settings.center_hz(s)), '--gain', '%g' % g, '--ppm', '%g' % s['ppm'],
                '--start-file', self.start_path]
        if self.spectrum_port:
            args += ['--spectrum-port', str(self.spectrum_port)]
        if self.audio_port:
            args += ['--audio-port', str(self.audio_port)]
        args += ['--auto-gain'] * auto + ['--no-dc'] * (not s['dc_correction']) + ['--parent-pid', str(os.getpid())]
        args += ['--if-gain', '%g' % sdr_settings.if_gain(s)]
        if prog[0] == GNSS_RX:      # the band's quiet noise, for the jamming watch (Integrity section)
            args += ['--noise-baseline', os.path.join(paths.DATA, 'noise-%s.npz' % self.system['key']),
                     '--spp-state', os.path.join(paths.DATA, 'gnss-solver.json')]
            if catalog.HOME:        # (warm start: which satellites are up, from the orbits kept last time)
                args += ['--home', '%.6f,%.6f,%.4f' % catalog.HOME]
            opt = os.path.join(paths.DATA, 'pocket_trk.opt')
            if os.path.exists(opt):         # pocket_trk's own settings, if tuned (see README)
                args += ['--pocket-opts', opt]
            # pocket_trk's log and RTCM beside the capture (<name>.trk1.log / .rtcm)
            args += ['--work-stem', self.live_path[:-len('.live')]]
            import integrity
            if self.system['key'] in integrity.WATCH_BANDS:     # (it keeps 20 s of samples in memory: ~80 MB)
                args += ['--snap-trigger', snap_trigger()]
        self.extractor.start(PY, args)
        if not self.extractor.waitForStarted(5000):
            self.status.emit('Could not start the %s receiver: %s' % (self.system['label'], self.extractor.errorString()))
            return
        self._started()

    def _started(self):
        try:
            with open(self.bits_path[:-5] + RUNNING, 'w') as f:
                f.write('%d' % os.getpid())
        except OSError:
            pass
        self.bits_pos = 0
        self.disk_check = time.time()
        self.bits_rest = self.out_rest = ''
        self.state = 'running'
        self.timer.start(250)
        self.running.emit(True)
        self.status.emit('Starting %s...' % ('the replay' if self.replay else
                                             '%s on %s' % (self.system['label'], self.sdr['label'])))

    # ---- while running
    def _tick(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        self._feed_bits(now)
        if self.state != 'running':
            return
        if time.time() - self.disk_check > 30:
            self.disk_check = time.time()
            need = storage.settings()['min_free_gb']
            if storage.free_gb() < need:
                self.stop('less than %.1f GB of disk space left' % need)
                return
        if not self.replay and self.system is systems.IRIDIUM and \
                private_bytes(self.extractor.processId()) > LIMIT_GB * 2 ** 30:
            self.stop('decoder used over %d GB of memory (too many false bursts) - raise the threshold' % LIMIT_GB)
            return
        e = int(time.time() - self.t_start)
        if self.system is systems.RADIO:
            self.status.emit('Radio on  %d:%02d:%02d  ·  tune in the Radio section%s'
                             % (e // 3600, e // 60 % 60, e % 60, '  ·  ' + self.last_status if self.last_status else ''))
            return
        if self.system is systems.METEOR:       # nothing is decoded until Stop: the signal is the news
            self.status.emit('Receiving %s %d:%02d:%02d  ·  recording the pass  ·  signal %s  ·  pictures are made '
                             'after Stop' % (self.system['label'], e // 3600, e // 60 % 60, e % 60,
                                             '%+.1f dB' % self.meteor_snr if self.meteor_snr is not None else '-'))
            return
        self.status.emit('Receiving %s %d:%02d:%02d  ·  %d decoded  ·  %d damaged  ·  %d positions%s'
                         % (self.system['label'], e // 3600, e // 60 % 60, e % 60, self.decoded_n, self.heard, self.fixes,
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

    def _extractor_err(self):
        """the decoder's messages: kept in the .log, except that its once-a-second totals are thinned out"""
        text = self.err_rest + bytes(self.extractor.readAllStandardError()).decode(errors='replace')
        lines = text.replace('\r\n', '\n').split('\n')
        self.err_rest = lines.pop()
        keep = []
        for line in lines:
            if re.match(r'^\d{10} \|', line):
                self.last_status = ''          # its per-second totals: it is running fine
                self.status_lines += 1
                if self.status_lines % STATUS_EVERY != 1:
                    continue
            elif re.search(r'DeprecationWarning|setDaemon', line) or not line.strip():
                continue
            else:
                self.last_status = line.strip()[:120]
            keep.append(line)
        if keep:
            with open(self.log_path, 'a') as f:
                f.write('\n'.join(keep) + '\n')

    def _parser_output(self):
        data = bytes(self.parser.readAllStandardOutput()).decode(errors='replace')
        if not data:
            return
        with open(self.live_path, 'a', newline='') as f:
            f.write(data.replace('\r\n', '\n').replace('\n', '\r\n'))
        lines = (self.out_rest + data.replace('\r\n', '\n')).split('\n')
        self.out_rest = lines.pop()             # a line the parser hasn't finished yet
        for line in lines:
            kind = line.split(':', 1)[0]
            if kind:
                self.frame.emit(kind, line)
            if kind in ('RAW', 'ERR'):
                self.heard += 1
            elif kind:
                self.decoded_n += 1
                m = re.search(r' alt=(-?\d+)', line)
                if kind == 'IRA' and m and int(m.group(1)) > 100:     # a real position, not a beam on the ground
                    self.fixes += 1

    def _orbcomm_output(self):
        """Orbcomm packets: OEP (ephemeris = a position), OSY (sync), OPK (other packet), OBAD (failed checksum);
        ham frames: HFR (decoded frame), HSIG (a signal heard but not decoded)"""
        data = bytes(self.extractor.readAllStandardOutput()).decode(errors='replace')
        if not data:
            return
        lines = (self.out_rest + data.replace('\r\n', '\n')).split('\n')
        self.out_rest = lines.pop()
        lines = [l for l in lines if re.match(r'^(O[A-Z]{2,3}|HFR|HSIG|GCH|GEPH|GPOS|GSPP|GGAP|GLOG|MSIG|GTIME|GNOISE|GSPEC): ', l)]
        if not lines:
            return
        with open(self.live_path, 'a', newline='') as f:
            f.write(''.join(l + '\r\n' for l in lines if not l.startswith(('GTIME', 'GNOISE', 'GSPEC'))))
        for line in lines:
            kind = line.split(':', 1)[0]
            if kind in ('GTIME', 'GNOISE', 'GSPEC'):    # (for the Integrity section; not saved, not frames)
                self.gnss_line.emit(kind, line)
            if kind in ('GNOISE', 'GSPEC'):
                continue
            if kind == 'GTIME':         # GNSS: GPS time minus the PC clock (Settings > PC clock); not saved
                try:
                    f = line.split()
                    self.gps_clock = {'t': float(f[1]), 'offset': float(f[2]), 'nsat': int(f[3]),
                                      'spread': float(f[4])}
                except (IndexError, ValueError):
                    pass
                continue
            if kind == 'MSIG':          # Meteor: its signal strength (not a frame)
                try:
                    self.meteor_snr = float(line.split()[2])
                except (IndexError, ValueError):
                    pass
                continue
            self.frame.emit(kind, line)
            if kind in ('GGAP', 'GLOG'):    # receiver events (a sample gap filled, pocket_trk restarted): not frames
                continue
            if kind in DAMAGED:
                self.heard += 1
            else:
                self.decoded_n += 1
                self.fixes += kind in ('OEP', 'GPOS')

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
            self._extractor_err()
            self.stop_reason = self.last_status or 'the decoder stopped by itself'
            if 'Failed to open' in self.stop_reason or 'usb_claim' in self.stop_reason:
                self.stop_reason = ('the SDR (%s) could not be opened - is it plugged in, and are SDR++, SatDump '
                                    % self.sdr['device'] +
                                    'and other receivers (like the Satellite Console) closed?')
        if self.state != 'stopping':
            return
        self.timer.stop()
        if self.parser is None:         # Orbcomm: nothing to wait for
            self._orbcomm_output()
            return self._parser_finished()
        self._feed_bits(datetime.datetime.now(datetime.timezone.utc))
        self.parser.finished.connect(self._parser_finished)
        self.parser.closeWriteChannel()       # let the parser finish what it was given
        QtCore.QTimer.singleShot(15000, lambda p=self.parser: p.state() != QtCore.QProcess.NotRunning and p.kill())

    def _received_file(self):
        """the file that shows whether anything was received: Iridium's raw bursts, Meteor's recording, the others'
        decoded lines"""
        if self.parser is not None:
            return self.bits_path
        return self.bits_path[:-5] + '.s16' if self.system is systems.METEOR else self.live_path

    def _clear_running(self):
        """receiving has ended (the SDR is free; the capture may still be going into the catalog)"""
        try:
            os.remove(self.bits_path[:-5] + RUNNING)
        except (OSError, AttributeError):
            pass

    def _parser_finished(self, *_):
        self._clear_running()
        if self.parser is not None:
            self._parser_output()
        if self.system is systems.RADIO:        # listening only: nothing to keep
            self._remove_capture()
            try:
                os.remove(self.log_path)
            except OSError:
                pass
            self.state = 'idle'
            self.running.emit(False)
            self.status.emit('Radio stopped%s' % (': ' + self.stop_reason if self.stop_reason else ''))
            return
        got = self._received_file()
        size = os.path.getsize(got) if os.path.exists(got) else 0
        if size < 200:      # nothing was received: don't leave empty files behind
            self._remove_capture()
            self.state = 'idle'
            self.running.emit(False)
            self.status.emit('Receiver stopped%s - nothing was received' % (': ' + self.stop_reason if self.stop_reason else ''))
            return
        self.state = 'decoding'
        meteor = self.system is systems.METEOR
        self.status.emit('Stopped%s. %s' % (' (' + self.stop_reason + ')' if self.stop_reason else '',
                                            'Making the pictures with SatDump (a minute or two)...' if meteor else
                                            'Adding the capture to the catalog...'))
        self.decoder = self._proc()
        self.decoder.finished.connect(self._decoder_finished)
        self.decoder.setStandardOutputFile(QtCore.QProcess.nullDevice())
        self.decoder.setStandardErrorFile(QtCore.QProcess.nullDevice())
        self.decoder.start(PY, [IMAGES_PY, '--decode', self.capname] if meteor else
                           [os.path.join(HERE, 'catalog.py'), '--ready=' + self.capname])

    def _decoder_finished(self, *_):
        self.state = 'idle'
        self.running.emit(False)
        ok = self.decoder.exitCode() == 0
        if self.system is systems.METEOR:
            import images
            try:
                meta = json.load(open(os.path.join(images.IMAGES, self.capname, 'pass.json')))
                text = '%d pictures - see Images' % meta['images'] if meta.get('images') else meta.get('note', '')
            except (OSError, ValueError):
                text = 'the pictures could not be made (SatDump)'
            self.status.emit('Meteor pass %s: %s%s' % (self.capname, text, ' - stopped: ' + self.stop_reason
                                                         if self.stop_reason else ''))
            self.decoded.emit()
            return
        self.status.emit('Capture %s %s (%d decoded, %d positions)%s'
                         % (self.capname, 'added to the catalog' if ok else 'could not be decoded - run 2-decode.bat',
                            self.decoded_n, self.fixes, ' - stopped: ' + self.stop_reason if self.stop_reason else ''))
        if ok:
            self.decoded.emit()

    def shutdown(self):
        """the app is closing: stop the SDR now and decode the capture in the background"""
        if self.state == 'running':
            subprocess.run(['taskkill', '/PID', str(self.extractor.processId()), '/T', '/F'],
                           capture_output=True, creationflags=NO_WINDOW)
            self._clear_running()
            self.timer.stop()
            if self.parser is not None:
                self._feed_bits(datetime.datetime.now(datetime.timezone.utc))
                self.parser.closeWriteChannel()
                self.parser.waitForFinished(15000)
                self._parser_output()
                self.parser.kill()
            got = self._received_file()
            if os.path.exists(got) and os.path.getsize(got) >= 200:
                subprocess.Popen([PY, IMAGES_PY, '--decode', self.capname] if self.system is systems.METEOR else
                                 [PY, os.path.join(HERE, 'catalog.py'), '--ready=' + self.capname],
                                 cwd=HERE, creationflags=NO_WINDOW)
            else:
                self._remove_capture()

    def _remove_capture(self):
        """delete the files of a run that received nothing - except its log, which says what the receiver
        heard (e.g. Orbcomm's per-channel levels), for working out why (storage removes it with other old files)"""
        for ext in ('.bits', '.live', '.start', '.s16', RUNNING):
            try:
                os.remove(self.bits_path[:-5] + ext)
            except OSError:
                pass


# for testing without the SDR: copies an old capture to stdout at about 200 lines a second
REPLAY_CODE = '''
import sys, time
for i, line in enumerate(open(sys.argv[1], errors="replace")):
    sys.stdout.write(line); sys.stdout.flush()
    if i % 20 == 19: time.sleep(0.1)
time.sleep(3600)
'''
