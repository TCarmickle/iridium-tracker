"""Live spectrum and waterfall (like SDR++) of the samples the receiver is decoding.

The decoder process (sdr_source.py) sends one FFT frame ~15 times a second to a local UDP port;
this widget listens on that port. Mouse wheel zooms in frequency, drag pans, hovering shows the
frequency and level under the mouse.
"""
import struct

import numpy as np
from PyQt5 import QtCore, QtGui, QtNetwork, QtWidgets
from PyQt5.QtCore import QPointF, Qt

import systems

BG, PANEL, GRID, TEXT, MUTED = '#0e1116', '#14171c', '#262b35', '#e6e9ef', '#8b93a1'
ROWS = 500          # waterfall history (about 30 s at 15 frames/s)
DECAY_DB = 3.0      # the peak line falls this much per frame after a burst (fast rise, slow fall)
TUNE_STEP_HZ = 1000  # the mouse wheel over the trace moves the tuned frequency in these steps
TRACE_MIN, TRACE_MAX = 0.12, 0.88   # how small / large the trace may be dragged (share of the height)
AXIS_H = 18         # the frequency scale between the trace and the waterfall (also the bar to drag)

# where things are in each system's band (MHz)
BANDS = [(lo, hi, name, QtGui.QColor(*rgba), 0) for s in systems.ORDER for lo, hi, name, rgba in s['bands']]
# ...and the general radio band plan, on label rows of its own under the satellite systems' ones
BANDS += [(lo, hi, name, QtGui.QColor(*systems.PLAN_COL), row) for lo, hi, name, row in systems.RADIO_PLAN]


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
    listenPicked = QtCore.pyqtSignal(float)     # a click chose the frequency to listen to (Hz)
    bandwidthPicked = QtCore.pyqtSignal(float)  # an edge of the tuned channel was dragged: its new width (Hz)
    rangePicked = QtCore.pyqtSignal(float, float)   # the dB scale was dragged: its new (bottom, top)
    EDGE_PX = 6                                 # this close to an edge of the yellow band grabs it

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
        self.trace = self.peak = self.avg = None
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
        self.listen_hz = None       # tuned sound: the channel being listened to (Hz), drawn as a marker
        self.passband = None        # ...and what is heard around it: (low, high) Hz from listen_hz
        self.pan_retune = None      # Radio: dragging the (unzoomed) spectrum moves the SDR - called with the shift (Hz)
        self.pan_df = 0.0           # ...the shift of the drag in progress
        self.bookmarks = []         # the Radio section's saved frequencies: [(Hz, name)], marked on the trace
        # how much of the height the trace gets (the waterfall the rest): dragged on the bar between them, and
        # remembered for each section the panel is shown in (see set_section)
        self.section = 'tracker'
        self.trace_frac = 0.42
        self.set_section('tracker')
        self.split_drag = False
        self.part = 'both'          # what this view draws: 'both', just the 'trace' or just the 'water'fall
        self.frames = 0
        self.db_drag = None         # dragging the dB scale: ('hi' or 'lo', y pressed, dB range then)
        self.dirty = False
        t = QtCore.QTimer(self)
        t.timeout.connect(self._repaint)
        t.start(66)

    # ---- data
    def _read(self):
        while self.sock.hasPendingDatagrams():
            d = bytes(self.sock.receiveDatagram().data())
            if len(d) < 24 or d[:4] not in (b'IRSP', b'IRS2'):
                continue
            _, center, rate = struct.unpack('<4sdd', d[:20])
            x = np.frombuffer(d[20:], np.float32).astype(np.float64)
            if d[:4] == b'IRS2':        # peak and average of every FFT in the frame
                v, av = x[:len(x) // 2], x[len(x) // 2:]
            else:                       # a single snapshot
                v = av = x
            if self.paused or not np.all(np.isfinite(x)):
                continue
            if len(v) != self.n or center != self.center or rate != self.rate:
                self._reset(center, rate, len(v))
            # peak line: jumps up with a burst, then falls back slowly so you can see it
            self.trace = v.copy() if self.trace is None else np.maximum(v, self.trace - DECAY_DB)
            self.avg = av.copy() if self.avg is None else 0.7 * self.avg + 0.3 * av
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
        self.trace = self.peak = self.avg = None
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

    # ---- the bar between the trace and the waterfall (the frequency scale): drag it up or down
    @staticmethod
    def _fracs():
        import theme
        got = theme.load_ui().get('spectrum_trace_frac', {})
        return got if isinstance(got, dict) else {}

    def set_section(self, name):
        """the panel is now shown in this section ('tracker', 'images', 'radio'): its own trace / waterfall split"""
        self.section = name
        default = {'radio': 0.38, 'images': 0.45}.get(name, 0.42)
        try:
            frac = float(self._fracs().get(name, default))
        except (TypeError, ValueError):
            frac = default
        self.trace_frac = min(max(frac, TRACE_MIN), TRACE_MAX)
        self.dirty = True

    def _top_h(self):
        if self.part == 'trace':            # the waveform alone: all of the height (the scale under it)
            return self.height() - AXIS_H
        if self.part == 'water':            # the waterfall alone: the scale at the top
            return 0
        return int(self.height() * self.trace_frac)

    def _on_split(self, y):
        if self.part != 'both':
            return False
        top = self._top_h()
        return top - 2 <= y <= top + AXIS_H + 2

    # ---- the dB scale (left edge of the waveform): drag its top half to move the top of the range, its bottom
    # half to move the bottom (finer control of the waveform - turns Auto range off); the wheel there zooms it
    DB_AXIS_W = 46

    def _on_db_axis(self, x, y):
        return self.n and self.part != 'water' and x <= self.DB_AXIS_W and 0 <= y < self._top_h()

    def _set_db(self, lo, hi):
        if hi - lo < 6:
            return
        self.auto = False
        self.set_range(lo, hi)
        self.rangePicked.emit(self.db_lo, self.db_hi)

    # ---- mouse
    def wheelEvent(self, e):
        if not self.n:
            return
        if self._on_db_axis(e.pos().x(), e.pos().y()):     # over the dB scale: zoom the range about the mouse
            top = self._top_h()
            db = self.db_hi - e.pos().y() / max(1, top) * (self.db_hi - self.db_lo)
            k = 0.85 if e.angleDelta().y() > 0 else 1 / 0.85
            self._set_db(db - (db - self.db_lo) * k, db + (self.db_hi - db) * k)
            return
        if e.pos().y() < self._top_h():     # over the trace: step the tuned listening frequency
            self._tune_wheel(e)
            return
        # over the waterfall: zoom (the trace and the waterfall together)
        f0, f1 = self._span()
        fm = self.f_of(e.pos().x(), self.width())
        k = 0.8 if e.angleDelta().y() > 0 else 1.25
        span = min(self.rate, max(self.rate / self.n * 32, (f1 - f0) * k))
        r = (fm - f0) / (f1 - f0)
        self._set_view(fm - span * r, fm + span * (1 - r))

    def _tune_wheel(self, e):
        """each wheel click moves the tuned frequency 1 kHz (10 kHz with Shift), snapped to that step"""
        if self.listen_hz is None:      # only while the sound is on Tuned
            return
        self.wheel_acc = getattr(self, 'wheel_acc', 0) + e.angleDelta().y()
        clicks = int(self.wheel_acc / 120)          # touchpads send small deltas: add them up
        if not clicks:
            return
        self.wheel_acc -= clicks * 120
        step = TUNE_STEP_HZ * (10 if e.modifiers() & Qt.ShiftModifier else 1)
        f = round(self.listen_hz / step) * step + clicks * step
        self.listen_hz = min(max(f, self.center - self.rate / 2), self.center + self.rate / 2)
        self.listenPicked.emit(self.listen_hz)
        self.dirty = True

    def _set_view(self, f0, f1):
        lo, hi = self.center - self.rate / 2, self.center + self.rate / 2
        span = f1 - f0
        if span >= self.rate * 0.999:
            self.view = None
        else:
            f0 = min(max(f0, lo), hi - span)
            self.view = (f0, f0 + span)
        self.dirty = True

    def _edge_at(self, x):
        """'lo' / 'hi' if x is on an edge of the tuned channel's yellow band, else None"""
        if self.listen_hz is None or not self.n or not self.passband:
            return None
        w = self.width()
        lo, hi = self.passband
        for name, f in (('lo', self.listen_hz + lo), ('hi', self.listen_hz + hi)):
            if f != self.listen_hz and abs(self.x_of(f, w) - x) <= self.EDGE_PX:
                return name
        return None

    def _drag_width(self, x):
        """the channel width for an edge dragged to x: both sides move together, except one-sided USB / LSB"""
        f = self.f_of(x, self.width())
        lo, hi = self.passband
        if lo == 0:                     # USB: the band is above the tuned frequency
            bw = f - self.listen_hz
        elif hi == 0:                   # LSB: below it
            bw = self.listen_hz - f
        else:
            bw = 2 * abs(f - self.listen_hz)
        return min(max(bw, 100.0), 250e3)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self._on_db_axis(e.pos().x(), e.pos().y()):
            self.db_drag = ('hi' if e.pos().y() < self._top_h() / 2 else 'lo', e.pos().y(), self.db_lo, self.db_hi)
            self.drag = self.edge_drag = None
            self.split_drag = False
            return
        self.split_drag = e.button() == Qt.LeftButton and self.n > 0 and self._on_split(e.pos().y())
        self.edge_drag = None if self.split_drag or e.button() != Qt.LeftButton else self._edge_at(e.pos().x())
        self.drag = None if self.edge_drag or self.split_drag else (e.pos().x(), self._span())
        self.press_x = e.pos().x()

    def mouseMoveEvent(self, e):
        self.mouse = e.pos()
        if self.db_drag:                            # the dB scale: pull the top (or bottom) of the range
            end, y0, lo0, hi0 = self.db_drag
            ddb = (e.pos().y() - y0) / max(1, self._top_h()) * (hi0 - lo0)
            if end == 'hi':
                self._set_db(lo0, max(hi0 - ddb, lo0 + 6))     # drag down: the top comes down
            else:
                self._set_db(min(lo0 - ddb, hi0 - 6), hi0)     # drag up: the bottom comes up
            self.dirty = True
            return
        if self.split_drag:                         # the bar between trace and waterfall: resizing them
            self.trace_frac = min(max(e.pos().y() / max(1, self.height()), TRACE_MIN), TRACE_MAX)
            self.dirty = True
            return
        if getattr(self, 'edge_drag', None):        # pressed on an edge, not released yet: widening / narrowing it
            bw = self._drag_width(e.pos().x())
            lo, hi = self.passband
            self.passband = (0.0, bw) if lo == 0 else (-bw, 0.0) if hi == 0 else (-bw / 2, bw / 2)
            self.dirty = True
            return
        if not e.buttons():
            self.setCursor(Qt.SizeVerCursor if self.n and (self._on_split(e.pos().y()) or
                                                          self._on_db_axis(e.pos().x(), e.pos().y())) else
                           Qt.SizeHorCursor if self._edge_at(e.pos().x()) else Qt.ArrowCursor)
        if self.drag and e.buttons() & Qt.LeftButton and self.view:
            x0, (f0, f1) = self.drag
            df = (e.pos().x() - x0) / max(1, self.width()) * (f1 - f0)
            self._set_view(f0 - df, f1 - df)
        elif self.drag and e.buttons() & Qt.LeftButton and self.pan_retune is not None and self.n:
            x0, (f0, f1) = self.drag            # Radio, not zoomed: dragging slides along the band
            self.pan_df = (e.pos().x() - x0) / max(1, self.width()) * (f1 - f0)
        self.dirty = True

    def mouseReleaseEvent(self, e):
        self.drag = None
        if self.db_drag:
            self.db_drag = None
            return
        if self.split_drag:
            self.split_drag = False
            import theme
            theme.save_ui(spectrum_trace_frac=dict(self._fracs(), **{self.section: round(self.trace_frac, 3)}))
            return
        if getattr(self, 'edge_drag', None):
            self.edge_drag = None
            lo, hi = self.passband
            self.bandwidthPicked.emit(hi - lo)
            return
        if self.pan_df and abs(e.pos().x() - getattr(self, 'press_x', -99)) >= 4:
            shift, self.pan_df = -self.pan_df, 0.0      # dragged right = towards lower frequencies, like a map
            self.dirty = True
            if self.pan_retune is not None:
                self.pan_retune(shift)
            return
        self.pan_df = 0.0
        if self.n and self.listen_hz is not None and abs(e.pos().x() - getattr(self, 'press_x', -99)) < 4:
            self.listen_hz = self.f_of(e.pos().x(), self.width())      # a click (not a drag): listen here
            self.listenPicked.emit(self.listen_hz)
            self.dirty = True

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
        top_h = self._top_h()
        axis_h = AXIS_H
        wf_top = top_h + axis_h
        a, b = self._bins()
        f0, f1 = self._span()

        # band sections and grid behind the trace (kept inside the trace's own area)
        p.save()
        p.setClipRect(QtCore.QRectF(0, 0, w, top_h))
        for lo, hi, name, col, row in BANDS:
            x0, x1 = self.x_of(lo * 1e6, w), self.x_of(hi * 1e6, w)
            if x1 < 0 or x0 > w:
                continue
            if row:                 # the band plan: a thin bar under its label, not a tint over the whole trace
                y = 2 + 14 * row
                p.fillRect(QtCore.QRectF(max(0, x0), y + 13, max(1.0, min(w, x1) - max(0, x0)), 2),
                           QtGui.QColor(150, 160, 180, 110))
            else:
                y = 2
                p.fillRect(QtCore.QRectF(max(0, x0), 0, min(w, x1) - max(0, x0), top_h), col)
            p.setPen(QtGui.QColor(MUTED))
            lx = max(x0, 48) + 4
            room = min(w, x1) - lx - 4
            if room > 40:       # shorten the name to fit its section, or leave it out
                p.drawText(QtCore.QRectF(lx, y, room, 14), Qt.AlignLeft | Qt.AlignVCenter,
                           p.fontMetrics().elidedText(name, Qt.ElideRight, int(room)))
        p.setPen(QtGui.QPen(QtGui.QColor(GRID), 1))
        lo_db, hi_db = self.db_lo, self.db_hi
        if self.auto and self.avg is not None:
            lo_db = min(lo_db, float(np.percentile(self.avg, 30)) - 6)
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
        if self.avg is not None:        # average: the noise floor, as a filled area
            pts = [QPointF(x, min(top_h, y_of(v))) for x, v in zip(xs, self.avg[a:b])]
            p.setPen(QtGui.QPen(QtGui.QColor(76, 154, 255, 110), 1))
            p.setBrush(QtGui.QColor(76, 154, 255, 40))
            p.drawPolygon(QtGui.QPolygonF([QPointF(0, top_h)] + pts + [QPointF(w, top_h)]))
        if self.trace is not None:      # peak: every burst in the last moments
            pts = [QPointF(x, min(top_h, y_of(v))) for x, v in zip(xs, self.trace[a:b])]
            p.setPen(QtGui.QPen(QtGui.QColor(150, 210, 255), 1.3))
            p.setBrush(Qt.NoBrush)
            p.drawPolyline(QtGui.QPolygonF(pts))
        p.restore()
        cx = self.x_of(self.center, w)
        p.setPen(QtGui.QPen(QtGui.QColor(255, 107, 107, 120), 1, Qt.DashLine))
        p.drawLine(QPointF(cx, 0), QPointF(cx, top_h))
        if self.listen_hz is not None:        # the tuned channel being listened to
            lx = self.x_of(self.listen_hz, w)
            lo, hi = self.passband or (-systems.of_frequency(self.center)['channel_hz'] / 2,
                                       systems.of_frequency(self.center)['channel_hz'] / 2)
            x0, x1 = self.x_of(self.listen_hz + lo, w), self.x_of(self.listen_hz + hi, w)
            if x1 - x0 < 4:             # always visible, even zoomed out
                x0, x1 = (x0 + x1) / 2 - 2, (x0 + x1) / 2 + 2
            p.fillRect(QtCore.QRectF(x0, 0, x1 - x0, top_h), QtGui.QColor(255, 210, 60, 45))
            p.setPen(QtGui.QPen(QtGui.QColor(255, 210, 60), 1.5))
            p.drawLine(QPointF(lx, 0), QPointF(lx, h))
            p.drawText(QPointF(min(lx + 6, w - 150), top_h - 6), '\U0001F50A %.4f MHz' % (self.listen_hz / 1e6))
        for hz, name in self.bookmarks:       # saved frequencies in view: a star and the name
            bx = self.x_of(hz, w)
            if 0 <= bx <= w:
                p.setPen(QtGui.QPen(QtGui.QColor(255, 210, 60, 110), 1, Qt.DotLine))
                p.drawLine(QPointF(bx, top_h - 34), QPointF(bx, top_h))
                p.setPen(QtGui.QColor(255, 210, 60, 200))
                p.drawText(QPointF(min(bx + 3, w - 120), top_h - 24), '★ ' + name)
        if self.pan_df:                       # Radio: a drag along the band in progress
            to = (self.listen_hz if self.listen_hz is not None else self.center) - self.pan_df
            p.fillRect(QtCore.QRectF(w / 2 - 170, top_h / 2 - 16, 340, 32), QtGui.QColor(20, 24, 30, 220))
            p.setPen(QtGui.QColor(255, 210, 60))
            p.drawText(QtCore.QRectF(w / 2 - 170, top_h / 2 - 16, 340, 32), Qt.AlignCenter,
                       '%s  release to move to %.4f MHz' % ('◀' if self.pan_df > 0 else '▶', to / 1e6))

        # waterfall
        area = h - wf_top
        rows = min(ROWS, area)
        if rows > 0:
            img_data = np.ascontiguousarray(self.lut[self.water[:rows, a:b]])
            img = QtGui.QImage(img_data.data, b - a, rows, (b - a) * 4, QtGui.QImage.Format_RGB32)
            p.drawImage(QtCore.QRectF(0, wf_top, w, area), img)     # (stretched when taller than its history)
        p.fillRect(QtCore.QRectF(0, top_h, w, axis_h), QtGui.QColor(PANEL))
        # the grip: this bar can be dragged up or down to give the trace or the waterfall more room
        hot = self.split_drag or (self.mouse is not None and self._on_split(self.mouse.y()))
        if self.part == 'both':             # (one part alone: nothing to share)
            p.setPen(QtGui.QPen(QtGui.QColor(255, 210, 60) if hot else QtGui.QColor(MUTED), 1.5))
            for dy in (5, 9, 13):
                p.drawLine(QPointF(8, top_h + dy), QPointF(22, top_h + dy))
                p.drawLine(QPointF(w - 22, top_h + dy), QPointF(w - 8, top_h + dy))
        # the dB scale lights up under the mouse: it can be dragged (top half: the top, bottom half: the bottom)
        if self.db_drag or (self.mouse is not None and self._on_db_axis(self.mouse.x(), self.mouse.y())):
            end = self.db_drag[0] if self.db_drag else ('hi' if self.mouse.y() < top_h / 2 else 'lo')
            y0 = 0 if end == 'hi' else top_h / 2
            p.fillRect(QtCore.QRectF(0, y0, self.DB_AXIS_W, top_h / 2), QtGui.QColor(255, 210, 60, 28))
            p.setPen(QtGui.QColor(255, 210, 60))
            p.drawText(QtCore.QRectF(2, y0 + (2 if end == 'hi' else top_h / 2 - 16), self.DB_AXIS_W + 120, 14),
                       Qt.AlignLeft | Qt.AlignVCenter, '↕ drag: %s %.0f dB' % (
                           'top' if end == 'hi' else 'bottom', self.db_hi if end == 'hi' else self.db_lo))
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
                    what = '%.4f MHz   peak %.1f dB   average %.1f dB' % (fm / 1e6, self.trace[i], self.avg[i])
                else:
                    row = int((my - wf_top) / max(1, area) * rows)     # (the image may be stretched)
                    db = self.water_db[min(max(row, 0), ROWS - 1), i]
                    what = '%.4f MHz   %.1f dB   %.1f s ago' % (fm / 1e6, db, row / 15)
                p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 90), 1, Qt.DotLine))
                p.drawLine(QPointF(mx, 0), QPointF(mx, h))
                p.setPen(QtGui.QColor(TEXT))
                p.drawText(QPointF(min(mx + 8, w - 220), 62), what)     # (under the band-label rows)
        if not self.live:
            p.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 120))
            p.setPen(QtGui.QColor(TEXT))
            p.drawText(self.rect(), Qt.AlignCenter, 'Receiver stopped - last spectrum shown')
        elif self.view:
            p.setPen(QtGui.QColor(MUTED))
            p.drawText(QPointF(w - 230, 30), 'zoomed - double-click to show all')


