"""Satellite info window: opens when you click a satellite, with tabs about it.

  About         what it is: block, launch, hardware, and how your receiver identified it
  Doppler curve each pass's measured frequency shift, with the predicted curve it was matched to
  Heard         every pass your antenna picked it up
  Orbit         our own orbit for it (from your observations), compared with the public catalog

The public catalog (CelesTrak) is used here for reference facts and to explain identification only;
everything the main window draws still comes from your own observations.
"""
import datetime
import math
import time

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import QPointF, Qt

import catalog

UTC = datetime.timezone.utc
F_HZ = 1626.1e6                 # the frequency the Doppler shift is worked out for (same as catalog.py)
TEXT, MUTED, GRID, ACCENT = '#e6e9ef', '#8b93a1', '#2f3541', '#4c9aff'
C_MEASURED, C_CATALOG, C_OURS = QtGui.QColor(255, 196, 61), QtGui.QColor(76, 154, 255), QtGui.QColor(76, 208, 125)

# Iridium NEXT launches, in order (all SpaceX Falcon 9 from Vandenberg). The catalog's launch IDs
# (year + launch number) are matched to these by year and order.
LAUNCHES = [
    ('2017', 'Iridium-1', 'Jan 14, 2017', 'The first 10 Iridium NEXT satellites, and Falcon 9\'s return to flight '
                                          'after the 2016 launch-pad explosion.'),
    ('2017', 'Iridium-2', 'Jun 25, 2017', 'The second batch of 10.'),
    ('2017', 'Iridium-3', 'Oct 9, 2017', 'The third batch of 10.'),
    ('2017', 'Iridium-4', 'Dec 22, 2017', 'Launched at dusk - the glowing exhaust plume was seen across '
                                          'Southern California and set off a wave of "UFO" reports.'),
    ('2018', 'Iridium-5', 'Mar 30, 2018', 'The fifth batch of 10.'),
    ('2018', 'Iridium-6 / GRACE-FO', 'May 22, 2018', 'Shared ride: 5 Iridium NEXT satellites with NASA\'s twin '
                                                     'GRACE-FO gravity-mapping satellites.'),
    ('2018', 'Iridium-7', 'Jul 25, 2018', 'The seventh batch of 10.'),
    ('2019', 'Iridium-8', 'Jan 11, 2019', 'The final batch, completing the 75-satellite NEXT launch campaign.'),
    ('2023', 'Iridium-9', 'May 20, 2023', 'Five ground spares launched years later to top up the in-orbit spares.'),
]

BLOCK_FACTS = [
    ('Block', 'Iridium NEXT (second generation), replacing the original 1990s Iridium satellites'),
    ('Builder', 'Thales Alenia Space (prime), with Northrop Grumman (then Orbital ATK) assembling them'),
    ('Main antenna', 'L-band phased array making 48 spot beams on the ground - the bursts you receive '
                     'at 1616-1626.5 MHz come from these'),
    ('Crosslinks', 'Ka-band links to 4 neighbours (ahead and behind in its plane, and one in each '
                   'neighbouring plane), so calls hop satellite to satellite'),
    ('Hosted payload', 'Aireon ADS-B receiver: picks up aircraft position broadcasts for worldwide '
                       'air-traffic surveillance'),
    ('Constellation', '66 working satellites in 6 polar planes of 11, plus in-orbit spares'),
]

DOPPLER_HELP = (
    '<b>What is a Doppler curve?</b> As the satellite comes towards you its signal arrives a little higher '
    'in frequency; as it goes away, a little lower. Plotting that shift over a pass gives an S-shaped curve. '
    'It crosses zero at the moment of <b>closest approach</b>. A pass nearly overhead gives a steep S '
    '(the shift flips quickly); a distant, low pass gives a gentle one. At 1626 MHz the shift can reach '
    'about &plusmn;36 kHz. Every satellite\'s pass over you has its own curve, so matching the measured '
    'points to the predicted curves identifies which satellite you heard - and the curves also help fit '
    'our own orbit.')


