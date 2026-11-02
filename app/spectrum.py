"""Live spectrum and waterfall (like SDR++) of the samples the receiver is decoding.

The decoder process (sdr_source.py) sends one FFT frame ~15 times a second to a local UDP port;
this widget listens on that port. Mouse wheel zooms in frequency, drag pans, hovering shows the
frequency and level under the mouse.
"""
import struct

import numpy as np
from PyQt5 import QtCore, QtGui, QtNetwork, QtWidgets
from PyQt5.QtCore import QPointF, Qt

BG, PANEL, GRID, TEXT, MUTED = '#0e1116', '#14171c', '#262b35', '#e6e9ef', '#8b93a1'
ROWS = 500          # waterfall history (about 30 s at 15 frames/s)

# where things are in the Iridium band (MHz)
BANDS = [(1616.0, 1626.0, 'duplex channels (phones, data)', QtGui.QColor(76, 154, 255, 28)),
         (1626.0, 1626.5, 'ring alerts & messaging', QtGui.QColor(76, 208, 125, 40))]


def colormap():
    """SDR++'s classic waterfall colours: black, blue, cyan, green, yellow, red"""
    stops = [(0.0, (0, 0, 0)), (0.2, (0, 0, 140)), (0.4, (0, 160, 255)), (0.6, (0, 220, 60)),
             (0.8, (255, 230, 0)), (1.0, (255, 30, 0))]
    lut = np.zeros((256, 3))
    x = np.linspace(0, 1, 256)
    for c in range(3):
        lut[:, c] = np.interp(x, [s[0] for s in stops], [s[1][c] for s in stops])
    rgb = lut.astype(np.uint32)
    return (0xFF000000 | (rgb[:, 0] << 16) | (rgb[:, 1] << 8) | rgb[:, 2]).astype(np.uint32)


