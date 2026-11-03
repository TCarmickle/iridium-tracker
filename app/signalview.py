"""The Signal window: how strongly your receiver hears each satellite, live.

One bar per satellite heard recently: its height is the signal on its system's own scale (systems.py
'signal' - bottom = barely decodes, top = strong), its colour the same red - yellow - green as the rings
on the map, faded when the satellite has gone quiet; the number on it is the measurement itself (dB for
Iridium bursts, Orbcomm packets and ham frames; dB-Hz C/N0 for GNSS). Under the bars, the selected
satellite's signal over the last ten minutes. Like an instrument display, it stays dark in both themes.
"""
import time

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import QPointF, QRectF, Qt

import systems

BG, PANEL, GRID, TEXT, MUTED = '#14171c', '#1b1f26', '#2f3541', '#e6e9ef', '#8b93a1'


class SignalBars(QtWidgets.QWidget):
    """the bars; set_bars([{name, label, value, unit, q, age, selected}])"""
    satelliteClicked = QtCore.pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(260)
        self.bars = []
        self.hits = []

    def set_bars(self, bars):
        self.bars = bars
        self.update()

    def mouseReleaseEvent(self, e):
        for rect, name in self.hits:
            if rect.contains(QPointF(e.pos())):
                self.satelliteClicked.emit(name)
                return

    def paintEvent(self, _):
        import iridium_tracker as it
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor(BG))
        w, h = self.width(), self.height()
        left, right, top, bottom = 100, 12, 26, 58
        ph = h - top - bottom
        font = p.font()
        font.setPointSize(8)
        p.setFont(font)
        # the scale: barely decodes / getting there / strong
        for q, label in ((0.0, 'barely decodes'), (0.5, 'good'), (1.0, 'strong')):
            y = top + ph * (1 - q)
            p.setPen(QtGui.QPen(QtGui.QColor(GRID), 1, Qt.DashLine if q else Qt.SolidLine))
            p.drawLine(QPointF(left, y), QPointF(w - right, y))
            p.setPen(it.strength_colour(q))
            p.drawText(QRectF(4, y - 8, left - 10, 16), Qt.AlignRight | Qt.AlignVCenter, label)
        self.hits = []
        if not self.bars:
            p.setPen(QtGui.QColor(MUTED))
            p.drawText(QRectF(left, top, w - left - right, ph), Qt.AlignCenter,
                       'Nothing heard in the last minute.\nStart the receiver - satellites appear here as they are heard.')
            return
        n = len(self.bars)
        slot = (w - left - right) / n
        bw = max(6.0, min(46.0, slot * 0.7))
        for k, b in enumerate(self.bars):
            x = left + slot * k + (slot - bw) / 2
            q = b['q']
            bh = max(3.0, ph * q)
            alpha = 255 if b['age'] <= 4 else max(70, int(255 - (b['age'] - 4) / 56 * 185))
            rect = QRectF(x, top + ph - bh, bw, bh)
            p.setPen(Qt.NoPen)
            p.setBrush(it.strength_colour(q, alpha))
            p.drawRoundedRect(rect, 3, 3)
            if b['selected']:
                p.setPen(QtGui.QPen(it.C_SELECT, 2))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), 4, 4)
            # the measurement above, the name below
            p.setPen(QtGui.QColor(TEXT))
            p.drawText(QRectF(x - 20, top + ph - bh - 18, bw + 40, 16), Qt.AlignCenter,
                       '%.0f' % b['value'] if bw < 34 else '%.0f %s' % (b['value'], b['unit']))
            p.save()
            p.translate(x + bw / 2, top + ph + 6)
            if slot < 70:
                p.rotate(45)
                p.drawText(QPointF(0, 4), b['label'])
            else:
                p.drawText(QRectF(-slot / 2, 0, slot, 16), Qt.AlignHCenter | Qt.AlignTop, b['label'])
            p.restore()
            self.hits.append((QRectF(left + slot * k, top, slot, ph + bottom), b['name']))