# ---------------------------------------------------------------- public catalog helpers
def tle_records():
    """name -> (line 1, line 2) from the public catalog file"""
    for path in (catalog.TLE_FILE, catalog.SATDUMP_TLES):
        try:
            lines = open(path).read().splitlines()
        except OSError:
            continue
        return {lines[i].strip(): (lines[i + 1], lines[i + 2])
                for i in range(len(lines) - 2) if lines[i].startswith('IRIDIUM') and lines[i + 1].startswith('1 ')}
    return {}


def launch_of(records, l1):
    """(launch name, date, note, piece) for a TLE line 1, from the order of launches in the catalog"""
    ids = sorted({(r[0][9:11], r[0][11:14]) for r in records.values()})
    me = (l1[9:11], l1[11:14])
    if me not in ids or len(ids) > len(LAUNCHES):
        return None
    year, name, date, note = LAUNCHES[ids.index(me)]
    if year[2:] != me[0]:
        return None
    return name, date, note, l1[14:17].strip()


def utc(t):
    return datetime.datetime.fromtimestamp(t, UTC)


def catalog_shift(sat, ts, shift=0.0):
    """predicted Doppler shift (Hz), elevation and range from home for unix times ts"""
    out = [catalog.look(sat, utc(t + shift), *catalog.HOME) for t in ts]
    return (np.array([-o[3] / catalog.C * F_HZ for o in out]), np.array([o[0] for o in out]),
            np.array([o[2] for o in out]))


def orbit_shift(orbit, home_xyz, ts):
    ts = np.asarray(ts, float)
    h = np.asarray(home_xyz, float)
    r0 = np.linalg.norm(orbit.ecef(ts) - h, axis=1)
    r1 = np.linalg.norm(orbit.ecef(ts + 1.0) - h, axis=1)
    return -(r1 - r0) / catalog.C * F_HZ


def subsample(a, n):
    return a if len(a) <= n else a[np.linspace(0, len(a) - 1, n).round().astype(int)]


def analyze_curve(pts, name, sats, orbit, home_xyz):
    """everything the Doppler tab shows for one measured curve"""
    pts = np.array(sorted(pts), float)
    t, hz = pts[:, 0], pts[:, 1]
    res = {'t': t, 'hz': hz, 'y': hz - np.median(hz), 'lines': [], 'tca': None}
    sat = next((s for s in sats if s.name == name), None)
    sub = subsample(pts, 40)
    t0, t1 = t[0], t[-1]
    line_t = np.linspace(t0 - 60, t1 + 60, 160)
    if sat is not None:
        best = None
        for sh in range(-20, 21, 2):            # the capture clock may be off by a few seconds
            pred = catalog_shift(sat, sub[:, 0], sh)[0]
            d = sub[:, 1] - pred
            rms = float(np.sqrt(np.mean((d - d.mean()) ** 2)))
            if best is None or rms < best[0]:
                best = (rms, sh)
        res['shift'] = best[1]
        pred_pts = catalog_shift(sat, t, best[1])[0]
        off = float(np.mean(hz - pred_pts))
        res['y'] = hz - off
        res['rms'] = float(np.sqrt(np.mean((hz - off - pred_pts) ** 2)))
        ly, lel, lrng = catalog_shift(sat, line_t, best[1])
        up = lel >= -1
        res['lines'].append((line_t[up], ly[up], C_CATALOG, 'public catalog prediction for %s' % name, True))
        # closest approach: where the predicted shift crosses zero (from + to -)
        cross = np.where((ly[:-1] > 0) & (ly[1:] <= 0))[0]
        if len(cross):
            i = cross[0]
            k = ly[i] / (ly[i] - ly[i + 1])
            tca = line_t[i] + k * (line_t[i + 1] - line_t[i])
            _, el, rng = catalog_shift(sat, [tca], best[1])
            res['tca'] = (tca, float(el[0]), float(rng[0]))
        rate = np.abs(np.diff(ly) / np.diff(line_t))
        res['max_rate'] = float(rate[up[1:]].max()) if up[1:].any() else None
        # how much better it fits than any other satellite
        fits = catalog.match_doppler(sats, [(utc(a), b) for a, b in sub], [best[1]])
        res['ranking'] = fits[:4]
    if orbit is not None:
        ours = orbit_shift(orbit, home_xyz, t)
        off = float(np.mean(hz - ours))
        res['rms_ours'] = float(np.sqrt(np.mean((hz - off - ours) ** 2)))
        if sat is None:
            res['y'] = hz - off
        shown_off = float(np.mean(hz - res['y']))      # the plot shows hz minus this
        res['lines'].append((line_t, orbit_shift(orbit, home_xyz, line_t) + off - shown_off, C_OURS, 'our own orbit', False))
    return res


