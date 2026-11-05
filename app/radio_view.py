"""The Radio section: listen anywhere the SDR reaches, like SDR++ - the full-size spectrum and waterfall, a big
frequency display (scroll or click any digit to change it), the mode, and the bandwidth (also by dragging the
edges of the yellow band on the spectrum). It uses the tracker's receiver on the Radio band (radio_rx.py), which
the SDR follows when the frequency leaves the received slice. On another band, the frequency can move only
within that band's slice.
"""
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import QPointF, QRectF, Qt

import sdr_settings
import sound
import systems
import theme

DIGITS = 10                 # up to 9.999 999 999 GHz
MIN_HZ, MAX_HZ = 0.0, 3000e6            # what the digits go to
SDR_LO, SDR_HI = 24e6, 2300e6           # what tuners like the R820T / E4000 reach (radio_rx.py keeps it in here)
PRESETS = [                 # (label, MHz, mode, bandwidth kHz)
    ('FM broadcast radio', 100.0, 'wfm', 180),
    ('NOAA Weather Radio', 162.475, 'nfm', 12.5),
    ('Aircraft - emergency (121.5)', 121.5, 'am', 8),
    ('Meteor-M pictures (137.9)', 137.9, 'nfm', 120),
    ('Orbcomm (137.46)', 137.46, 'nfm', 10),
    ('ISS APRS (145.825)', 145.825, 'nfm', 12.5),
    ('2 m ham calling (146.52)', 146.52, 'nfm', 12.5),
    ('Marine channel 16 (156.8)', 156.8, 'nfm', 12.5),
    ('Iridium ring alerts (1626.27)', 1626.2708, 'am', 36),
]


class FreqDisplay(QtWidgets.QWidget):
    """the frequency in big digits, SDR++ style: scroll over a digit (or click its top / bottom half) to step it"""
    valueChanged = QtCore.pyqtSignal(float)

    def __init__(self):
        super().__init__()
        self.hz = 100e6
        self.hover = None
        self.setMouseTracking(True)
        self.setMinimumHeight(64)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip('Scroll over a digit to change it (or click its top half for up, bottom half for down).\n'
                        'Each digit steps its own place: 1 MHz, 100 kHz, 10 kHz ... 1 Hz.')
        self.font_ = QtGui.QFont('Consolas', 34, QtGui.QFont.Bold)
        self.wheel_acc = 0

    def set_value(self, hz):
        hz = float(min(max(hz, MIN_HZ), MAX_HZ))
        if abs(hz - self.hz) >= 0.5:
            self.hz = hz
            self.update()

    def sizeHint(self):
        fm = QtGui.QFontMetrics(self.font_)
        return QtCore.QSize(fm.horizontalAdvance('0') * (DIGITS + 4) + 80, fm.height() + 12)

    def _layout(self):
        """[(x, width, place)] for each digit, left to right (place = its power of ten), and the dot positions"""
        fm = QtGui.QFontMetrics(self.font_)
        dw, sep = fm.horizontalAdvance('0'), fm.horizontalAdvance('.') + 2
        out, x = [], 8.0
        for i in range(DIGITS):
            place = DIGITS - 1 - i
            out.append((x, dw, place))
            x += dw
            if place in (9, 6, 3):
                x += sep
        return out, x

    def _digit_at(self, x):
        for dx, dw, place in self._layout()[0]:
            if dx <= x < dx + dw:
                return place
        return None

    def _step(self, place, n):
        self.set_value(round(self.hz) + n * 10 ** place)
        self.valueChanged.emit(self.hz)

    def wheelEvent(self, e):
        place = self._digit_at(e.pos().x())
        if place is None:
            return
        self.wheel_acc += e.angleDelta().y()
        clicks = int(self.wheel_acc / 120)
        if clicks:
            self.wheel_acc -= clicks * 120
            self._step(place, clicks)

    def mousePressEvent(self, e):
        place = self._digit_at(e.pos().x())
        if place is not None and e.button() == Qt.LeftButton:
            self._step(place, 1 if e.pos().y() < self.height() / 2 else -1)

    def mouseMoveEvent(self, e):
        place = self._digit_at(e.pos().x())
        if place != self.hover:
            self.hover = place
            self.update()

    def leaveEvent(self, e):
        self.hover = None
        self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor('#0e1116'))
        p.setFont(self.font_)
        fm = QtGui.QFontMetrics(self.font_)
        h = self.height()
        base = (h + fm.ascent() - fm.descent()) / 2
        text = '%0*d' % (DIGITS, int(round(self.hz)))
        lead = len(text) - len(text.lstrip('0'))
        digits, end = self._layout()
        for i, (x, dw, place) in enumerate(digits):
            if place == self.hover:
                p.fillRect(QRectF(x - 1, 4, dw + 2, h - 8), QtGui.QColor(255, 210, 60, 40))
                p.setPen(QtGui.QColor(255, 210, 60, 120))
                p.drawLine(QPointF(x, h / 2), QPointF(x + dw, h / 2))
            p.setPen(QtGui.QColor('#4a5263') if i < lead else QtGui.QColor('#ffd23c'))
            p.drawText(QPointF(x, base), text[i])
            if place in (9, 6, 3):
                p.setPen(QtGui.QColor('#8b93a1'))
                p.drawText(QPointF(x + dw + 1, base), '.')
        small = QtGui.QFont(self.font_)
        small.setPointSize(13)
        p.setFont(small)
        p.setPen(QtGui.QColor('#8b93a1'))
        p.drawText(QPointF(end + 8, base), 'Hz')