class SpectrumView(QtWidgets.QWidget):
    framesChanged = QtCore.pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(220)
        self.setMouseTracking(True)
        self.sock = QtNetwork.QUdpSocket(self)
        self.sock.bind(QtNetwork.QHostAddress.LocalHost, 0)
        self.port = self.sock.localPort()
        self.sock.readyRead.connect(self._read)
        self.lut = colormap()
        self.center = self.rate = 0.0
        self.n = 0
        self.trace = self.peak = None
        self.water = None           # ROWS x n uint8 (colour index), newest row first
        self.water_db = None        # the dB values behind it (to recolour when the range changes)
        self.last_frame = 0.0
        self.paused = False
        self.auto = True
        self.peak_hold = False
        self.db_lo, self.db_hi = -110.0, -30.0
        self.view = None            # (f0, f1) Hz shown; None = everything
        self.drag = None
        self.mouse = None
        self.dirty = False
        t = QtCore.QTimer(self)
        t.timeout.connect(self._repaint)
        t.start(66)

    # ---- data
    def _read(self):
        while self.sock.hasPendingDatagrams():
            d = bytes(self.sock.receiveDatagram().data())
            if len(d) < 24 or d[:4] != b'IRSP':
                continue
            _, center, rate = struct.unpack('<4sdd', d[:20])
            v = np.frombuffer(d[20:], np.float32).astype(np.float64)
            if self.paused or not np.all(np.isfinite(v)):
                continue
            if len(v) != self.n or center != self.center or rate != self.rate:
                self._reset(center, rate, len(v))
            self.trace = v if self.trace is None else 0.6 * self.trace + 0.4 * v
            self.peak = np.maximum(self.peak, v) if self.peak is not None else v.copy()
            self.water_db = np.roll(self.water_db, 1, axis=0)
            self.water_db[0] = v
            self.water = np.roll(self.water, 1, axis=0)
            self.water[0] = self._index(v)
            self.last_frame = QtCore.QDateTime.currentMSecsSinceEpoch() / 1000
            self.frames += 1
            if self.auto and self.frames % 15 == 1:
                self._auto_range()
            self.dirty = True

    def _reset(self, center, rate, n):
        self.center, self.rate, self.n = center, rate, n
        self.trace = self.peak = None
        self.water = np.zeros((ROWS, n), np.uint8)
        self.water_db = np.full((ROWS, n), -200.0)
        self.frames = 0
        self.view = None

    def _index(self, v):
        x = (v - self.db_lo) / max(1e-3, self.db_hi - self.db_lo)
        return (np.clip(x, 0, 1) * 255).astype(np.uint8)

    def _auto_range(self):
        """noise floor near the bottom, strongest bursts near the top (like SDR++'s auto range)"""
        recent = self.water_db[:min(self.frames, 90)]
        recent = recent[recent > -199]
        if not recent.size:
            return
        lo = np.percentile(recent, 35) - 1
        hi = max(np.percentile(recent, 99.9) + 3, lo + 30)
        if abs(lo - self.db_lo) > 2 or abs(hi - self.db_hi) > 2:
            self.set_range(lo, hi)

    def set_range(self, lo, hi):
        self.db_lo, self.db_hi = float(lo), float(hi)
        if self.water_db is not None:
            self.water = self._index(self.water_db)
        self.dirty = True

    def clear_peak(self):
        self.peak = None if self.trace is None else self.trace.copy()

    @property
    def live(self):
        return self.n and QtCore.QDateTime.currentMSecsSinceEpoch() / 1000 - self.last_frame < 3

    def _repaint(self):
        if self.dirty or (self.n and not self.live):
            self.dirty = False
            self.update()

    # ---- frequency <-> pixels
    def _span(self):
        if not self.n:
            return 0, 1
        full = (self.center - self.rate / 2, self.center + self.rate / 2)
        return self.view or full

    def _bins(self):
        """bin index range shown"""
        f0, f1 = self._span()
        lo = self.center - self.rate / 2
        a = int(np.floor((f0 - lo) / self.rate * self.n))
        b = int(np.ceil((f1 - lo) / self.rate * self.n))
        return max(0, a), min(self.n, b)

    def x_of(self, f, w):
        f0, f1 = self._span()
        return (f - f0) / (f1 - f0) * w

    def f_of(self, x, w):
        f0, f1 = self._span()
        return f0 + x / max(1, w) * (f1 - f0)

    # ---- mouse
    def wheelEvent(self, e):
        if not self.n:
            return
        f0, f1 = self._span()
        fm = self.f_of(e.pos().x(), self.width())
        k = 0.8 if e.angleDelta().y() > 0 else 1.25
        span = min(self.rate, max(self.rate / self.n * 32, (f1 - f0) * k))
        r = (fm - f0) / (f1 - f0)
        self._set_view(fm - span * r, fm + span * (1 - r))

    def _set_view(self, f0, f1):
        lo, hi = self.center - self.rate / 2, self.center + self.rate / 2
        span = f1 - f0
        if span >= self.rate * 0.999:
            self.view = None
        else:
            f0 = min(max(f0, lo), hi - span)
            self.view = (f0, f0 + span)
        self.dirty = True

    def mousePressEvent(self, e):
        self.drag = (e.pos().x(), self._span())

    def mouseMoveEvent(self, e):
        self.mouse = e.pos()
        if self.drag and e.buttons() & Qt.LeftButton and self.view:
            x0, (f0, f1) = self.drag
            df = (e.pos().x() - x0) / max(1, self.width()) * (f1 - f0)
            self._set_view(f0 - df, f1 - df)
        self.dirty = True

    def mouseReleaseEvent(self, e):
        self.drag = None

    def mouseDoubleClickEvent(self, e):
        self.view = None
        self.dirty = True

    def leaveEvent(self, e):
        self.mouse = None
        self.dirty = True

    # ---- drawing
    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QtGui.QColor(BG))
        font = p.font()
        font.setPointSize(8)
        p.setFont(font)
        if not self.n:
            p.setPen(QtGui.QColor(MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, 'Start the receiver to see the spectrum and waterfall.\n'
                       '(It shows the same samples the decoder is working on - only one program can use the SDR.)')
            return
        top_h = int(h * 0.42)
        axis_h = 18
        wf_top = top_h + axis_h
        a, b = self._bins()
        f0, f1 = self._span()

        # band sections and grid behind the trace
        for lo, hi, name, col in BANDS:
            x0, x1 = self.x_of(lo * 1e6, w), self.x_of(hi * 1e6, w)
            if x1 < 0 or x0 > w:
                continue
            p.fillRect(QtCore.QRectF(max(0, x0), 0, min(w, x1) - max(0, x0), top_h), col)
            p.setPen(QtGui.QColor(MUTED))
            lx = max(x0, 48) + 4
            p.drawText(QtCore.QRectF(lx, 2, max(0, min(w, x1) - lx - 4), 14), Qt.AlignLeft | Qt.AlignVCenter, name)
        p.setPen(QtGui.QPen(QtGui.QColor(GRID), 1))
        lo_db, hi_db = self.db_lo, self.db_hi
        step = 10 if hi_db - lo_db > 40 else 5
        y_of = lambda db: top_h - (db - lo_db) / (hi_db - lo_db) * top_h
        for db in np.arange(np.ceil(lo_db / step) * step, hi_db, step):
            y = y_of(db)
            p.setPen(QtGui.QPen(QtGui.QColor(GRID), 1))
            p.drawLine(QPointF(0, y), QPointF(w, y))
            p.setPen(QtGui.QColor(MUTED))
            p.drawText(QPointF(3, y - 2), '%d dB' % db)
        fstep = next(s for s in (5e3, 10e3, 25e3, 50e3, 100e3, 250e3, 500e3, 1e6, 2e6, 5e6) if (f1 - f0) / s <= 10)
        f = np.ceil(f0 / fstep) * fstep
        while f <= f1:
            x = self.x_of(f, w)
            p.setPen(QtGui.QPen(QtGui.QColor(GRID), 1))
            p.drawLine(QPointF(x, 0), QPointF(x, top_h))
            f += fstep

        # the trace (and peak hold)
        xs = np.linspace(0, w, b - a)
        if self.peak_hold and self.peak is not None:
            p.setPen(QtGui.QPen(QtGui.QColor(255, 210, 80, 150), 1))
            p.drawPolyline(QtGui.QPolygonF([QPointF(x, y_of(v)) for x, v in zip(xs, self.peak[a:b])]))
        if self.trace is not None:
            pts = [QPointF(x, min(top_h, y_of(v))) for x, v in zip(xs, self.trace[a:b])]
            fill = QtGui.QPolygonF([QPointF(0, top_h)] + pts + [QPointF(w, top_h)])
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(76, 154, 255, 45))
            p.drawPolygon(fill)
            p.setPen(QtGui.QPen(QtGui.QColor(120, 190, 255), 1.2))
            p.drawPolyline(QtGui.QPolygonF(pts))
        cx = self.x_of(self.center, w)
        p.setPen(QtGui.QPen(QtGui.QColor(255, 107, 107, 120), 1, Qt.DashLine))
        p.drawLine(QPointF(cx, 0), QPointF(cx, top_h))

        # waterfall
        rows = min(ROWS, h - wf_top)
        if rows > 0:
            img_data = np.ascontiguousarray(self.lut[self.water[:rows, a:b]])
            img = QtGui.QImage(img_data.data, b - a, rows, (b - a) * 4, QtGui.QImage.Format_RGB32)
            p.drawImage(QtCore.QRectF(0, wf_top, w, rows), img)
        p.fillRect(QtCore.QRectF(0, top_h, w, axis_h), QtGui.QColor(PANEL))
        f = np.ceil(f0 / fstep) * fstep
        p.setPen(QtGui.QColor(MUTED))
        while f <= f1:
            x = self.x_of(f, w)
            if 30 <= x <= w - 30:
                label = ('%.3f' if fstep < 1e6 else '%.1f') % (f / 1e6)
                p.drawText(QtCore.QRectF(x - 40, top_h + 2, 80, axis_h - 2), Qt.AlignCenter, label)
            f += fstep

        # hover readout
        if self.mouse is not None:
            mx, my = self.mouse.x(), self.mouse.y()
            fm = self.f_of(mx, w)
            i = int((fm - (self.center - self.rate / 2)) / self.rate * self.n)
            if 0 <= i < self.n:
                if my < top_h and self.trace is not None:
                    db = self.trace[i]
                    what = '%.4f MHz   %.1f dB' % (fm / 1e6, db)
                else:
                    row = int((my - wf_top) / max(1, rows) * rows)
                    db = self.water_db[min(max(row, 0), ROWS - 1), i]
                    what = '%.4f MHz   %.1f dB   %.1f s ago' % (fm / 1e6, db, row / 15)
                p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 90), 1, Qt.DotLine))
                p.drawLine(QPointF(mx, 0), QPointF(mx, h))
                p.setPen(QtGui.QColor(TEXT))
                p.drawText(QPointF(min(mx + 8, w - 220), 30), what)
        if not self.live:
            p.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 120))
            p.setPen(QtGui.QColor(TEXT))
            p.drawText(self.rect(), Qt.AlignCenter, 'Receiver stopped - last spectrum shown')
        elif self.view:
            p.setPen(QtGui.QColor(MUTED))
            p.drawText(QPointF(w - 230, 30), 'zoomed - double-click to show all')


