"""Sky plot: where in your sky the satellites are now, and where they were when your antenna heard them.

Centre = straight overhead, edge = the horizon, north at the top, east to the right (like a map).
  satellites  the ones above your horizon now (from our own orbits), named; a green ring = heard in the last
              minute, blue brackets = the one selected, whose pass across your sky is drawn as a path
  big dots    positions the satellites reported (Iridium ring alerts, Orbcomm ephemeris), coloured by signal
  small dots  everything else heard from identified satellites (frames, packets, GNSS tracking), placed with
              our own orbit for that satellite
Empty parts of the circle are directions or heights your antenna isn't hearing - blocked, or in its
pattern's blind spot (a vertical whip, for instance, hears little straight up).
"""
import math
import time

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import QPointF, Qt

TEXT, MUTED, GRID = '#e6e9ef', '#8b93a1', '#2f3541'
SNR_LO, SNR_HI = 12.0, 30.0
SECTORS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']


def snr_colour(snr):
    """weak = blue, medium = green/yellow, strong = red"""
    x = min(1.0, max(0.0, (snr - SNR_LO) / (SNR_HI - SNR_LO)))
    stops = [(0.0, (60, 120, 255)), (0.4, (40, 210, 140)), (0.7, (250, 220, 60)), (1.0, (255, 70, 60))]
    for (x0, c0), (x1, c1) in zip(stops, stops[1:]):
        if x <= x1:
            k = (x - x0) / (x1 - x0)
            return QtGui.QColor(*(int(a + (b - a) * k) for a, b in zip(c0, c1)))
    return QtGui.QColor(*stops[-1][1])


def sky_points(data, from_home, since=0.0, shows=None):
    """(fixes [(el, az, snr, t)], frames [(el, az, t)]) heard since `since` (unix time);
    shows(name, capture) -> bool limits it to the systems being shown"""
    shows = shows or (lambda name, cap=None: True)
    fixes = []
    fx = [f for f in data.all_fixes() if f.get('xyz_km') and f['t'] >= since and shows(f['name'], f['cap'])]
    if fx:
        X, Y, Z = np.array([f['xyz_km'] for f in fx], float).T
        el, az, _ = from_home(X, Y, Z)
        fixes = [(float(e), float(a), f['snr'], f['t']) for e, a, f in zip(el, az, fx) if e > -2]
    frames = []
    for name, groups in data.doppler.items():
        orbit = data.orbits.get(name)
        if orbit is None or not shows(name):
            continue
        ts = np.array([t for g in groups for t, _ in g if t >= since], float)
        if not len(ts):
            continue
        ok = orbit.sigma_km(ts) < 300           # only where our orbit for it is good
        if not ok.any():
            continue
        X, Y, Z = orbit.ecef(ts[ok]).T
        el, az, _ = from_home(X, Y, Z)
        frames += [(float(e), float(a), float(t)) for e, a, t in zip(el, az, ts[ok]) if e > -2]
    return fixes, frames