class RadioView(QtWidgets.QWidget):
    def __init__(self, tracker):
        super().__init__()
        self.tracker = tracker
        self.player = tracker.sound
        self.panel = tracker.spectrum

        self.freq = FreqDisplay()
        self.freq.set_value(sdr_settings.load()['radio_freq_mhz'] * 1e6)
        self.freq.valueChanged.connect(self.set_frequency)
        self.entry = QtWidgets.QLineEdit()
        self.entry.setPlaceholderText('Type MHz, Enter')
        self.entry.setFixedWidth(130)
        self.entry.setToolTip('Type a frequency in MHz (e.g. 162.475) and press Enter')
        self.entry.returnPressed.connect(self._typed)
        self.presets = QtWidgets.QComboBox()
        self.presets.addItem('Quick picks...')
        for label, mhz, mode, bw in PRESETS:
            self.presets.addItem('%s  -  %s' % (label, mode.upper()), (mhz, mode, bw))
        self.presets.activated.connect(self._preset)
        self.start_btn = QtWidgets.QPushButton()
        self.start_btn.setMinimumWidth(170)
        self.start_btn.clicked.connect(self._start_stop)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.freq)
        top.addSpacing(10)
        col = QtWidgets.QVBoxLayout()
        col.addWidget(self.entry)
        col.addWidget(self.presets)
        top.addLayout(col)
        top.addStretch(1)
        top.addWidget(self.start_btn)

        # mode and bandwidth
        self.mode_btns = {}
        modes = QtWidgets.QHBoxLayout()
        modes.addWidget(QtWidgets.QLabel('Mode'))
        group = QtWidgets.QButtonGroup(self)
        group.setExclusive(True)
        for d in sound.DEMODS:
            b = QtWidgets.QPushButton(d.upper())
            b.setCheckable(True)
            b.setMinimumWidth(52)
            theme.style(b, 'QPushButton:checked { background: %(SEL_BG)s; border: 1px solid %(ACCENT)s; '
                           'color: %(TEXT)s; font-weight: 600; }')
            b.clicked.connect(lambda _, d=d: self.player.set_demod(demod=d))
            group.addButton(b)
            self.mode_btns[d] = b
            modes.addWidget(b)
        modes.addSpacing(18)
        modes.addWidget(QtWidgets.QLabel('Bandwidth'))
        self.bw_slider = QtWidgets.QSlider(Qt.Horizontal)
        self.bw_slider.setRange(0, 1000)            # logarithmic: 100 Hz .. 250 kHz
        self.bw_slider.setMinimumWidth(220)
        self.bw_slider.setToolTip('How wide the channel you hear is - the yellow band on the spectrum.\n'
                                  'You can also drag the yellow band\'s edges on the spectrum.')
        self.bw_slider.valueChanged.connect(self._bw_slid)
        self.bw_slider.sliderReleased.connect(lambda: self.player.set_demod(bandwidth=self._slider_hz()))
        self.bw = QtWidgets.QDoubleSpinBox()
        self.bw.setRange(0.1, 250)
        self.bw.setDecimals(1)
        self.bw.setSuffix(' kHz')
        self.bw.setStepType(QtWidgets.QAbstractSpinBox.AdaptiveDecimalStepType)
        self.bw.setKeyboardTracking(False)
        self.bw.valueChanged.connect(lambda v: self.player.set_demod(bandwidth=v * 1e3))
        modes.addWidget(self.bw_slider)
        modes.addWidget(self.bw)
        modes.addStretch(1)

        self.note = QtWidgets.QLabel()
        self.note.setWordWrap(True)
        theme.style(self.note, 'color: %(MUTED)s;')

        self.area = QtWidgets.QVBoxLayout()         # the spectrum panel goes here while this section is shown
        self.area.setContentsMargins(0, 0, 0, 0)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addLayout(top)
        lay.addLayout(modes)
        lay.addWidget(self.note)
        lay.addLayout(self.area, 1)

        self.pending_hz = None          # Radio starts here once the band being received has stopped
        self.switch_note = ''
        self.want_hz = None             # Radio started for this frequency: set once the SDR is there
        self.pending_mode = None        # a quick pick's (mode, bandwidth), set once Radio is on
        self.range_note = None          # a frequency asked for beyond what the SDR reaches
        self.save_timer = QtCore.QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self._save)
        self.player.demodChanged.connect(self._show_demod)
        tracker.receiver.running.connect(self._rx_changed)
        sync = QtCore.QTimer(self)
        sync.timeout.connect(self._sync)
        sync.start(250)
        self._show_demod()
        self._rx_changed(tracker.receiver.state == 'running')

    def take_spectrum(self, panel):
        self.area.addWidget(panel, 1)
        panel.view.pan_retune = self._pan           # (Tracker.show_section clears it in the other sections)

    # ---- the receiver
    def _on_radio(self):
        r = self.tracker.receiver
        return r.state == 'running' and getattr(r, 'system', None) is systems.RADIO

    def _rx_changed(self, on):
        r = self.tracker.receiver
        self.player.force_system = 'radio' if on and getattr(r, 'system', None) is systems.RADIO else None
        # Radio just started: hold on to this frequency until the spectrum shows the SDR really is there (frames
        # from the band before can still arrive, and would otherwise move the channel back into that band)
        self.want_hz = min(max(self.freq.hz, SDR_LO), SDR_HI) if self.player.force_system else None
        self._sync()

    def _start_stop(self):
        tr, r = self.tracker, self.tracker.receiver
        if r.state == 'running':
            r.stop()
            return
        if r.busy:
            return
        self._start_radio()

    def _start_radio(self):
        tr = self.tracker
        self._save()
        i = tr.rx_system.findData(systems.RADIO['key'])
        if tr.rx_system.currentIndex() != i:
            tr.rx_system.setCurrentIndex(i)
        self.player.listen_hz = self.freq.hz
        tr.toggle_receiver()

    # ---- tuning
    def set_frequency(self, hz):
        """listen at hz: within the received slice the channel moves; on Radio the SDR follows when it leaves it"""
        hz = float(min(max(hz, MIN_HZ), MAX_HZ))
        self.range_note = hz if not SDR_LO <= hz <= SDR_HI else None      # (asked for; the SDR stops at its end)
        hz = min(max(hz, SDR_LO), SDR_HI)
        self.want_hz = None
        self.freq.set_value(hz)
        r, v = self.tracker.receiver, self.panel.view
        if self.panel.sound.currentIndex() != 2:        # the sound on Tuned (the channel can be heard)
            self.panel.sound.setCurrentIndex(2)
        center = self.player.center or (v.center if v.n else None)
        rate = v.rate if v.n else systems.RADIO['rate']
        if self._on_radio():
            if center is None or abs(hz - center) > 0.4 * rate:
                self.player.listen_hz = hz
                v.listen_hz = hz
                self.player.tune_center(hz + systems.RADIO_OFFSET_HZ)
            else:
                v.listen_hz = hz
                self.player.set_mode('tuned', hz)
            self.save_timer.start(1500)
        elif r.state == 'running':                      # another band: within its slice, or over to Radio
            if center is not None and abs(hz - center) <= rate / 2:
                v.listen_hz = hz
                self.player.set_mode('tuned', hz)
            else:
                self._switch_to(hz)
        elif r.busy:                                    # (finishing a capture: Radio starts once it is done)
            self.pending_hz = hz
        else:
            self.player.listen_hz = hz
            self.save_timer.start(1500)
        v.dirty = True
        self._sync()

    def _switch_to(self, hz):
        """the frequency is outside the band being received: stop that band (its capture is kept as usual) and
        start Radio there once the receiver is free"""
        r = self.tracker.receiver
        self.pending_hz = hz
        self.switch_note = 'Stopping %s to go to %.4f MHz (its capture is kept)...' % (r.system['label'], hz / 1e6)
        r.stop()

    def _pan(self, shift):
        """the spectrum was dragged along the band (Radio section, not zoomed): move everything by shift Hz"""
        v = self.panel.view
        hz = (v.listen_hz if v.listen_hz is not None else self.freq.hz) + shift
        if self._on_radio() and v.n:
            hz = float(min(max(hz, SDR_LO), SDR_HI))
            self.freq.set_value(hz)
            self.player.listen_hz = hz
            v.listen_hz = hz
            self.player.tune_center(min(max(v.center + shift, SDR_LO), SDR_HI))
            self.save_timer.start(1500)
            v.dirty = True
        else:
            self.set_frequency(hz)

    def _typed(self):
        try:
            mhz = float(self.entry.text().replace(',', '.').split()[0])
        except (ValueError, IndexError):
            return
        self.entry.clear()
        self.set_frequency(mhz * 1e6)

    def _preset(self, i):
        data = self.presets.itemData(i)
        self.presets.setCurrentIndex(0)
        if not data:
            return
        mhz, mode, bw = data
        self.set_frequency(mhz * 1e6)
        if self.pending_hz is not None or self.want_hz is not None:
            self.pending_mode = (mode, bw * 1e3)    # switching to Radio: its mode once Radio is on (else the band
        else:                                       # being stopped would remember it as its own)
            self.player.set_demod(demod=mode, bandwidth=bw * 1e3)

    def _save(self):
        s = sdr_settings.load()
        s['radio_freq_mhz'] = round(self.freq.hz / 1e6, 6)
        sdr_settings.save(s)

    # ---- mode and bandwidth
    def _slider_hz(self):
        return 100.0 * (2500.0 ** (self.bw_slider.value() / 1000.0))

    def _bw_slid(self, _):
        hz = self._slider_hz()
        self.bw.blockSignals(True)
        self.bw.setValue(hz / 1e3)
        self.bw.blockSignals(False)
        self.panel.view.passband = sound.passband(self.player.demod, hz)    # (shown live; sent on release)
        self.panel.view.dirty = True
        if not self.bw_slider.isSliderDown():
            self.player.set_demod(bandwidth=hz)

    def _show_demod(self):
        import math
        p = self.player
        if p.demod in self.mode_btns:
            self.mode_btns[p.demod].setChecked(True)
        for wdg, val in ((self.bw, p.bandwidth / 1e3),
                         (self.bw_slider, int(round(1000 * math.log(max(p.bandwidth, 100.0) / 100.0) / math.log(2500.0))))):
            wdg.blockSignals(True)
            wdg.setValue(val)
            wdg.blockSignals(False)

    def _sync(self):
        """follow tuning done elsewhere (a click or the wheel on the spectrum), and say what is going on"""
        r, v = self.tracker.receiver, self.panel.view
        if self.pending_hz is not None:             # switching over to Radio: start once the receiver is free
            if not r.busy:
                hz, self.pending_hz, self.switch_note = self.pending_hz, None, ''
                self.freq.set_value(hz)
                self._start_radio()
            else:
                self.freq.set_value(self.pending_hz)
                self.start_btn.setText('Switching to Radio...')
                self.start_btn.setEnabled(False)
                self.note.setText(self.switch_note or 'Finishing the last capture, then Radio starts...')
                return
        if self.want_hz is not None and self._on_radio():
            self.freq.set_value(self.want_hz)
            if v.n and abs(v.center - (self.want_hz + systems.RADIO_OFFSET_HZ)) < v.rate * 0.4:   # the SDR is there
                hz, self.want_hz = self.want_hz, None
                self.player.listen_hz = hz
                v.listen_hz = hz
                self.player.set_mode('tuned', hz)
                if self.panel.sound.currentIndex() != 2:
                    self.panel.sound.setCurrentIndex(2)
                v.dirty = True
        elif v.listen_hz is not None and (self._on_radio() or r.state == 'running') and not self.freq.underMouse():
            self.freq.set_value(v.listen_hz)
        if self.pending_mode and self.want_hz is None and self._on_radio() and self.player.system == 'radio':
            mode, bw = self.pending_mode
            self.pending_mode = None
            self.player.set_demod(demod=mode, bandwidth=bw)
        if self._on_radio():
            self.start_btn.setText('■  Stop radio')
            s = systems.of_frequency(self.freq.hz)
            mhz = max(self.freq.hz / 1e6, 1.0)
            hint = ('antenna: %s' % s['antenna']) if s is not systems.RADIO else \
                'antenna: a quarter wave is about %.0f cm (%.1f in) at this frequency' % (7125 / mhz, 7125 / mhz / 2.54)
            text = ('Listening. Turn the volume up on the spectrum bar. Scroll the big digits, click or drag the '
                    'spectrum, or drag the yellow band\'s edges to change the bandwidth.  ·  ' + hint)
        elif r.state == 'running':
            self.start_btn.setText('Stop %s' % r.system['label'])
            text = ('The receiver is on %s: click the spectrum to listen anywhere in its slice. Tune outside it '
                    '(digits, typing, quick picks) and it switches to Radio.' % r.system['label'])
        elif r.busy:
            self.start_btn.setText('Receiver finishing...')
            text = ''
        else:
            self.start_btn.setText('▶  Start radio')
            text = ('Choose a frequency (scroll the digits, type it, or pick one), then Start radio. '
                    'Close SDR++ and SatDump first - only one program can use the SDR.')
        if self.range_note is not None:
            text = ('%.3f MHz is %s what this SDR can receive (about 24 MHz to 2.3 GHz; E4000 tuners from about '
                    '52 MHz), so it is at %.0f MHz, the nearest it gets.' % (
                        self.range_note / 1e6, 'below' if self.range_note < SDR_LO else 'above', self.freq.hz / 1e6))
        self.note.setText(text)
        self.start_btn.setEnabled(not r.busy or r.state == 'running')