def nice_step(span, n=6):
    raw = span / max(n, 1)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


# ---------------------------------------------------------------- the plot
class DopplerPlot(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(280)
        self.res = None

    def set_curve(self, res):
        self.res = res
        self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor('#14171c'))
        r = self.res
        font = p.font()
        font.setPointSize(8)
        p.setFont(font)
        if r is None:
            p.setPen(QtGui.QColor(MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, 'No Doppler curve recorded for this satellite yet.')
            return
        L, R, T, B = 58, 14, 14, 30
        w, h = self.width() - L - R, self.height() - T - B
        xs = [r['t']] + [ln[0] for ln in r['lines']]
        ys = [r['y']] + [ln[1] for ln in r['lines'] if len(ln[1])]
        tmin, tmax = min(a.min() for a in xs if len(a)), max(a.max() for a in xs if len(a))
        ymin, ymax = min(a.min() for a in ys) / 1e3, max(a.max() for a in ys) / 1e3
        pad = max(0.5, (ymax - ymin) * 0.08)
        ymin, ymax = ymin - pad, ymax + pad
        X = lambda tt: L + (tt - tmin) / max(tmax - tmin, 1) * w
        Y = lambda k: T + (ymax - k) / (ymax - ymin) * h
        # grid
        p.setPen(QtGui.QPen(QtGui.QColor(GRID), 1))
        step = nice_step(ymax - ymin)
        k = math.ceil(ymin / step) * step
        while k <= ymax:
            p.drawLine(QPointF(L, Y(k)), QPointF(L + w, Y(k)))
            p.setPen(QtGui.QColor(MUTED))
            p.drawText(QtCore.QRectF(0, Y(k) - 8, L - 6, 16), Qt.AlignRight | Qt.AlignVCenter,
                       ('%+.0f' if step >= 1 else '%+.1f') % k if abs(k) > 1e-9 else '0')
            p.setPen(QtGui.QPen(QtGui.QColor(GRID), 1))
            k += step
        tstep = nice_step(tmax - tmin, 6)
        for s in (15, 30, 60, 120, 300, 600, 900, 1800):
            if s >= tstep:
                tstep = s
                break
        tt = math.ceil(tmin / tstep) * tstep
        while tt <= tmax:
            p.setPen(QtGui.QPen(QtGui.QColor(GRID), 1))
            p.drawLine(QPointF(X(tt), T), QPointF(X(tt), T + h))
            p.setPen(QtGui.QColor(MUTED))
            p.drawText(QtCore.QRectF(X(tt) - 40, T + h + 4, 80, 14), Qt.AlignCenter,
                       datetime.datetime.fromtimestamp(tt).strftime('%I:%M:%S').lstrip('0'))
            tt += tstep
        p.setPen(QtGui.QPen(QtGui.QColor(MUTED), 1))
        if ymin < 0 < ymax:
            p.drawLine(QPointF(L, Y(0)), QPointF(L + w, Y(0)))
        p.save()
        p.translate(12, T + h / 2)
        p.rotate(-90)
        p.drawText(QtCore.QRectF(-80, -8, 160, 16), Qt.AlignCenter, 'Doppler shift (kHz)')
        p.restore()
        # closest approach
        if r['tca']:
            x = X(r['tca'][0])
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 90), 1, Qt.DashLine))
            p.drawLine(QPointF(x, T), QPointF(x, T + h))
            p.setPen(QtGui.QColor(MUTED))
            p.drawText(QPointF(x + 4, T + 12), 'closest approach')
        # predicted curves, then the measurements on top
        for lt, ly, col, label, dashed in r['lines']:
            if len(lt) < 2:
                continue
            path = QtGui.QPainterPath(QPointF(X(lt[0]), Y(ly[0] / 1e3)))
            for a, b in zip(lt[1:], ly[1:]):
                path.lineTo(X(a), Y(b / 1e3))
            p.setPen(QtGui.QPen(col, 2, Qt.DashLine if dashed else Qt.SolidLine))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 160), 1))
        p.setBrush(C_MEASURED)
        for a, b in zip(r['t'], r['y']):
            p.drawEllipse(QPointF(X(a), Y(b / 1e3)), 3.2, 3.2)
        # legend
        items = [(C_MEASURED, 'measured by your receiver', None)] + [(c, lab, d) for _, _, c, lab, d in r['lines']]
        y0 = T + 6
        for col, lab, dashed in items:
            x0 = L + w - 250
            if dashed is None:
                p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 160), 1))
                p.setBrush(col)
                p.drawEllipse(QPointF(x0 + 9, y0 + 6), 3.2, 3.2)
            else:
                p.setPen(QtGui.QPen(col, 2, Qt.DashLine if dashed else Qt.SolidLine))
                p.drawLine(QPointF(x0, y0 + 6), QPointF(x0 + 18, y0 + 6))
            p.setPen(QtGui.QColor(TEXT))
            p.drawText(QPointF(x0 + 24, y0 + 10), lab)
            y0 += 16