class SpectrumMirror(SpectrumView):
    """a second look at a SpectrumView's live data - its waveform or its waterfall alone, in a window of its own.
    Everything about what is received and listened to is the main view's (shared, both ways); the mouse, drags
    and repainting are its own."""
    SHARED = ('center', 'rate', 'n', 'trace', 'peak', 'avg', 'water', 'water_db', 'last_frame', 'paused', 'auto',
              'peak_hold', 'db_lo', 'db_hi', 'view', 'listen_hz', 'passband', 'bookmarks', 'frames', 'pan_retune',
              'lut')

    def __init__(self, master, part):
        QtWidgets.QWidget.__init__(self)
        self.master = master
        self.part = part
        self.setMinimumHeight(120)
        self.setMouseTracking(True)
        self.drag = self.mouse = self.edge_drag = self.db_drag = None
        self.split_drag = False
        self.pan_df = 0.0
        self.trace_frac, self.section = 0.42, 'mirror'
        self.dirty = True
        self.seen = -1
        # its clicks and drags act on the main view's listening (the panel is connected to those)
        self.listenPicked.connect(master.listenPicked)
        self.bandwidthPicked.connect(master.bandwidthPicked)
        self.rangePicked.connect(master.rangePicked)
        t = QtCore.QTimer(self)
        t.timeout.connect(self._repaint)
        t.start(66)

    def _repaint(self):
        if self.dirty or self.master.frames != self.seen or self.master.dirty:
            self.seen = self.master.frames
            self.dirty = False
            self.update()