class SkyPlot(QtWidgets.QWidget):
    satelliteClicked = QtCore.pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(420, 420)
        self.fixes, self.frames = [], []
        self.now = []           # satellites up now: dicts with el, az, name, selected, live, el colour, system
        self.path = []          # the selected satellite's pass: [(el, az, t)]
        self.show_heard = self.show_now = self.show_path = True
        self.hits = []

    def set_points(self, fixes, frames):
        self.fixes, self.frames = fixes, frames
        self.update()

    def set_now(self, sats, path):
        self.now, self.path = sats, path
        self.update()

    def mouseReleaseEvent(self, e):
        best = min(((abs(x - e.x()) + abs(y - e.y()), n) for x, y, n in self.hits), default=None)
        if best and best[0] < 18:
            self.satelliteClicked.emit(best[1])

    def _xy(self, el, az, cx, cy, R):
        r = (90 - max(0.0, el)) / 90 * R
        a = math.radians(az)
        return QPointF(cx + r * math.sin(a), cy - r * math.cos(a))

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor('#14171c'))
        w, h = self.width(), self.height()
        R = min(w, h) / 2 - 34
        cx, cy = w / 2, h / 2
        font = p.font()
        font.setPointSize(8)
        p.setFont(font)
        # grid: horizon, 30 and 60 degrees up, compass lines
        p.setBrush(QtGui.QColor('#1b1f26'))
        p.setPen(QtGui.QPen(QtGui.QColor(GRID), 1))
        p.drawEllipse(QPointF(cx, cy), R, R)
        p.setBrush(Qt.NoBrush)
        for el in (30, 60):
            r = (90 - el) / 90 * R
            p.drawEllipse(QPointF(cx, cy), r, r)
        for az in range(0, 360, 45):
            p.drawLine(QPointF(cx, cy), self._xy(0, az, cx, cy, R))
        p.setPen(QtGui.QColor(MUTED))
        for el in (0, 30, 60):
            r = (90 - el) / 90 * R
            p.drawText(QPointF(cx + 4, cy - r + 12), '%d°' % el)
        p.drawText(QPointF(cx + 4, cy + 12), '90° (overhead)')
        for name, az in (('N', 0), ('E', 90), ('S', 180), ('W', 270)):
            pt = self._xy(-8, az, cx, cy, R + 14)
            p.setPen(QtGui.QColor(TEXT))
            f2 = p.font()
            f2.setBold(True)
            p.setFont(f2)
            p.drawText(QtCore.QRectF(pt.x() - 10, pt.y() - 8, 20, 16), Qt.AlignCenter, name)
            p.setFont(font)
        # frames from identified satellites, then the position fixes on top
        if self.show_heard:
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(150, 170, 200, 70))
            for el, az, _ in self.frames:
                p.drawEllipse(self._xy(el, az, cx, cy, R), 1.8, 1.8)
            for el, az, snr, _ in sorted(self.fixes, key=lambda f: f[2]):
                p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 140), 1))
                p.setBrush(snr_colour(snr))
                p.drawEllipse(self._xy(el, az, cx, cy, R), 4.5, 4.5)
        import iridium_tracker as it        # (the map's satellite drawing and colours)
        # the selected satellite's pass: rises, highest, sets
        if self.show_path and len(self.path) > 1:
            path = QtGui.QPainterPath(self._xy(self.path[0][0], self.path[0][1], cx, cy, R))
            for el, az, _ in self.path[1:]:
                path.lineTo(self._xy(el, az, cx, cy, R))
            p.setPen(QtGui.QPen(it.C_SELECT, 2, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
            p.setPen(QtGui.QColor(TEXT))
            for el, az, t in (self.path[0], self.path[-1]):
                q = self._xy(el, az, cx, cy, R)
                p.drawText(QPointF(q.x() + 6, q.y() + 14), time.strftime('%I:%M %p', time.localtime(t)).lstrip('0'))
        # the satellites up now
        self.hits = []
        if self.show_now:
            for s in sorted(self.now, key=lambda s: s['selected']):
                q = self._xy(s['el'], s['az'], cx, cy, R)
                ring = it.heard_colour(s.get('heard_age'))
                if ring is not None:
                    p.setPen(QtGui.QPen(ring, 2))
                    p.setBrush(Qt.NoBrush)
                    p.drawEllipse(q, 14, 14)
                it.draw_satellite(p, q.x(), q.y(), QtGui.QColor(40, 190, 90), 1.1 if s['selected'] else 0.85, s['system'])
                if s['selected']:
                    it.draw_selection(p, q.x(), q.y(), 16.0)
                p.setPen(QtGui.QColor(TEXT))
                p.drawText(QPointF(q.x() + 13, q.y() - 7), s['label'])
                self.hits.append((q.x(), q.y(), s['name']))
        # colour scale (for the reported positions, when there are any)
        if self.show_heard and self.fixes:
            x0, y0 = 12, h - 22
            for i in range(100):
                p.fillRect(QtCore.QRectF(x0 + i, y0, 1.2, 8), snr_colour(SNR_LO + (SNR_HI - SNR_LO) * i / 99))
            p.setPen(QtGui.QColor(MUTED))
            p.drawText(QPointF(x0, y0 - 4), 'reported position signal  %.0f - %.0f dB' % (SNR_LO, SNR_HI))


def summary(fixes, frames):
    """a few lines on what the plot shows"""
    pts = [(f[0], f[1]) for f in fixes] + [(f[0], f[1]) for f in frames]
    if not pts:
        return 'Nothing heard in this period yet.'
    els = np.array([e for e, _ in pts])
    sector = [SECTORS[int(((a + 22.5) % 360) // 45)] for _, a in pts]
    counts = {s: sector.count(s) for s in SECTORS}
    total = len(pts)
    dead = [s for s in SECTORS if counts[s] < 0.02 * total]
    bands = [('overhead (60-90°)', els >= 60), ('high (30-60°)', (els >= 30) & (els < 60)),
             ('low (0-30°)', els < 30)]
    lines = ['<b>%d reported positions</b> and <b>%d other receptions</b> from identified satellites.'
             % (len(fixes), len(frames)),
             'By height: ' + ', '.join('%s %d%%' % (n, round(100 * m.mean())) for n, m in bands) + '.',
             'By direction: ' + ', '.join('%s %d%%' % (s, round(100 * counts[s] / total)) for s in SECTORS) + '.']
    if dead:
        lines.append('<span style="color:#ff6b6b">Little or nothing heard from: %s.</span>' % ', '.join(dead))
    if (els >= 60).mean() < 0.05:
        lines.append('<span style="color:#ff6b6b">Almost nothing from overhead</span> - typical of a vertical whip '
                     '(blind straight up), or something above the antenna. An upward-facing patch antenna hears '
                     'overhead satellites best - and those are the closest and strongest.')
    return '<br>'.join(lines)


class SkyDialog(QtWidgets.QDialog):
    """the Sky window: the plot (satellites up now, the selected one's pass, and where you have heard them),
    what to show, the satellites up now, and a summary; refreshes while open"""
    PERIODS = [('This run', None), ('Last hour', 3600), ('Last 24 hours', 86400), ('Everything', 0)]

    def __init__(self, parent, data, from_home, current_run_start, shows=None):
        super().__init__(None)      # its own window: taskbar button, minimize/maximize (closed with the main window)
        self.setWindowFlags(Qt.Window | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
        self.setWindowTitle('Sky plot - where your antenna hears satellites')
        self.data, self.from_home, self.run_start, self.shows = data, from_home, current_run_start, shows
        self.tracker = parent
        self.plot = SkyPlot()
        self.plot.satelliteClicked.connect(parent.select)
        self.period = QtWidgets.QComboBox()
        for label, _ in self.PERIODS:
            self.period.addItem(label)
        self.period.setCurrentIndex(3)
        self.period.currentIndexChanged.connect(self.refresh)
        self.text = QtWidgets.QLabel()
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.RichText)
        top = QtWidgets.QHBoxLayout()
        self.layers = []
        for label, attr, tip in (('Satellites now', 'show_now', 'the satellites above your horizon now'),
                                 ('Selected pass', 'show_path', "the selected satellite's path across your sky"),
                                 ('Heard', 'show_heard', 'where you heard satellites (fixes and frames)')):
            b = QtWidgets.QPushButton(label)
            b.setCheckable(True)
            b.setChecked(True)
            bold = b.font()
            bold.setBold(True)                  # (checked buttons are drawn bold: leave room for that)
            b.setMinimumWidth(QtGui.QFontMetrics(bold).width(label) + 34)
            b.setToolTip('Show ' + tip)
            b.toggled.connect(lambda on, a=attr: (setattr(self.plot, a, on), self.plot.update()))
            top.addWidget(b)
            self.layers.append(b)
        top.addSpacing(10)
        top.addWidget(QtWidgets.QLabel('Heard in:'))
        top.addWidget(self.period)
        top.addStretch(1)
        note = QtWidgets.QLabel('<span style="color:%s">Centre = straight overhead, edge = horizon, north up, east right. '
                                'Satellites: up now (click one to select it; ring = heard: green now, yellow in the '
                                'last 10 s, red in the last minute). '
                                'Big dots: positions the satellites reported (colour = signal). Small dots: everything '
                                'else heard from identified satellites. Move the antenna and watch the empty areas '
                                'fill in.</span>' % MUTED)
        note.setWordWrap(True)
        self.up = QtWidgets.QTableWidget(0, 4)          # the satellites up now
        self.up.setHorizontalHeaderLabels(['Up now', 'Height', 'Direction', 'Heard'])
        self.up.verticalHeader().hide()
        self.up.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.up.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.up.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.up.horizontalHeader().setStretchLastSection(True)
        self.up.setMinimumWidth(270)
        self.up.cellClicked.connect(lambda r, c: parent.select(self.up.item(r, 0).data(Qt.UserRole)))
        self.up.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)    # (the selected satellite is coloured)
        self.up.setFocusPolicy(Qt.NoFocus)
        mid = QtWidgets.QHBoxLayout()
        mid.addWidget(self.plot, 1)
        mid.addWidget(self.up)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(mid, 1)
        lay.addWidget(self.text)
        lay.addWidget(note)
        self.resize(900, 760)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(5000)
        self.refresh()

    def refresh(self):
        span = self.PERIODS[self.period.currentIndex()][1]
        if span is None:
            since = self.run_start() or time.time()
        else:
            since = time.time() - span if span else 0.0
        fixes, frames = sky_points(self.data, self.from_home, since, self.shows)
        self.plot.set_points(fixes, frames)
        self.text.setText(summary(fixes, frames))
        self.update_now()

    def update_now(self):
        """the satellites up now and the selected one's pass, from the tracker's current picture (every second)"""
        tr = self.tracker
        sc = getattr(tr.map, 'scene', None) or {}
        import systems
        import iridium_tracker as it
        sats = [dict(el=s['el'], az=s['az'], name=s['name'], selected=s['selected'], live=s['live'],
                     heard_age=s.get('heard_age'),
                     system=s['system'], label=systems.short_name(s['name']))
                for s in sc.get('sats', []) if s['el'] > 0]
        path = []
        sel = tr.selected
        orbit = tr.data.orbits.get(sel) if sel else None
        np_ = tr.next_pass(sel) if orbit is not None else None
        if np_:
            ts = np.linspace(np_[0], np_[1], 80)
            el, az, _ = self.from_home(*orbit.ecef(ts).T)
            path = [(float(e), float(a), float(t)) for e, a, t in zip(el, az, ts) if e >= 0]
        self.plot.set_now(sats, path)
        sats.sort(key=lambda s: -s['el'])
        self.up.setRowCount(len(sats))
        for r, s in enumerate(sats):
            heard = 'now' if s['live'] else ''
            info = tr.data.info.get(s['name'], {}).get('now')
            if info and s['live'] and 'cn0' in info:
                heard = '%.0f dB-Hz' % info['cn0']
            for c, v in enumerate([s['label'], '%.0f\u00b0' % s['el'], '%s %.0f\u00b0' % (
                    SECTORS[int(((s['az'] + 22.5) % 360) // 45)], s['az']), heard]):
                item = QtWidgets.QTableWidgetItem(v)
                if c == 0:
                    item.setData(Qt.UserRole, s['name'])
                f = item.font()
                f.setBold(s['selected'])
                item.setFont(f)
                if s['selected']:           # the main list's selection colour
                    item.setBackground(it.C_SELECTED_NAME)
                    item.setForeground(QtGui.QColor('#ffffff'))
                self.up.setItem(r, c, item)