class SignalHistory(QtWidgets.QWidget):
    """the selected satellite's signal over the last ten minutes"""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(150)
        self.name, self.points, self.scale = None, [], (0, 1, '')

    def set_history(self, name, points, scale):
        self.name, self.points, self.scale = name, points, scale
        self.update()

    def paintEvent(self, _):
        import iridium_tracker as it
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor(PANEL))
        w, h = self.width(), self.height()
        left, right, top, bottom = 100, 12, 22, 22
        ph, pw = h - top - bottom, w - left - right
        font = p.font()
        font.setPointSize(8)
        p.setFont(font)
        p.setPen(QtGui.QColor(MUTED))
        if not self.name:
            p.drawText(self.rect(), Qt.AlignCenter, 'Select a satellite to see its signal over the last ten minutes')
            return
        lo, hi, unit = self.scale
        span = hi - lo
        vmin, vmax = lo - 0.25 * span, hi + 0.15 * span
        now = time.time()
        t0 = now - 600
        p.drawText(QPointF(left, 15), '%s - signal over the last 10 minutes (%s)' % (self.name, unit))
        y = lambda v: top + ph * (1 - (min(vmax, max(vmin, v)) - vmin) / (vmax - vmin))     # noqa: E731
        x = lambda t: left + pw * (t - t0) / 600.0                                          # noqa: E731
        for v, q in ((lo, 0.0), (hi, 1.0)):
            p.setPen(QtGui.QPen(it.strength_colour(q, 150), 1, Qt.DashLine))
            p.drawLine(QPointF(left, y(v)), QPointF(w - right, y(v)))
            p.drawText(QRectF(4, y(v) - 8, left - 10, 16), Qt.AlignRight | Qt.AlignVCenter, '%.0f %s' % (v, unit))
        p.setPen(QtGui.QColor(MUTED))
        for m in range(0, 11, 2):
            tx = x(now - m * 60)
            p.drawText(QRectF(tx - 30, h - bottom + 4, 60, 14), Qt.AlignCenter, 'now' if m == 0 else '-%d min' % m)
        pts = [(t, v) for t, v in self.points if t >= t0]
        if not pts:
            p.drawText(QRectF(left, top, pw, ph), Qt.AlignCenter, 'not heard in the last ten minutes')
            return
        for t, v in pts:         # one dot per reception, in its strength colour
            p.setPen(Qt.NoPen)
            p.setBrush(it.strength_colour(min(1.0, max(0.0, (v - lo) / span))))
            p.drawEllipse(QPointF(x(t), y(v)), 2.2, 2.2)


class SignalWindow(QtWidgets.QDialog):
    PERIODS = [('heard in the last minute', 60), ('heard in the last 10 minutes', 600)]

    def __init__(self, tracker):
        super().__init__(None)      # its own window: taskbar button, minimize/maximize (closed with the main window)
        self.setWindowFlags(Qt.Window | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
        self.setWindowTitle('Signal - how strongly your receiver hears each satellite')
        self.tracker = tracker
        self.bars = SignalBars()
        self.bars.satelliteClicked.connect(tracker.select)
        self.history = SignalHistory()
        self.period = QtWidgets.QComboBox()
        for label, _ in self.PERIODS:
            self.period.addItem('Satellites ' + label)
        self.period.currentIndexChanged.connect(self.refresh)
        self.sort = QtWidgets.QComboBox()
        self.sort.addItems(['Sorted by name', 'Sorted by signal'])
        self.sort.currentIndexChanged.connect(self.refresh)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.period)
        top.addWidget(self.sort)
        top.addStretch(1)
        note = QtWidgets.QLabel(
            '<span style="color:%s">Bar height and colour: the signal on its system\'s own scale, from barely '
            'decoding (red) to strong (green) - Iridium 12-30 dB, Orbcomm 8-25 dB, ham 6-24 dB (signal to noise of '
            'each burst, packet or frame), GNSS 28-45 dB-Hz (C/N0; about 35 is needed to decode a broadcast orbit). '
            'The number is the measurement. Faded bars: not heard for a while. Click a bar to select its '
            'satellite.</span>' % MUTED)
        note.setWordWrap(True)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.bars, 3)
        lay.addWidget(self.history, 2)
        lay.addWidget(note)
        self.resize(900, 620)

    def refresh(self):
        tr = self.tracker
        d, now = tr.data, time.time()
        span = self.PERIODS[self.period.currentIndex()][1]
        bars = []
        for name in d.live_signal:
            age = now - d.live_heard.get(name, 0)
            got = d.signal_now(name)
            if not got or age > span or not tr.shows(name):
                continue
            import iridium_tracker as it
            bars.append({'name': name, 'label': systems.short_name(name), 'value': got[0],
                         'unit': systems.of_name(name).get('signal', (0, 0, 'dB'))[2],
                         'q': it.signal_quality(name, got[0]), 'age': age, 'selected': name == tr.selected})
        if self.sort.currentIndex() == 1:
            bars.sort(key=lambda b: -b['q'])
        else:
            bars.sort(key=lambda b: (systems.DISPLAY.index(systems.of_name(b['name'])), b['label']))
        self.bars.set_bars(bars)
        sel = tr.selected
        self.history.set_history(sel, list(d.live_signal.get(sel, [])) if sel else [],
                                 systems.of_name(sel).get('signal', (10.0, 30.0, 'dB')) if sel else (0, 1, ''))