for _name in SpectrumMirror.SHARED:         # (each shared value is read and written on the main view)
    setattr(SpectrumMirror, _name, property(lambda self, n=_name: getattr(self.master, n),
                                            lambda self, v, n=_name: setattr(self.master, n, v)))


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
        self.sound = QtWidgets.QComboBox()
        self.sound.addItems(['\U0001F507 Sound off', '\U0001F50A Whole band', '\U0001F50A Tuned (click to tune)'])
        self.sound.setToolTip('Hear the radio like SDR++: static, with a click or "zzt" for each burst or packet.\n'
                              'Whole band: every burst the receiver hears. Tuned: one channel - click the spectrum '
                              'to choose it.\nNothing is decoded into speech.')
        self.volume = QtWidgets.QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(60)
        self.volume.setFixedWidth(90)
        self.volume.setToolTip('Volume')
        self.audio_btn = QtWidgets.QToolButton()
        self.audio_btn.setText('Audio \u25be')
        self.audio_btn.setToolTip('The tuned sound\'s mode (NFM, WFM, AM, USB, LSB, CW) and bandwidth;\n'
                                  'low-pass and high-pass filters, and squelch, for the sound')
        self.audio_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.player = None
        self.pause = QtWidgets.QPushButton('Pause')
        self.pause.setCheckable(True)
        self.pause.toggled.connect(lambda on: setattr(self.view, 'paused', on))
        # the waveform or the waterfall can each go into a window of their own
        import popout
        self.popped = {}            # part -> (its window, its mirror view)
        self.pop_btns = {}
        for part, label, tip in (('trace', 'Waveform', 'the spectrum line'), ('water', 'Waterfall', 'the waterfall')):
            b = QtWidgets.QToolButton()
            b.setText('%s %s' % (popout.POP, label))
            b.setToolTip('Pop %s out into a window of its own (close that window to bring it back)' % tip)
            b.clicked.connect(lambda _, part=part: self.pop_out(part))
            self.pop_btns[part] = b
        bar.addWidget(title)
        bar.addSpacing(10)
        bar.addWidget(self.info, 1)
        for wdg in (self.sound, self.volume, self.audio_btn, self.auto, self.lo, self.hi, self.peak, self.pause,
                    self.pop_btns['trace'], self.pop_btns['water']):
            bar.addWidget(wdg)
        self.both_out = popout.placeholder('The waveform and the waterfall are in their own windows.',
                                           self.dock_all)
        self.both_out.hide()
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.both_out, 1)
        self.view.rangePicked.connect(self._range_picked)
        t = QtCore.QTimer(self)
        t.timeout.connect(self._update_info)
        t.start(500)

    # ---- pop-outs
    def pop_out(self, part):
        """the waveform ('trace') or the waterfall ('water') in a window of its own; the panel keeps the other"""
        import popout
        if part in self.popped:
            win = self.popped[part][0]
            win.showNormal()
            win.raise_()
            return
        mirror = SpectrumMirror(self.view, part)
        title = 'Waveform (spectrum)' if part == 'trace' else 'Waterfall'
        win = popout.PopWindow('spectrum_' + part, title, mirror, size=(1200, 380 if part == 'trace' else 520))
        win.closed.connect(lambda: self._docked(part))
        self.popped[part] = (win, mirror)
        self._parts()
        win.show()

    def _docked(self, part):
        got = self.popped.pop(part, None)
        if got:
            got[0].deleteLater()
        self._parts()

    def dock_all(self):
        for part in list(self.popped):
            self.popped[part][0].close()

    close_windows = dock_all

    def _parts(self):
        """what the panel's own view shows: whatever is not popped out"""
        out = set(self.popped)
        self.view.part = 'both' if not out else 'water' if out == {'trace'} else 'trace' if out == {'water'} else None
        both = self.view.part is None
        self.view.setVisible(not both)
        self.both_out.setVisible(both)
        if not both:
            self.view.dirty = True
        for part, b in self.pop_btns.items():
            b.setDown(part in out)

    def _range_picked(self, lo, hi):
        """the dB scale was dragged: the numbers follow, Auto range off (numbers first: switching Auto off applies
        the boxes' values, and old ones there would undo the drag)"""
        for sb, val in ((self.lo, lo), (self.hi, hi)):
            sb.blockSignals(True)
            sb.setValue(int(round(val)))
            sb.blockSignals(False)
        if self.auto.isChecked():
            self.auto.setChecked(False)
            self.view.set_range(lo, hi)     # (the boxes hold whole dB; keep the exact range dragged)

    def attach_sound(self, player):
        """connect the sound controls to a sound.SoundPlayer"""
        self.player = player
        self.sound.currentIndexChanged.connect(self._sound_mode)
        self.volume.valueChanged.connect(lambda v: player.set_volume(v / 100))
        self.view.listenPicked.connect(lambda hz: player.set_mode('tuned', hz))
        self.view.bandwidthPicked.connect(lambda bw: player.set_demod(bandwidth=bw))
        self.audio_btn.setMenu(self._audio_menu(player))
        player.demodChanged.connect(self._show_demod)
        self._show_demod()
        # every start: sound on (tuned) but the volume at zero - turn it up to listen
        self.volume.setValue(0)
        player.set_volume(0.0)
        self.sound.setCurrentIndex(2)

    def _audio_menu(self, player):
        """the Audio menu: the tuned sound's mode and bandwidth (made in the receiver, remembered per system);
        low-pass, high-pass and squelch (applied in the tracker, remembered)"""
        import sound
        menu = QtWidgets.QMenu(self)
        box = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(box)
        form.setContentsMargins(12, 10, 12, 10)
        a = player.settings
        self.demod = QtWidgets.QComboBox()
        for d in sound.DEMODS:
            self.demod.addItem(d.upper(), d)
        self.demod.setToolTip('How the tuned channel is demodulated, like SDR++:\n'
                              'NFM narrow FM (voice, APRS, data warble), WFM broadcast FM, AM (also the bursts\' '
                              'envelope),\nUSB / LSB one sideband, CW Morse (a carrier heard as a 700 Hz tone).\n'
                              'Each band starts in its own mode; your last choice is kept per system.')
        self.demod.currentIndexChanged.connect(lambda i: player.set_demod(demod=self.demod.itemData(i)))
        self.bw = QtWidgets.QDoubleSpinBox()
        self.bw.setRange(0.1, 250)
        self.bw.setDecimals(2)
        self.bw.setSuffix(' kHz')
        self.bw.setStepType(QtWidgets.QAbstractSpinBox.AdaptiveDecimalStepType)
        self.bw.setKeyboardTracking(False)
        self.bw.setToolTip('How wide the tuned channel is (the yellow band on the spectrum)')
        self.bw.valueChanged.connect(lambda v: player.set_demod(bandwidth=v * 1e3))
        default = QtWidgets.QPushButton('Band default')
        default.setToolTip('Back to this band\'s own mode and bandwidth')
        default.clicked.connect(player.reset_demod)
        row_bw = QtWidgets.QHBoxLayout()
        row_bw.addWidget(self.bw, 1)
        row_bw.addWidget(default)
        form.addRow('Mode:', self.demod)
        form.addRow('Bandwidth:', row_bw)
        lp = QtWidgets.QComboBox()
        for hz in sound.LOW_PASS:
            lp.addItem('Off' if not hz else '%g kHz' % (hz / 1000), hz)
        lp.setCurrentIndex(max(0, lp.findData(a['low_pass'])))
        lp.setToolTip('Cuts the sound above this frequency: softer, less hiss')
        lp.currentIndexChanged.connect(lambda i: player.set_filters(low_pass=lp.itemData(i)))
        hp = QtWidgets.QComboBox()
        for hz in sound.HIGH_PASS:
            hp.addItem('Off' if not hz else ('%d Hz' % hz if hz < 1000 else '%g kHz' % (hz / 1000)), hz)
        hp.setCurrentIndex(max(0, hp.findData(a['high_pass'])))
        hp.setToolTip('Cuts the sound below this frequency: less rumble, crisper clicks')
        hp.currentIndexChanged.connect(lambda i: player.set_filters(high_pass=hp.itemData(i)))
        row = QtWidgets.QHBoxLayout()
        sq = QtWidgets.QSlider(Qt.Horizontal)
        sq.setRange(0, 30)
        sq.setValue(int(a['squelch_db']))
        sq.setFixedWidth(140)
        sq_lbl = QtWidgets.QLabel()
        sq_lbl.setMinimumWidth(48)
        show = lambda v: sq_lbl.setText('Off' if not v else '%d dB' % v)
        show(sq.value())
        sq.setToolTip('Mutes the static; opens only for signals this much louder than the noise (0 = off)')
        sq.valueChanged.connect(show)
        sq.valueChanged.connect(lambda v: player.set_filters(squelch_db=float(v)))
        row.addWidget(sq)
        row.addWidget(sq_lbl)
        form.addRow('Low-pass:', lp)
        form.addRow('High-pass:', hp)
        form.addRow('Squelch:', row)
        act = QtWidgets.QWidgetAction(menu)
        act.setDefaultWidget(box)
        menu.addAction(act)
        return menu

    def _show_demod(self):
        """the player's tuned mode and width: in the Audio menu, and on the spectrum marker"""
        import sound
        p = self.player
        for wdg, set_value in ((self.demod, lambda: self.demod.setCurrentIndex(self.demod.findData(p.demod))),
                               (self.bw, lambda: self.bw.setValue(p.bandwidth / 1e3))):
            wdg.blockSignals(True)
            set_value()
            wdg.blockSignals(False)
        self.audio_btn.setText('Audio %s ▾' % p.demod.upper())
        self.view.passband = sound.passband(p.demod, p.bandwidth)
        self.view.dirty = True

    def _sound_mode(self, i):
        mode = ('off', 'wide', 'tuned')[i]
        if mode == 'tuned':
            self.view.listen_hz = self.view.listen_hz or self.player.listen_hz
        else:
            self.view.listen_hz = None
        self.view.dirty = True
        self.player.set_mode(mode, self.view.listen_hz)

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
        if self.player is not None and v.n and self.player.center != v.center:
            self.player.center = v.center
            band = systems.get(self.player.force_system) if self.player.force_system else systems.of_frequency(v.center)
            if band['key'] != self.player.system:      # another band: its own mode
                self.player.set_band(v.center)
            if abs(self.player.listen_hz - v.center) > v.rate / 2:      # a different band: its own channel
                f = band['listen_hz']
                if abs(f - v.center) > v.rate / 2:      # (Radio, or a band's channel outside this slice)
                    f = v.center - systems.RADIO_OFFSET_HZ
                self.player.listen_hz = f
                if v.listen_hz is not None:
                    v.listen_hz = self.player.listen_hz
            self.player.set_mode(self.player.mode)
        if v.auto:      # show the range auto picked, ready for switching to manual
            for sb, val in ((self.lo, v.db_lo), (self.hi, v.db_hi)):
                sb.blockSignals(True)
                sb.setValue(int(round(val)))
                sb.blockSignals(False)
        if v.n:
            self.info.setText('%.4f MHz centre  ·  %.2f MS/s  ·  %d-point FFT  ·  line = peak, fill = average  ·  MHz  ·  %s'
                              % (v.center / 1e6, v.rate / 1e6, v.n, 'live' if v.live else 'stopped'))
        else:
            self.info.setText('waiting for the receiver')