class SpectrumPanel(QtWidgets.QWidget):
    """the spectrum view with its small toolbar"""

    def __init__(self):
        super().__init__()
        self.view = SpectrumView()
        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        title = QtWidgets.QLabel('<span style="color:%s; font-weight:600">SPECTRUM</span>' % MUTED)
        self.info = QtWidgets.QLabel()
        self.info.setStyleSheet('color: %s;' % MUTED)
        self.auto = QtWidgets.QCheckBox('Auto range')
        self.auto.setChecked(True)
        self.auto.toggled.connect(self._auto)
        self.lo = QtWidgets.QSpinBox()
        self.hi = QtWidgets.QSpinBox()
        for sb, v, name in ((self.lo, -110, 'Min'), (self.hi, -30, 'Max')):
            sb.setRange(-160, 40)
            sb.setSuffix(' dB')
            sb.setPrefix(name + ' ')
            sb.setValue(v)
            sb.setEnabled(False)
            sb.valueChanged.connect(self._manual)
        self.peak = QtWidgets.QCheckBox('Peak hold')
        self.peak.toggled.connect(self._peak)
        self.pause = QtWidgets.QPushButton('Pause')
        self.pause.setCheckable(True)
        self.pause.toggled.connect(lambda on: setattr(self.view, 'paused', on))
        bar.addWidget(title)
        bar.addSpacing(10)
        bar.addWidget(self.info, 1)
        for wdg in (self.auto, self.lo, self.hi, self.peak, self.pause):
            bar.addWidget(wdg)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(self.view, 1)
        t = QtCore.QTimer(self)
        t.timeout.connect(self._update_info)
        t.start(500)

    def _auto(self, on):
        self.view.auto = on
        self.lo.setEnabled(not on)
        self.hi.setEnabled(not on)
        if not on:
            self.lo.setValue(int(self.view.db_lo))
            self.hi.setValue(int(self.view.db_hi))

    def _manual(self):
        if not self.view.auto and self.lo.value() < self.hi.value():
            self.view.set_range(self.lo.value(), self.hi.value())

    def _peak(self, on):
        self.view.peak_hold = on
        self.view.clear_peak()

    def _update_info(self):
        v = self.view
        if v.auto:      # show the range auto picked, ready for switching to manual
            for sb, val in ((self.lo, v.db_lo), (self.hi, v.db_hi)):
                sb.blockSignals(True)
                sb.setValue(int(round(val)))
                sb.blockSignals(False)
        if v.n:
            self.info.setText('%.4f MHz centre  ·  %.2f MS/s  ·  %d-point FFT  ·  frequencies in MHz  ·  %s'
                              % (v.center / 1e6, v.rate / 1e6, v.n, 'live' if v.live else 'stopped'))
        else:
            self.info.setText('waiting for the receiver')
