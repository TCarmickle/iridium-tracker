"""Sky plot: where in your sky the satellites are now, and where they were when your antenna heard them.

Centre = straight overhead, edge = the horizon, north at the top, east to the right (like a map).
  satellites  the ones above your horizon now (from our own orbits), named; a green ring = heard in the last
              minute, blue brackets = the one selected, whose pass across your sky is drawn as a path
  big dots    positions the satellites reported (Iridium ring alerts, Orbcomm ephemeris), coloured by signal
  shading     everything else heard from identified satellites (frames, packets, GNSS tracking), placed with
              our own orbit for that satellite and counted in 3-degree patches of sky (brighter = more heard)
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
# 'heard' receptions are counted in cells of the sky this many degrees on a side (elevation and azimuth): a GNSS pass
# has a point per second for hours (760 000 points in the catalog after a few days); drawing a dot for each froze the
# window for seconds, and the cap of 200 dots per pass that replaced it (73fabfc) thinned long passes out
CELL_DEG = 3
N_EL, N_AZ = 90 // CELL_DEG, 360 // CELL_DEG


def snr_colour(snr):
    """weak = blue, medium = green/yellow, strong = red"""
    x = min(1.0, max(0.0, (snr - SNR_LO) / (SNR_HI - SNR_LO)))
    stops = [(0.0, (60, 120, 255)), (0.4, (40, 210, 140)), (0.7, (250, 220, 60)), (1.0, (255, 70, 60))]
    for (x0, c0), (x1, c1) in zip(stops, stops[1:]):
        if x <= x1:
            k = (x - x0) / (x1 - x0)
            return QtGui.QColor(*(int(a + (b - a) * k) for a, b in zip(c0, c1)))
    return QtGui.QColor(*stops[-1][1])


def sky_fixes(data, from_home, since=0.0, shows=None):
    """[(el, az, snr, t)]: the positions satellites reported (Iridium ring alerts, Orbcomm) since `since`;
    shows(name, capture) -> bool limits it to the systems being shown"""
    shows = shows or (lambda name, cap=None: True)
    fx = [f for f in data.all_fixes() if f.get('xyz_km') and f['t'] >= since and shows(f['name'], f['cap'])]
    if not fx:
        return []
    X, Y, Z = np.array([f['xyz_km'] for f in fx], float).T
    el, az, _ = from_home(X, Y, Z)
    return [(float(e), float(a), f['snr'], f['t']) for e, a, f in zip(el, az, fx) if e > -2]


class HeardCells:
    """everything heard from identified satellites (frames, packets, GNSS tracking), each reception placed with our
    own orbit for that satellite and counted in a CELL_DEG sky cell. Worked out incrementally: each Doppler curve's
    receptions are placed once and kept (cell number + time); a curve still growing live only has its new points
    placed; a satellite whose orbit was refitted is placed again; a moved home starts over.
    Measured on the 11-06 catalog + an 11 h live session (week-2 sky_load.py): 'Everything' took 190 ms to gather
    and 360 ms to paint every 5 s even with the 200-dot cap; now placing all 783 000 receptions takes 400 ms once
    (when the sky is first shown), each 5 s refresh 5-9 ms and its paint 3 ms (25 ms when the cells changed)."""

    def __init__(self):
        self.curves = {}        # id(curve) -> [curve, orbit, points done, cells (int16), times, name, all-time counts]
        self.home = None

    def update(self, data, from_home, shows):
        probe = tuple(float(np.asarray(v).reshape(-1)[0]) for v in from_home(np.array([7000.0]), np.array([0.0]),
                                                                            np.array([0.0])))
        if probe != self.home:          # (home moved: every place in the sky is different)
            self.curves, self.home = {}, probe
        seen = set()
        for name, groups in data.doppler.items():
            orbit = data.orbits.get(name)
            if orbit is None:
                continue
            visible = shows(name)
            for g in groups:
                k = id(g)
                e = self.curves.get(k)
                if e is not None and e[0] is g and not visible:
                    seen.add(k)             # (hidden for now: kept as it was, placed when shown)
                    continue
                if not visible:
                    continue
                if e is None or e[0] is not g or e[1] is not orbit or len(g) < e[2]:
                    e = self.curves[k] = [g, orbit, 0, np.zeros(0, np.int16), np.zeros(0), name, None]
                seen.add(k)
                if len(g) > e[2]:
                    self._place(e, g[e[2]:], from_home)
        for k in [k for k in self.curves if k not in seen]:
            del self.curves[k]

    @staticmethod
    def _place(e, new, from_home):
        orbit = e[1]
        e[2] += len(new)
        ts = np.fromiter((t for t, _ in new), float, len(new))
        ts = ts[orbit.sigma_km(ts) < 300]           # only where our orbit for it is good
        if not len(ts):
            return
        X, Y, Z = orbit.ecef(ts).T
        el, az, _ = from_home(X, Y, Z)
        keep = el > -2                              # (just below the horizon counts as on it)
        el, az, ts = el[keep], az[keep], ts[keep]
        cell = (np.clip(el, 0, 89.999) // CELL_DEG).astype(np.int16) * N_AZ +             ((az % 360) // CELL_DEG).astype(np.int16)
        e[3] = np.concatenate([e[3], cell])
        e[4] = np.concatenate([e[4], ts])
        e[6] = None

    def counts(self, since, shows):
        """(N_EL, N_AZ) receptions per cell since `since`, of the satellites shown"""
        acc = np.zeros(N_EL * N_AZ, np.int64)
        for e in self.curves.values():
            ts = e[4]
            if not len(ts) or not shows(e[5]):
                continue
            if since <= ts.min():
                if e[6] is None:
                    e[6] = np.bincount(e[3], minlength=N_EL * N_AZ)
                acc += e[6]
            elif since <= ts.max():
                acc += np.bincount(e[3][ts >= since], minlength=N_EL * N_AZ)
        return acc.reshape(N_EL, N_AZ)


class SkyPlot(QtWidgets.QWidget):
    satelliteClicked = QtCore.pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(300, 240)
        self.fixes = []
        self.cells = np.zeros((N_EL, N_AZ), np.int64)       # receptions per sky cell (HeardCells.counts)
        self.now = []           # satellites up now: dicts with el, az, name, selected, live, el colour, system
        self.path = []          # the selected satellite's pass: [(el, az, t)]
        self.show_heard = self.show_now = self.show_path = True
        self.hits = []
        # the grid and everything heard are drawn once into a picture and reused: the satellites up now change every
        # second, what was heard only every 5 s (and often not at all) - so a second's repaint draws only the overlay
        self._layer, self._layer_key, self._layer_version = None, None, 0

    @property
    def n_heard(self):
        return int(self.cells.sum())

    def set_points(self, fixes, cells):
        if fixes != self.fixes or not np.array_equal(cells, self.cells):
            self.fixes, self.cells = fixes, cells
            self._layer_version += 1
            self.update()

    def set_now(self, sats, path):
        self.now, self.path = sats, path
        self.update()

    def mouseReleaseEvent(self, e):
        best = min(((abs(x - e.x()) + abs(y - e.y()), n) for x, y, n in self.hits), default=None)
        self.satelliteClicked.emit(best[1] if best and best[0] < 18 else '')     # ('': empty space - deselect)

    def _xy(self, el, az, cx, cy, R):
        r = (90 - max(0.0, el)) / 90 * R
        a = math.radians(az)
        return QPointF(cx + r * math.sin(a), cy - r * math.cos(a))

    def _geometry(self):
        w, h = self.width(), self.height()
        return w, h, min(w, h) / 2 - 34, w / 2, h / 2

    def _heard_layer(self):
        """the background picture: grid, labels, heard cells, reported positions and their colour scale"""
        dpr = self.devicePixelRatioF()
        key = (self.width(), self.height(), dpr, self.show_heard, self._layer_version)
        if self._layer is not None and self._layer_key == key:
            return self._layer
        pix = QtGui.QPixmap(max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr)))
        pix.setDevicePixelRatio(dpr)
        p = QtGui.QPainter(pix)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setFont(self.font())
        self._paint_heard(p)
        p.end()
        self._layer, self._layer_key = pix, key
        return pix

    def _paint_heard(self, p):
        w, h, R, cx, cy = self._geometry()
        p.fillRect(QtCore.QRectF(0, 0, w, h), QtGui.QColor('#14171c'))
        font = p.font()
        font.setPointSize(8)
        p.setFont(font)
        # grid: horizon, 30 and 60 degrees up, compass lines
        p.setBrush(QtGui.QColor('#1b1f26'))
        p.setPen(QtGui.QPen(QtGui.QColor(GRID), 1))
        p.drawEllipse(QPointF(cx, cy), R, R)
        # what was heard, under the grid lines: one wedge per sky cell, brighter where more was heard
        if self.show_heard and self.cells.any():
            p.setPen(Qt.NoPen)
            top = math.log1p(float(self.cells.max()))
            for i, j in zip(*np.nonzero(self.cells)):
                alpha = int(45 + 150 * math.log1p(float(self.cells[i, j])) / top)
                p.setBrush(QtGui.QColor(150, 170, 200, alpha))
                p.drawPath(self._cell_path(int(i), int(j), cx, cy, R))
            p.setPen(QtGui.QPen(QtGui.QColor(GRID), 1))
        p.setBrush(Qt.NoBrush)
        for el in (30, 60):
            r = (90 - el) / 90 * R
            p.drawEllipse(QPointF(cx, cy), r, r)
        for az in range(0, 360, 45):
            p.drawLine(QPointF(cx, cy), self._xy(0, az, cx, cy, R))
        p.setPen(QtGui.QColor(MUTED))
        for el in (0, 30, 60):
            r = (90 - el) / 90 * R
            p.drawText(QPointF(cx + 4, cy - r + 12), '%d\u00b0' % el)
        p.drawText(QPointF(cx + 4, cy + 12), '90\u00b0 (overhead)')
        for name, az in (('N', 0), ('E', 90), ('S', 180), ('W', 270)):
            pt = self._xy(-8, az, cx, cy, R + 14)
            p.setPen(QtGui.QColor(TEXT))
            f2 = p.font()
            f2.setBold(True)
            p.setFont(f2)
            p.drawText(QtCore.QRectF(pt.x() - 10, pt.y() - 8, 20, 16), Qt.AlignCenter, name)
            p.setFont(font)
        if not self.show_heard:
            return
        # the position fixes on top
        for el, az, snr, _ in sorted(self.fixes, key=lambda f: f[2]):
            p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 140), 1))
            p.setBrush(snr_colour(snr))
            p.drawEllipse(self._xy(el, az, cx, cy, R), 4.5, 4.5)
        # colour scale (for the reported positions, when there are any)
        if self.fixes:
            x0, y0 = 12, h - 22
            for i in range(100):
                p.fillRect(QtCore.QRectF(x0 + i, y0, 1.2, 8), snr_colour(SNR_LO + (SNR_HI - SNR_LO) * i / 99))
            p.setPen(QtGui.QColor(MUTED))
            p.drawText(QPointF(x0, y0 - 4), 'reported position signal  %.0f - %.0f dB' % (SNR_LO, SNR_HI))

    @staticmethod
    def _cell_path(i, j, cx, cy, R):
        """the wedge of sky cell (elevation row i, azimuth column j)"""
        r_out = (90 - i * CELL_DEG) / 90 * R
        r_in = (90 - (i + 1) * CELL_DEG) / 90 * R
        a0 = 90 - j * CELL_DEG          # (Qt's angles: anticlockwise from east; azimuth: clockwise from north)
        outer = QtCore.QRectF(cx - r_out, cy - r_out, 2 * r_out, 2 * r_out)
        path = QtGui.QPainterPath()
        path.arcMoveTo(outer, a0)
        path.arcTo(outer, a0, -CELL_DEG)
        if r_in > 0.5:
            path.arcTo(QtCore.QRectF(cx - r_in, cy - r_in, 2 * r_in, 2 * r_in), a0 - CELL_DEG, CELL_DEG)
        else:
            path.lineTo(cx, cy)
        path.closeSubpath()
        return path

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.drawPixmap(0, 0, self._heard_layer())
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h, R, cx, cy = self._geometry()
        font = p.font()
        font.setPointSize(8)
        p.setFont(font)
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
                ring = it.heard_colour(s.get('heard_age'), s.get('sig_q'))
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


def summary(fixes, cells):
    """a few lines on what the plot shows (the receptions counted by sky cell, at each cell's middle)"""
    el_c, az_c = np.meshgrid((np.arange(N_EL) + 0.5) * CELL_DEG, (np.arange(N_AZ) + 0.5) * CELL_DEG, indexing='ij')
    els = np.concatenate([np.array([f[0] for f in fixes], float), el_c.ravel()])
    azs = np.concatenate([np.array([f[1] for f in fixes], float), az_c.ravel()])
    wts = np.concatenate([np.ones(len(fixes)), np.asarray(cells, float).ravel()])
    total = wts.sum()
    if not total:
        return 'Nothing heard in this period yet.'
    sector = (((azs + 22.5) % 360) // 45).astype(int)
    counts = {s: wts[sector == k].sum() for k, s in enumerate(SECTORS)}
    dead = [s for s in SECTORS if counts[s] < 0.02 * total]
    bands = [('overhead (60-90\u00b0)', els >= 60), ('high (30-60\u00b0)', (els >= 30) & (els < 60)),
             ('low (0-30\u00b0)', els < 30)]
    share = {n: wts[m].sum() / total for n, m in bands}
    lines = ['<b>%d reported positions</b> and <b>%d other receptions</b> from identified satellites.'
             % (len(fixes), int(np.asarray(cells).sum())),
             'By height: ' + ', '.join('%s %d%%' % (n, round(100 * share[n])) for n, _ in bands) + '.',
             'By direction: ' + ', '.join('%s %d%%' % (s, round(100 * counts[s] / total)) for s in SECTORS) + '.']
    if dead:
        lines.append('<span style="color:#ff6b6b">Little or nothing heard from: %s.</span>' % ', '.join(dead))
    if share[bands[0][0]] < 0.05:
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
        self.heard = HeardCells()
        self.period = QtWidgets.QComboBox()
        for label, _ in self.PERIODS:
            self.period.addItem(label)
        self.period.setCurrentIndex(0)          # this run: 'Everything' is a choice, not the default it was (slow)
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
                                'Satellites: up now (click one to select it; ring = signal strength, red weak to '
                                'green strong, fading as it goes quiet). '
                                'Big dots: positions the satellites reported (colour = signal). Shading: everything '
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
        self._up_rows = None        # what the table shows now (update_now changes only what differs)
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
        self.timer.timeout.connect(self._tick)
        self.timer.start(5000)
        self.refresh()

    def _tick(self):
        """the 5 s refresh - only while the sky is actually on screen (as a view or its own window): once opened,
        it used to keep recomputing every 5 s behind the map, freezing the whole tracker each time"""
        if self.isVisible():
            self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def refresh(self):
        span = self.PERIODS[self.period.currentIndex()][1]
        if span is None:
            since = self.run_start() or time.time()
        else:
            since = time.time() - span if span else 0.0
        shows = self.shows or (lambda name, cap=None: True)
        fixes = sky_fixes(self.data, self.from_home, since, shows)
        self.heard.update(self.data, self.from_home, shows)
        cells = self.heard.counts(since, shows)
        self.plot.set_points(fixes, cells)
        self.text.setText(summary(fixes, cells))
        self.update_now()

    def update_now(self):
        """the satellites up now and the selected one's pass, from the tracker's current picture (every second)"""
        tr = self.tracker
        sc = getattr(tr.map, 'scene', None) or {}
        import systems
        import iridium_tracker as it
        sats = [dict(el=s['el'], az=s['az'], name=s['name'], selected=s['selected'], live=s['live'],
                     heard_age=s.get('heard_age'), sig_q=s.get('sig_q'),
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
        rows = []
        for s in sats:
            heard = 'now' if s['live'] else ''
            info = tr.data.info.get(s['name'], {}).get('now')
            if info and s['live'] and 'cn0' in info:
                heard = '%.0f dB-Hz' % info['cn0']
            rows.append((s['name'], s['selected'], [s['label'], '%.0f\u00b0' % s['el'], '%s %.0f\u00b0' % (
                SECTORS[int(((s['az'] + 22.5) % 360) // 45)], s['az']), heard]))
        if rows == self._up_rows:
            return
        # only the cells that changed are touched: rebuilding the table with new items took ~1 ms a cell (its
        # columns are sized to their contents, measured again at every one) - 98 ms of each second's update
        # with 25 satellites up (week-2 sky_load.py)
        self._up_rows = rows
        self.up.setUpdatesEnabled(False)
        try:
            self.up.setRowCount(len(rows))
            for r, (name, selected, texts) in enumerate(rows):
                for c, v in enumerate(texts):
                    item = self.up.item(r, c)
                    if item is None:
                        item = QtWidgets.QTableWidgetItem(v)
                        self.up.setItem(r, c, item)
                    elif item.text() != v:
                        item.setText(v)
                    if c == 0 and item.data(Qt.UserRole) != name:
                        item.setData(Qt.UserRole, name)
                    if item.font().bold() != selected:
                        f = item.font()
                        f.setBold(selected)
                        item.setFont(f)
                    if selected:            # the main list's selection colour
                        item.setBackground(it.C_SELECTED_NAME)
                        item.setForeground(QtGui.QColor('#ffffff'))
                    elif item.background().style() != Qt.NoBrush:
                        item.setBackground(QtGui.QBrush())
                        item.setForeground(QtGui.QBrush())
        finally:
            self.up.setUpdatesEnabled(True)