# ---------------------------------------------------------------- the window
def _facts(rows):
    return '<table cellspacing="0" cellpadding="3">%s</table>' % ''.join(
        '<tr><td style="color:%s; padding-right:14px" valign="top">%s</td><td>%s</td></tr>' % (MUTED, a, b)
        for a, b in rows)


class SatInfoDialog(QtWidgets.QDialog):
    def __init__(self, tracker):
        super().__init__(tracker)
        self.tracker = tracker
        self.name = None
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.title = QtWidgets.QLabel()
        self.title.setTextFormat(Qt.RichText)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)

        self.about = self._text_tab('About')

        dop = QtWidgets.QWidget()
        dl = QtWidgets.QVBoxLayout(dop)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel('Pass:'))
        self.pass_box = QtWidgets.QComboBox()
        self.pass_box.currentIndexChanged.connect(self._show_curve)
        row.addWidget(self.pass_box, 1)
        dl.addLayout(row)
        self.plot = DopplerPlot()
        dl.addWidget(self.plot, 1)
        self.dop_text = QtWidgets.QLabel()
        self.dop_text.setWordWrap(True)
        self.dop_text.setTextFormat(Qt.RichText)
        dl.addWidget(self.dop_text)
        help_ = QtWidgets.QLabel('<span style="color:%s">%s</span>' % (MUTED, DOPPLER_HELP))
        help_.setWordWrap(True)
        dl.addWidget(help_)
        self.tabs.addTab(dop, 'Doppler curve')

        heard = QtWidgets.QWidget()
        hl = QtWidgets.QVBoxLayout(heard)
        self.heard_table = QtWidgets.QTableWidget(0, 6)
        self.heard_table.setHorizontalHeaderLabels(['When', 'Length', 'Slot frames', 'Position fixes', 'Best fix signal', 'Highest heard'])
        self.heard_table.verticalHeader().setVisible(False)
        self.heard_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.heard_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.heard_table.horizontalHeader().setStretchLastSection(True)
        self.heard_table.cellDoubleClicked.connect(self._go_to_pass)
        hl.addWidget(self.heard_table, 1)
        self.heard_note = QtWidgets.QLabel()
        self.heard_note.setWordWrap(True)
        self.heard_note.setTextFormat(Qt.RichText)
        hl.addWidget(self.heard_note)
        self.tabs.addTab(heard, 'Heard')

        self.orbit_text = self._text_tab('Orbit')

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(self.title)
        lay.addWidget(self.tabs, 1)
        self.resize(780, 680)
        self.curves = []
        self.heard_passes = []
        self.dop_cache = {}

    def _text_tab(self, label):
        w = QtWidgets.QTextBrowser()
        w.setOpenExternalLinks(True)
        w.setFrameShape(QtWidgets.QFrame.NoFrame)
        w.setStyleSheet('QTextBrowser { background: transparent; }')
        self.tabs.addTab(w, label)
        return w

    # ---- filling it in
    def show_satellite(self, name):
        self.name = name
        self.dop_cache = {}
        self.setWindowTitle('%s - satellite info' % name)
        d = self.tracker.data
        self.info = d.info.get(name, {'fixes': 0, 'net_ids': set(), 'slots': set(), 'last': 0, 'best': 0, 'how': set()})
        self.records = tle_records()
        self.tle = self.records.get(name)
        self.sats = d.tles()
        self.orbit = d.orbits.get(name)
        how = ' + '.join(sorted(self.info['how'])) or 'not identified'
        self.title.setText('<span style="font-size:15pt; font-weight:600">%s</span>'
                           '&nbsp;&nbsp;<span style="color:%s">Iridium NEXT &middot; identified by %s</span>'
                           % (name, MUTED, how.replace('position', 'position fix')))
        self._fill_about()
        self._fill_doppler()
        self._fill_heard()
        self._fill_orbit()

    def _fill_about(self):
        i, name = self.info, self.name
        rows = []
        if self.tle:
            l1, l2 = self.tle
            rows.append(('NORAD number', l1[2:7].strip()))
            rows.append(('International ID', '%s-%s%s' % (('20' + l1[9:11]), l1[11:14], l1[14:17].strip())))
            lau = launch_of(self.records, l1)
            if lau:
                rows.append(('Launched', '<b>%s</b> on the %s mission (SpaceX Falcon 9, Vandenberg, California) '
                                         '- piece %s of that launch' % (lau[1], lau[0], lau[3])))
                rows.append(('', '<span style="color:%s">%s</span>' % (MUTED, lau[2])))
                try:
                    years = (datetime.date.today() - datetime.datetime.strptime(lau[1], '%b %d, %Y').date()).days / 365.25
                    rows.append(('In space', '%.1f years' % years))
                except ValueError:
                    pass
        else:
            rows.append(('Catalog', 'not in the downloaded public catalog'))
        rows += BLOCK_FACTS
        if self.tle:
            l2 = self.tle[1]
            n = float(l2[52:63])
            a = (catalog.MU / (n * 2 * math.pi / 86400) ** 2) ** (1 / 3)
            rows.append(('Orbit (catalog)', '%.0f km up, inclined %.1f&deg;, once around every %.1f min (%.1f orbits a day)'
                         % (a - 6371.0, float(l2[8:16]), 1440 / n, n)))
        mine = [('How identified', {'position': 'its ring-alert position fixes matched it',
                                    'Doppler': 'its Doppler curve matched it'}.get(next(iter(i['how'])), '-')
                 if len(i['how']) == 1 else 'position fixes and Doppler curve both matched it' if i['how'] else '-'),
                ('Plane / slot', ', '.join('plane %d, slot %d' % ps for ps in sorted(i['slots'])) or 'not heard yet'),
                ('Network ID', ', '.join('%03d' % x for x in sorted(i['net_ids'])) or '-'),
                ('Position fixes', str(i['fixes'])),
                ('Doppler curves', str(len(self.curves_for()))),
                ('Last heard', self.tracker_fmt(i['last']) if i['last'] else '-'),
                ('Best signal', '%.0f dB' % i['best'] if i['best'] else '-')]
        self.about.setHtml('<h3>The satellite</h3>%s<h3>In your observations</h3>%s'
                           '<p style="color:%s">Launch and hardware facts are general Iridium NEXT information; '
                           'the NORAD number, launch and catalog orbit come from the public CelesTrak list.</p>'
                           % (_facts(rows), _facts(mine), MUTED))

    def tracker_fmt(self, t):
        import iridium_tracker as it
        return it.fmt_time(t)

    def curves_for(self):
        """[(kind, [(t, Hz)])] measured for this satellite"""
        d, name = self.tracker.data, self.name
        out = [('time & location frames', list(g)) for g in d.doppler.get(name, []) if len(g) >= 2]
        by_cap = {}
        for f in d.all_fixes():
            if f['name'] == name and f.get('hz'):
                by_cap.setdefault(f['cap'], []).append((f['t'], f['hz']))
        out += [('ring alerts', g) for g in by_cap.values() if len(g) >= 2]
        return sorted(out, key=lambda c: -max(t for t, _ in c[1]))

    def _fill_doppler(self):
        self.curves = self.curves_for()
        self.pass_box.blockSignals(True)
        self.pass_box.clear()
        for kind, g in self.curves:
            ts = [t for t, _ in g]
            self.pass_box.addItem('%s  ·  %s long  ·  %d points  ·  %s' % (
                self.tracker_fmt(min(ts)), _dur(max(ts) - min(ts)), len(g), kind))
        self.pass_box.blockSignals(False)
        self._show_curve(0)

    def _show_curve(self, idx):
        if not self.curves or idx < 0:
            self.plot.set_curve(None)
            self.dop_text.setText('No Doppler curve for this satellite yet: one needs at least 2 frames '
                                  'from the same pass.')
            return
        if idx not in self.dop_cache:
            QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                import iridium_tracker as it
                self.dop_cache[idx] = analyze_curve(self.curves[idx][1], self.name, self.sats, self.orbit, it._HOME_XYZ)
            finally:
                QtWidgets.QApplication.restoreOverrideCursor()
        r = self.dop_cache[idx]
        self.plot.set_curve(r)
        lines = []
        y = r['y'] / 1e3
        lines.append('Measured shift ran from <b>%+.1f</b> to <b>%+.1f kHz</b> over %s (%d points).'
                     % (y[0], y[-1], _dur(r['t'][-1] - r['t'][0]), len(y)))
        if r['tca']:
            lines.append('Closest approach at <b>%s</b>, %.0f&deg; above your horizon, %.0f km away%s.'
                         % (datetime.datetime.fromtimestamp(r['tca'][0]).strftime('%I:%M:%S %p').lstrip('0'),
                            r['tca'][1], r['tca'][2],
                            ' - fastest change %.0f Hz per second' % r['max_rate'] if r.get('max_rate') else ''))
        if 'rms' in r and len(y) < 3:
            lines.append('<span style="color:%s">Only %d points - too few to judge how well it matches '
                         '(any curve fits 2 points).</span>' % (MUTED, len(y)))
        elif 'rms' in r:
            rank = r.get('ranking') or []
            others = [(rms, n) for rms, n in rank if n != self.name]
            txt = 'Matches the public catalog\'s curve for %s to within <b>%.0f Hz</b> rms' % (self.name, r['rms'])
            if others:
                txt += ' - the next-best satellite, %s, is off by %.0f Hz (%.1f&times; worse)' % (
                    others[0][1], others[0][0], others[0][0] / max(r['rms'], 1))
            if r.get('shift'):
                txt += '. Capture clock corrected by %+d s' % r['shift']
            lines.append(txt + '.')
        if 'rms_ours' in r and len(y) >= 3:
            lines.append('Our own orbit for it reproduces this curve to within <b>%.0f Hz</b> rms.' % r['rms_ours'])
        self.dop_text.setText('<br>'.join(lines))

    def _fill_heard(self):
        d, name = self.tracker.data, self.name
        obs = [(t, 'frame', None) for kind, g in self.curves if kind == 'time & location frames' for t, _ in g]
        fixes = [f for f in d.all_fixes() if f['name'] == name]
        obs += [(f['t'], 'fix', f) for f in fixes]
        obs.sort(key=lambda o: o[0])
        passes, cur = [], []
        for o in obs:
            if cur and o[0] - cur[-1][0] > 900:
                passes.append(cur)
                cur = []
            cur.append(o)
        if cur:
            passes.append(cur)
        passes.reverse()
        self.heard_passes = passes
        tb = self.heard_table
        tb.setRowCount(len(passes))
        import iridium_tracker as it
        for r, ps in enumerate(passes):
            t0, t1 = ps[0][0], ps[-1][0]
            fx = [o[2] for o in ps if o[1] == 'fix']
            best = max((f['snr'] for f in fx), default=None)
            high = None
            if self.orbit is not None:
                tt = np.linspace(t0, max(t1, t0 + 1), 30)
                high = float(it.from_home(*self.orbit.ecef(tt).T)[0].max())
            vals = [it.fmt_time(t0), _dur(t1 - t0), str(sum(1 for o in ps if o[1] == 'frame')), str(len(fx)),
                    '%.0f dB' % best if best else '-', '%.0f°' % high if high is not None else '-']
            for c, v in enumerate(vals):
                tb.setItem(r, c, QtWidgets.QTableWidgetItem(v))
        tb.resizeColumnsToContents()
        self.heard_note.setText('<span style="color:%s">%d pass%s heard. Double-click one to jump the timeline '
                                'to it. "Highest heard" is how high it was while you heard it, from our own orbit '
                                '(when we have one).</span>' % (MUTED, len(passes), '' if len(passes) == 1 else 'es'))

    def _go_to_pass(self, row, _col):
        ps = self.heard_passes[row]
        tr = self.tracker
        tr.timeline.show_range(ps[0][0] - 900, ps[-1][0] + 900)
        tr.scrub(ps[0][0])

    def _fill_orbit(self):
        import iridium_tracker as it
        o, now = self.orbit, self.tracker.t
        if o is None:
            why = self.tracker.data.no_orbit.get(self.name, '')
            self.orbit_text.setHtml('<h3>No orbit of our own yet</h3><p>%s</p><p style="color:%s">Our orbits come only '
                                    'from your own observations: position fixes from its ring alerts, and Doppler '
                                    'curves. One more position fix usually does it.</p>'
                                    % ('Reason: %s.' % why if why else '', MUTED))
            return
        node, inc = o.plane_at(now)
        a = o.p[4]
        oi = o.info
        np_ = self.tracker.next_pass(self.name)
        rows = [('Height', '%.0f km' % (a - 6371.0)),
                ('Inclination', '%.2f&deg;' % math.degrees(inc)),
                ('Period', '%.2f min' % o.period_min),
                ('Uncertainty now', '&plusmn;%.0f km (at %s)' % (o.sigma_km(now)[0], it.fmt_time(now))),
                ('Plane position', '&plusmn;%.0f km' % o.plane_sigma_km),
                ('Built from', '%d position fixes over %d passes, %d Doppler curves (%d points)'
                 % (oi['fixes'], oi['passes'], oi['doppler_groups'], oi['doppler_points'])),
                ('Fit quality', 'position fixes agree to %.1f km rms' % oi['fix_rms_km']),
                ('Observed', '%s to %s' % (it.fmt_time(oi['first_obs']), it.fmt_time(oi['last_obs']))),
                ('Next pass', 'not within the time our orbit is good for' if np_ is None else
                 'now (up to %.0f&deg;)' % np_[2] if np_[3] else
                 '%s, up to %.0f&deg;' % (it.fmt_time(np_[0]), np_[2]))]
        comp = []
        sat = next((s for s in self.sats if s.name == self.name), None)
        if sat is not None:
            X = np.array(sat.ecef(utc(now)))
            diff = float(np.linalg.norm(o.ecef(now)[0] - X))
            comp = [('Where it is now', 'our orbit and the public catalog differ by <b>%.0f km</b> '
                                        '(%s our &plusmn;%.0f km uncertainty)'
                     % (diff, 'within' if diff <= 2 * o.sigma_km(now)[0] else 'outside', o.sigma_km(now)[0])),
                    ('Catalog inclination', '%.2f&deg;' % math.degrees(sat.i)),
                    ('Catalog period', '%.2f min' % (2 * math.pi / sat.n / 60))]
        self.orbit_text.setHtml('<h3>Our orbit (from your observations only)</h3>%s'
                                '<h3>Compared with the public catalog</h3>%s'
                                '<p style="color:%s">A circular orbit with the Earth\'s flattening (J2) taken into '
                                'account, fitted to your fixes and Doppler curves. The catalog is only used here as a '
                                'check.</p>' % (_facts(rows), _facts(comp) if comp else '<p>Not in the catalog.</p>', MUTED))


def _dur(s):
    s = int(round(s))
    return '%d s' % s if s < 90 else '%d min %02d s' % (s // 60, s % 60) if s < 3600 else '%.1f h' % (s / 3600)
