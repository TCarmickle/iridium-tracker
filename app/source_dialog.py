"""Source panel: which satellite system to receive, which SDR, and how it is tuned (like SDR++'s Source menu)."""
import os

from PyQt5 import QtCore, QtWidgets

import sdr_settings as sdr
import systems
import theme

NETWORK = 'rtl_tcp=192.168.1.10:1234'
TIPS = {
    'rate': 'Wider shows and decodes more of the Iridium band (1616-1626.5 MHz). RTL-SDRs: 2.0-2.5 MS/s. '
            'HackRF / Airspy / bladeRF: up to 10-20 MS/s for the whole band. Must be a multiple of 250 kHz.',
    'center': 'Automatic keeps the top of the band - the ring-alert and messaging channels at 1626.0-1626.5 MHz, '
              'where satellite positions come from - inside the received slice.',
    'threshold': 'How far over the noise a burst must be to be decoded. Lower finds weaker bursts but floods the '
                 'decoder with false ones; below 14 dB it can run the PC out of memory, so 14 is the minimum.',
}


class _Find(QtCore.QThread):
    found = QtCore.pyqtSignal(list)

    def run(self):
        self.found.emit(sdr.discover())


# device searches still running. A search can outlive its window (closing it doesn't wait for the search),
# and a running QThread whose Python object is garbage-collected takes the whole app down - so each one is
# kept here, owned by no window, until it has finished.
_SEARCHES = set()

class SourceDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, receiving=False):
        super().__init__(parent)
        self.setWindowTitle('SDR source')
        self.s = sdr.load()
        lay = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        lay.addLayout(form)

        # which system: one SDR listens to one band at a time
        self.system = QtWidgets.QComboBox()
        for s in systems.ORDER:
            self.system.addItem('%s  (%s)' % (s['label'], systems.band_tag(s)), s['key'])
        self.system.setCurrentIndex(max(0, self.system.findData(self.s['system'])))
        self.system.setToolTip('One SDR receives one satellite system at a time. Every system you have received '
                               'is still shown on the map and in the list.')
        self.gains = self._gains(self.s)
        self.shown_system = self.s['system']
        form.addRow('Receive:', self.system)
        self.antenna = QtWidgets.QLabel()
        self.antenna.setWordWrap(True)
        theme.style(self.antenna, 'color: %(WARN)s;')
        form.addRow('', self.antenna)

        # device
        row = QtWidgets.QHBoxLayout()
        self.device = QtWidgets.QComboBox()
        self.device.setMinimumWidth(320)
        self.device.activated.connect(self._picked)
        self.refresh = QtWidgets.QPushButton('Refresh')
        self.refresh.clicked.connect(self.find)
        row.addWidget(self.device, 1)
        row.addWidget(self.refresh)
        form.addRow('Device:', row)
        self.args = QtWidgets.QLineEdit(self.s['device'])
        self.args.setToolTip('gr-osmosdr device string, e.g. rtl=0, rtl=1, rtl_tcp=host:port, hackrf=0, airspy=0, '
                             'soapy=0,driver=sdrplay')
        form.addRow('Device string:', self.args)

        # tuning
        self.rate = QtWidgets.QComboBox()
        for r in sdr.RATES:
            self.rate.addItem('%g MS/s' % (r / 1e6), r)
        self.rate.setCurrentIndex(max(0, self.rate.findData(self.s['sample_rate'])))
        self.rate.setToolTip(TIPS['rate'])
        self.rate.currentIndexChanged.connect(self._update_center)
        form.addRow('Sample rate:', self.rate)
        row = QtWidgets.QHBoxLayout()
        self.center_auto = QtWidgets.QCheckBox('Automatic')
        self.center_auto.setChecked(self.s['center_mhz'] <= 0)
        self.center = QtWidgets.QDoubleSpinBox()
        self.center.setRange(1000, 2200)
        self.center.setDecimals(3)
        self.center.setSuffix(' MHz')
        self.center.setValue(self.s['center_mhz'] if self.s['center_mhz'] > 0 else sdr.center_hz(self.s) / 1e6)
        self.center_auto.toggled.connect(self._update_center)
        self.center.setToolTip(TIPS['center'])
        self.center_auto.setToolTip(TIPS['center'])
        row.addWidget(self.center)
        row.addWidget(self.center_auto)
        row.addStretch(1)
        form.addRow('Frequency:', row)
        self.tuning_rows = [form.labelForField(self.rate), self.rate, form.labelForField(row),
                            self.center, self.center_auto]      # Iridium's: Orbcomm's are fixed
        self.coverage = QtWidgets.QLabel()
        theme.style(self.coverage, 'color: %(MUTED)s;')
        form.addRow('', self.coverage)

        # gain
        row = QtWidgets.QHBoxLayout()
        self.gain = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.gain.setRange(0, 500)
        self.gain.setValue(int(self.s['gain'] * 10))
        self.gain_lbl = QtWidgets.QLabel()
        self.gain_lbl.setMinimumWidth(56)
        self.gain.valueChanged.connect(lambda v: self.gain_lbl.setText('%.1f dB' % (v / 10)))
        self.gain_lbl.setText('%.1f dB' % self.s['gain'])
        self.auto_gain = QtWidgets.QCheckBox('Auto')
        self.auto_gain.setChecked(self.s['auto_gain'])
        self.auto_gain.toggled.connect(lambda on: self.gain.setEnabled(not on))
        self.gain.setEnabled(not self.s['auto_gain'])
        row.addWidget(self.gain, 1)
        row.addWidget(self.gain_lbl)
        row.addWidget(self.auto_gain)
        form.addRow('Gain:', row)
        row = QtWidgets.QHBoxLayout()
        self.if_gain = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.if_gain.setRange(0, 70)
        self.if_gain.setValue(int(sdr.if_gain(self.s)))
        self.if_gain_lbl = QtWidgets.QLabel('%d dB' % sdr.if_gain(self.s))
        self.if_gain_lbl.setMinimumWidth(56)
        self.if_gain.valueChanged.connect(lambda v: self.if_gain_lbl.setText('%d dB' % v))
        self.if_gain.setToolTip('The tuner\'s second amplifier (E4000 tuners; others ignore it). Too low and weak\n'
                                'signals are lost in the 8-bit rounding: watch the "Input" level next to the spectrum -\n'
                                'it should read roughly 5-30%. The Tune gain button finds the best value for a band.')
        row.addWidget(self.if_gain, 1)
        row.addWidget(self.if_gain_lbl)
        self.tune_btn = QtWidgets.QPushButton('Tune gain')
        self.tune_btn.setToolTip('About 10 seconds: steps the IF gain for this band and picks the one that fills\n'
                                 'the SDR\'s converter most without clipping (the receiver must be stopped).\n'
                                 'Run it with the band\'s antenna connected - its signals set how much room is needed.')
        self.tune_btn.clicked.connect(self._tune)
        row.addWidget(self.tune_btn)
        form.addRow('IF gain:', row)
        self.tune_note = QtWidgets.QLabel()
        self.tune_note.setWordWrap(True)
        theme.style(self.tune_note, 'color: %(MUTED)s;')
        self.tune_note.hide()
        form.addRow('', self.tune_note)
        self.tuner = None
        self.ppm = QtWidgets.QDoubleSpinBox()
        self.ppm.setRange(-200, 200)
        self.ppm.setDecimals(1)
        self.ppm.setSuffix(' ppm')
        self.ppm.setValue(self.s['ppm'])
        form.addRow('PPM correction:', self.ppm)

        # options
        self.bias = QtWidgets.QCheckBox('Bias-T (power an active antenna / LNA through the coax)')
        self.bias.setChecked(self.s['bias_t'])
        self.offset = QtWidgets.QCheckBox('Offset tuning (E4000 tuners: moves the spike at the centre frequency off the signal)')
        self.offset.setChecked(self.s['offset_tune'])
        self.offset.setToolTip('E4000 tuners (like the SMArTee XTR) put a strong spike exactly at the frequency they are tuned to.\n'
                               'Offset tuning tunes slightly to one side and shifts back digitally, so the spike lands away\n'
                               'from the signal. Most useful for GNSS, whose signal is centred on that frequency.\n'
                               'Harmless for the other bands. Applies the next time the receiver starts.')
        self.iq = QtWidgets.QCheckBox('IQ correction (automatic - can create mirror images of bursts; best left off)')
        self.iq.setChecked(self.s['iq_correction'])
        self.dc = QtWidgets.QCheckBox('DC offset correction')
        self.dc.setChecked(self.s['dc_correction'])
        box = QtWidgets.QVBoxLayout()
        for c in (self.bias, self.offset, self.iq, self.dc):
            box.addWidget(c)
        form.addRow('Options:', box)
        self.extra = QtWidgets.QLineEdit(self.s['extra_args'])
        self.extra.setPlaceholderText('e.g. buffers=32 (added to the device string)')
        form.addRow('Extra device options:', self.extra)
        self.threshold = QtWidgets.QDoubleSpinBox()
        self.threshold.setRange(sdr.MIN_THRESHOLD, 40)
        self.threshold.setDecimals(0)
        self.threshold.setSuffix(' dB over noise')
        self.threshold.setValue(self.s['threshold_db'])
        self.threshold.setToolTip(TIPS['threshold'])
        form.addRow('Burst threshold:', self.threshold)
        self.threshold_row = (form.labelForField(self.threshold), self.threshold)

        self.note = QtWidgets.QLabel('<span style="color:#ffb454">The receiver is running - changes apply the next '
                                     'time you start it.</span>' if receiving else '')
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        defaults = btns.addButton('Defaults', QtWidgets.QDialogButtonBox.ResetRole)
        defaults.clicked.connect(self.defaults)
        btns.accepted.connect(self.save)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self.device.addItem('%s  (saved)' % self.s['label'], (self.s['label'], self.s['device']))
        self._add_fixed()
        self.system.currentIndexChanged.connect(self._system_changed)
        self._system_changed()
        self._update_center()
        self.find()
        self.resize(560, 0)

    # ---- devices
    def _add_fixed(self):
        self.device.insertSeparator(self.device.count())
        self.device.addItem('RTL-TCP over the network...', ('RTL-TCP', NETWORK))
        self.device.addItem('Custom (type a device string)', ('Custom', None))

    def find(self):
        self.refresh.setEnabled(False)
        self.refresh.setText('Searching...')
        job = _Find()
        _SEARCHES.add(job)
        job.finished.connect(lambda j=job: _SEARCHES.discard(j))
        job.found.connect(self._found)
        self.job = job
        job.start()

    def done(self, result):
        try:            # a search that finishes after the window is closed has nothing to fill in
            self.job.found.disconnect(self._found)
        except (TypeError, RuntimeError):
            pass
        super().done(result)

    def _found(self, devices):
        if not self.isVisible():
            return
        current = self.args.text().strip()
        self.device.clear()
        for label, args in devices:
            self.device.addItem('%s   [%s]' % (label, args), (label, args))
        if not devices:
            self.device.addItem('No SDR found - plugged in? driver installed with Zadig?', (self.s['label'], current))
        i = next((k for k in range(self.device.count())
                  if self.device.itemData(k) and self.device.itemData(k)[1] == current), None)
        if i is None:       # the saved device isn't plugged in (or is a network / custom source): keep showing it
            self.device.insertItem(0, '%s   [%s]  (saved)' % (self.s['label'], current), (self.s['label'], current))
            i = 0
        self._add_fixed()
        self.device.setCurrentIndex(i)
        self.refresh.setEnabled(True)
        self.refresh.setText('Refresh')

    def _picked(self, i):
        label, args = self.device.itemData(i)
        if args:
            self.args.setText(args)
        if label in ('RTL-TCP', 'Custom'):
            self.args.setFocus()
            self.args.selectAll()

    # ---- which system
    @staticmethod
    def _gains(s):
        """band -> (gain, auto, IF gain) from saved settings"""
        return {sy['key']: sdr.gain(dict(s, system=sy['key'])) + (sdr.if_gain(dict(s, system=sy['key'])),)
                for sy in systems.ORDER}

    def _system_changed(self, *_):
        """each band keeps its own gain; every band but Iridium has its rate and frequency set by its decoders"""
        self.gains[self.shown_system] = (self.gain.value() / 10, self.auto_gain.isChecked(), self.if_gain.value())
        key = self.system.currentData()
        self.shown_system = key
        g, auto, ifg = self.gains[key]
        self.gain.setValue(int(g * 10))
        self.auto_gain.setChecked(auto)
        self.if_gain.setValue(int(ifg))
        fixed = key != 'iridium'
        for w in self.tuning_rows:
            if w is not None:
                w.setVisible(not fixed)
        if not fixed:
            self.center.setEnabled(not self.center_auto.isChecked())
        for w in self.threshold_row:
            if w is not None:
                w.setVisible(not fixed)       # the burst threshold is Iridium's decoder only
        self.antenna.setText('Antenna for %s: %s.' % (systems.get(key)['label'], systems.get(key)['antenna']))
        self._update_center()

    # ---- tuning
    def _values(self):
        label = self.device.currentData()[0] if self.device.currentData() else self.s['label']
        key = self.system.currentData()
        gains = dict(self.gains)
        gains[key] = (self.gain.value() / 10, self.auto_gain.isChecked(), float(self.if_gain.value()))
        per_band = {}
        for k, (g, auto, ifg) in gains.items():
            if k == 'iridium':
                per_band.update(gain=g, auto_gain=auto, if_gain=ifg)
            else:
                per_band.update({k + '_gain': g, k + '_auto_gain': auto, k + '_if_gain': ifg})
        return dict(self.s, system=key, device=self.args.text().strip() or 'rtl=0', label=label or 'SDR',
                    sample_rate=self.rate.currentData(), center_mhz=0.0 if self.center_auto.isChecked() else self.center.value(),
                    ppm=self.ppm.value(), **per_band,
                    bias_t=self.bias.isChecked(), offset_tune=self.offset.isChecked(), iq_correction=self.iq.isChecked(),
                    dc_correction=self.dc.isChecked(), threshold_db=max(sdr.MIN_THRESHOLD, self.threshold.value()),
                    extra_args=self.extra.text().strip())

    def _update_center(self):
        v = self._values()
        if sdr.fixed_tuning(v):
            c, half = sdr.center_hz(v) / 1e6, sdr.sample_rate(v) / 2e6
            what = {'orbcomm': 'every Orbcomm downlink channel (137.2 - 137.8 MHz)',
                    'ham2m': 'the 2 m satellite subband (145.8 - 146.0 MHz), with the ISS',
                    'ham70': 'most of the 70 cm satellite subband (435 - 438 MHz)',
                    'gnss1': 'GPS L1, Galileo E1 and WAAS (1575.42 MHz)',
                    'glo1': 'GLONASS L1, frequency channels -2..+2',
                    'bds1': 'BeiDou B1I (1561.098 MHz): every BeiDou satellite'}.get(v['system'], '')
            self.coverage.setText('Receives %.3f - %.3f MHz at %.4f MS/s: %s'
                                  % (c - half, c + half, sdr.sample_rate(v) / 1e6, what))
            return
        self.center.setEnabled(not self.center_auto.isChecked())
        if self.center_auto.isChecked():
            self.center.setValue(sdr.center_hz(v) / 1e6)
        c, half = sdr.center_hz(v) / 1e6, v['sample_rate'] / 2e6
        lo, hi = c - half, c + half
        part = max(0.0, min(hi, 1626.5) - max(lo, 1616.0))
        self.coverage.setText('Receives %.3f - %.3f MHz: %.1f of the 10.5 MHz Iridium band%s'
                              % (lo, hi, part, '' if lo <= 1626.0 and hi >= 1626.5 else
                                 '  -  ring-alert channels NOT included'))

    def defaults(self):
        self.s = dict(sdr.DEFAULTS, device=self.args.text().strip() or 'rtl=0', label=self.s['label'],
                      system=self.system.currentData())
        d = self.s
        self.gains = self._gains(d)
        g, auto, ifg = self.gains[self.shown_system]
        self.gain.setValue(int(g * 10))
        self.auto_gain.setChecked(auto)
        self.if_gain.setValue(int(ifg))
        self.rate.setCurrentIndex(self.rate.findData(d['sample_rate']))
        self.center_auto.setChecked(True)
        self.ppm.setValue(d['ppm'])
        self.bias.setChecked(d['bias_t'])
        self.offset.setChecked(d['offset_tune'])
        self.iq.setChecked(d['iq_correction'])
        self.dc.setChecked(d['dc_correction'])
        self.extra.setText('')
        self.threshold.setValue(d['threshold_db'])
        self._update_center()

    def save(self):
        v = self._values()
        try:
            sdr.decimation(v['sample_rate'])
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, 'Sample rate', str(e))
            return
        sdr.save(v)
        self.result = v
        self.accept()

    # ---- IF gain tuning (gain_tune.py)
    def _tune(self):
        import receiver
        if self.tuner is not None:
            return
        rx = getattr(self.parent(), 'receiver', None)
        if (rx is not None and rx.busy) or receiver.active_capture():
            self._tune_say('Stop the receiver first - the tuner needs the SDR.')
            return
        v = self._values()
        g, auto = sdr.gain(v)
        args = [os.path.join(receiver.HERE, 'gain_tune.py'), '--device', sdr.device_args(v),
                '--rate', str(sdr.sample_rate(v)), '--center', str(sdr.center_hz(v)), '--gain', '%g' % g,
                '--ppm', '%g' % v['ppm']] + ['--auto-gain'] * auto
        self.tuner = QtCore.QProcess(self)
        if rx is not None:
            self.tuner.setProcessEnvironment(rx.env)
        self.tuner.setWorkingDirectory(receiver.HERE)
        self.tuner_out, self.tuner_steps = '', []
        self.tuner.readyReadStandardOutput.connect(self._tune_output)
        self.tuner.finished.connect(self._tune_done)
        self.tune_btn.setEnabled(False)
        self._tune_say('Tuning %s: measuring the input level at each IF gain...' % systems.get(v['system'])['label'])
        self.tuner.start(receiver.PY, args)

    def _tune_say(self, text):
        self.tune_note.setText(text)
        self.tune_note.show()

    def _tune_output(self):
        self.tuner_out += bytes(self.tuner.readAllStandardOutput()).decode(errors='replace')
        lines = self.tuner_out.split('\n')
        self.tuner_out = lines.pop()
        for line in lines:
            f = line.split()
            if f[:1] == ['STEP'] and len(f) >= 4:
                self.tuner_steps.append('%s dB: %s%%%s' % (f[1], f[2], ' (clips)' if float(f[3]) > 0.01 else ''))
                self._tune_say('IF gain - input level: ' + ',  '.join(self.tuner_steps))
            elif f[:1] == ['BEST'] and len(f) >= 3:
                self.if_gain.setValue(int(float(f[1])))
                self.gains[self.shown_system] = (self.gain.value() / 10, self.auto_gain.isChecked(),
                                                 self.if_gain.value())
                self._tune_say('IF gain - input level: %s.<br><b>Best: %s dB (input %s%%)</b> - press Save to keep it.'
                               % (',  '.join(self.tuner_steps), f[1], f[2]))
            elif f[:1] == ['ERROR']:
                self._tune_say('Could not tune: ' + line[6:])

    def _tune_done(self, *_):
        self._tune_output()
        self.tune_btn.setEnabled(True)
        if not self.tuner_steps and 'Best' not in self.tune_note.text():
            err = bytes(self.tuner.readAllStandardError()).decode(errors='replace').strip().splitlines()
            self._tune_say('Could not open the SDR%s' % (': ' + err[-1] if err else ''))
        self.tuner = None

