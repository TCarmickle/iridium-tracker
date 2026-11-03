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
import systems

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

# Orbcomm launches by year (the catalog's launch ID starts with the year)
ORBCOMM_LAUNCHES = {
    '14': ('OG2 Mission 1', 'Jul 14, 2014', 'SpaceX Falcon 9, Cape Canaveral, Florida',
           'The first six second-generation (OG2) Orbcomm satellites.'),
    '15': ('OG2 Mission 2', 'Dec 21, 2015', 'SpaceX Falcon 9, Cape Canaveral, Florida',
           'Eleven OG2 satellites - and the first time a Falcon 9 booster landed back on Earth, at Cape Canaveral.'),
}
ORBCOMM_FACTS = [
    ('Block', 'Orbcomm Generation 2 (OG2), which joined the remaining first-generation satellites of the 1990s'),
    ('Builder', 'Sierra Nevada Corporation'),
    ('What it does', 'Short two-way data messages for machines - trucks, shipping containers, pipelines, buoys, '
                     'farm equipment: one of the first "Internet of Things" satellite networks'),
    ('Downlink', '137-138 MHz VHF at 4800 bit/s - the packets you decode, including its own position '
                 '(ephemeris) every few seconds'),
    ('Hosted payload', 'AIS receiver: picks up ships\' position broadcasts for worldwide ship tracking'),
]
ORBCOMM_OG1_FACTS = [
    ('Block', 'Orbcomm first generation (OG1), launched 1995-1999 by Orbital Sciences'),
    ('What it does', 'Short two-way data messages for machines: one of the first "Internet of Things" '
                     'satellite networks'),
    ('Downlink', '137-138 MHz VHF'),
]

ISS_FACTS = [
    ('What it is', 'The International Space Station: a crewed laboratory about the size of a football field, '
                   'its first module (Zarya) launched Nov 20, 1998'),
    ('Ham radio on board', 'ARISS - Amateur Radio on the ISS: an APRS digipeater on 145.825 MHz that repeats '
                           'packets from hams on the ground, crew voice contacts with schools on 145.800 MHz, '
                           'and slow-scan TV picture events on 145.800 MHz'),
    ('Callsigns', 'RS0ISS (Russian segment), NA1SS (US segment), digipeater alias ARISS'),
]
HAM_FACTS = [
    ('What it is', 'An amateur (ham radio) satellite: most are CubeSats built by universities and radio clubs, '
                   'and their telemetry is sent openly for anyone to receive'),
    ('Decoding', 'gr-satellites (Daniel Estevez), which knows each satellite\'s modulation and framing'),
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

GPS_FACTS = [
    ('What it does', 'Navigation: it broadcasts its own precise time and orbit, so a receiver that hears four or more '
                     'satellites can work out where it is'),
    ('Signal you receive', 'L1 C/A at 1575.42 MHz, about 20 dB under the noise - found by correlating with this '
                           'satellite\'s own code (its PRN number). Its broadcast orbit (ephemeris) comes every 30 s'),
    ('Constellation', 'about 31 working satellites in 6 orbital planes at 20,200 km, inclined 55 degrees, '
                      'once around every 11 h 58 min (twice a day): the same ground track every day'),
    ('Operator', 'US Space Force'),
]
GPS_BLOCKS = {
    'BIIR': 'Block IIR (launched 1997-2004, built by Lockheed Martin)',
    'BIIRM': 'Block IIR-M (launched 2005-2009, Lockheed Martin) - added the civil L2C signal',
    'BIIF': 'Block IIF (launched 2010-2016, built by Boeing) - added the L5 signal',
    'BIII': 'GPS III (launched from 2018, built by Lockheed Martin) - adds the L1C signal shared with Galileo',
}
GALILEO_FACTS = [
    ('What it does', 'Europe\'s navigation system: like GPS it broadcasts its own time and orbit, and receivers use '
                     'both together'),
    ('Signal you receive', 'E1 at 1575.42 MHz - the same frequency as GPS L1, with its own code per satellite '
                           '(E1-B). Its broadcast orbit (I/NAV) comes every 30 s'),
    ('Constellation', 'about 27 working satellites in 3 orbital planes at 23,200 km, inclined 56 degrees, once '
                      'around every 14 h 05 min'),
    ('Builder', 'OHB System (most), with Surrey Satellite Technology; the first four by Airbus / Thales Alenia'),
    ('Operator', 'European Union (EUSPA)'),
]

DOPPLER_HELP = (
    '<b>What is a Doppler curve?</b> As the satellite comes towards you its signal arrives a little higher '
    'in frequency; as it goes away, a little lower. Plotting that shift over a pass gives an S-shaped curve. '
    'It crosses zero at the moment of <b>closest approach</b>. A pass nearly overhead gives a steep S '
    '(the shift flips quickly); a distant, low pass gives a gentle one. At 1626 MHz the shift can reach '
    'about &plusmn;36 kHz (Iridium); at 137 MHz about &plusmn;3 kHz (Orbcomm); GPS and Galileo, much higher and '
    'slower, only about &plusmn;4-5 kHz at 1575 MHz, over hours. Every satellite\'s pass over you has its own curve, so matching the measured '
    'points to the predicted curves identifies which satellite you heard - and the curves also help fit '
    'our own orbit.')


# ---------------------------------------------------------------- public catalog helpers
def tle_records(system='iridium'):
    """name -> (line 1, line 2) from the public catalog file of a system"""
    prefix = (systems.get(system)['prefix'] or '').strip()
    for path in (catalog.tle_file(system), catalog.SATDUMP_TLES):
        try:
            lines = open(path).read().splitlines()
        except OSError:
            continue
        found = {lines[i].strip(): (lines[i + 1], lines[i + 2])
                 for i in range(len(lines) - 2) if lines[i].startswith(prefix) and lines[i + 1].startswith('1 ')}
        if found:
            return found
    return {}


def launch_of(records, l1, system='iridium'):
    """(launch name, date, note, piece, rocket) for a TLE line 1. Iridium: from the order of launches in
    the catalog; Orbcomm: from the launch year"""
    if system == 'orbcomm':
        hit = ORBCOMM_LAUNCHES.get(l1[9:11])
        return (hit[0], hit[1], hit[3], l1[14:17].strip(), hit[2]) if hit else None
    ids = sorted({(r[0][9:11], r[0][11:14]) for r in records.values()})
    me = (l1[9:11], l1[11:14])
    if me not in ids or len(ids) > len(LAUNCHES):
        return None
    year, name, date, note = LAUNCHES[ids.index(me)]
    if year[2:] != me[0]:
        return None
    return name, date, note, l1[14:17].strip(), 'SpaceX Falcon 9, Vandenberg, California'


def utc(t):
    return datetime.datetime.fromtimestamp(t, UTC)


def catalog_shift(sat, ts, shift=0.0, carrier=F_HZ):
    """predicted Doppler shift (Hz), elevation and range from home for unix times ts"""
    out = [catalog.look(sat, utc(t + shift), *catalog.HOME) for t in ts]
    return (np.array([-o[3] / catalog.C * carrier for o in out]), np.array([o[0] for o in out]),
            np.array([o[2] for o in out]))


def orbit_shift(orbit, home_xyz, ts, carrier=F_HZ):
    ts = np.asarray(ts, float)
    h = np.asarray(home_xyz, float)
    r0 = np.linalg.norm(orbit.ecef(ts) - h, axis=1)
    r1 = np.linalg.norm(orbit.ecef(ts + 1.0) - h, axis=1)
    return -(r1 - r0) / catalog.C * carrier


def subsample(a, n):
    return a if len(a) <= n else a[np.linspace(0, len(a) - 1, n).round().astype(int)]


def analyze_curve(pts, name, sats, orbit, home_xyz, system=systems.IRIDIUM):
    """everything the Doppler tab shows for one measured curve"""
    carrier = system['carrier_hz'] or float(np.median([h for _, h in pts]))
    pts = np.array(sorted(pts), float)
    t, hz = pts[:, 0], pts[:, 1]
    res = {'t': t, 'hz': hz, 'y': hz - np.median(hz), 'lines': [], 'tca': None}
    sat = next((s for s in sats if s.name == name), None)
    sub = subsample(pts, 40)
    t0, t1 = t[0], t[-1]
    line_t = np.linspace(t0 - 60, t1 + 60, 160)
    if sat is not None:
        best = None
        for sh in ([0] if system is systems.GNSS else range(-20, 21, 2)):   # the capture clock may be off a few s
            pred = catalog_shift(sat, sub[:, 0], sh, carrier)[0]
            d = sub[:, 1] - pred
            rms = float(np.sqrt(np.mean((d - d.mean()) ** 2)))
            if best is None or rms < best[0]:
                best = (rms, sh)
        res['shift'] = best[1]
        pred_pts = catalog_shift(sat, t, best[1], carrier)[0]
        off = float(np.mean(hz - pred_pts))
        res['y'] = hz - off
        res['rms'] = float(np.sqrt(np.mean((hz - off - pred_pts) ** 2)))
        ly, lel, lrng = catalog_shift(sat, line_t, best[1], carrier)
        up = lel >= -1
        res['lines'].append((line_t[up], ly[up], C_CATALOG, 'public catalog prediction', True))
        # closest approach: where the predicted shift crosses zero (from + to -)
        cross = np.where((ly[:-1] > 0) & (ly[1:] <= 0))[0]
        if len(cross):
            i = cross[0]
            k = ly[i] / (ly[i] - ly[i + 1])
            tca = line_t[i] + k * (line_t[i + 1] - line_t[i])
            _, el, rng = catalog_shift(sat, [tca], best[1], carrier)
            res['tca'] = (tca, float(el[0]), float(rng[0]))
        rate = np.abs(np.diff(ly) / np.diff(line_t))
        res['max_rate'] = float(rate[up[1:]].max()) if up[1:].any() else None
        # how much better it fits than any other satellite (Iridium satellites are named this way;
        # Orbcomm ones by the position they report)
        if system is systems.IRIDIUM:
            fits = catalog.match_doppler(sats, [(utc(a), b) for a, b in sub], [best[1]])
            res['ranking'] = fits[:4]
    if orbit is not None:
        ours = orbit_shift(orbit, home_xyz, t, carrier)
        off = float(np.mean(hz - ours))
        res['rms_ours'] = float(np.sqrt(np.mean((hz - off - ours) ** 2)))
        if sat is None:
            res['y'] = hz - off
        shown_off = float(np.mean(hz - res['y']))      # the plot shows hz minus this
        res['lines'].append((line_t, orbit_shift(orbit, home_xyz, line_t, carrier) + off - shown_off, C_OURS, 'our own orbit', False))
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
        y0 = T + 20
        for col, lab, dashed in items:
            x0 = L + w - 210
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
        super().__init__(None)      # its own window: taskbar button, minimize/maximize (closed with the main window)
        self.tracker = tracker
        self.name = None
        self.setWindowFlags(Qt.Window | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
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
        self.heard_table.setHorizontalHeaderLabels(['When', 'Length', 'Received', 'Reported positions',
                                                    'Best position signal', 'Highest heard'])
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
        self.system = systems.of_name(name)
        self.records = tle_records(self.system['key'])
        self.orbit = d.orbits.get(name)
        # GNSS: our name ('GPS G06') is not the catalog's ('GPS BIIF-6  (PRN 06)')
        self.cat_name = (d.gnss_catalog_name(name) if self.system is systems.GNSS else name) or name
        self.tle = self.records.get(self.cat_name)
        self.sats = d.tles(self.system['key'])
        self.orbit = d.orbits.get(name)
        how = ' + '.join(sorted(self.info['how'])) or 'not identified'
        self.title.setText('<span style="font-size:15pt; font-weight:600">%s</span>'
                           '&nbsp;&nbsp;<span style="color:%s">%s &middot; identified by %s</span>'
                           % (name, MUTED, self.block(), how.replace('position', 'position fix')
                              .replace('tracked', 'its own ranging code')))
        self._fill_about()
        self._fill_doppler()
        self._fill_heard()
        self._fill_orbit()

    def block(self):
        if self.system is systems.GNSS:
            if self.name.startswith('Galileo'):
                return 'Galileo'
            return 'GPS ' + GPS_BLOCKS.get(self._gps_block(), 'satellite').split(' (')[0]
        if self.system is systems.HAM:
            return 'International Space Station' if self.name.startswith('ISS') else 'Amateur satellite'
        if self.system is systems.ORBCOMM:
            num = int(''.join(ch for ch in self.name if ch.isdigit()) or 0)
            return 'Orbcomm OG2' if num >= 100 else 'Orbcomm OG1'
        return 'Iridium NEXT'

    def _gps_block(self):
        """'BIIF' from the catalog name 'GPS BIIF-6  (PRN 06)'"""
        return self.cat_name.split()[1].split('-')[0] if self.cat_name.startswith('GPS ') else ''

    def _fill_about(self):
        i, name = self.info, self.name
        orbcomm = self.system is systems.ORBCOMM
        rows = []
        if self.tle:
            l1, l2 = self.tle
            rows.append(('NORAD number', l1[2:7].strip()))
            century = '19' if int(l1[9:11]) > 56 else '20'
            rows.append(('International ID', '%s-%s%s' % ((century + l1[9:11]), l1[11:14], l1[14:17].strip())))
            lau = launch_of(self.records, l1, self.system['key']) \
                if self.system not in (systems.HAM, systems.GNSS) else None
            if (self.system is systems.HAM and not self.name.startswith('ISS')) or self.system is systems.GNSS:
                rows.append(('Launched', 'in %s%s (launch %s of that year)' % (century, l1[9:11], l1[11:14].lstrip('0'))))
            if lau:
                rows.append(('Launched', '<b>%s</b> on the %s mission (%s) - piece %s of that launch'
                             % (lau[1], lau[0], lau[4], lau[3])))
                rows.append(('', '<span style="color:%s">%s</span>' % (MUTED, lau[2])))
                try:
                    years = (datetime.date.today() - datetime.datetime.strptime(lau[1], '%b %d, %Y').date()).days / 365.25
                    rows.append(('In space', '%.1f years' % years))
                except ValueError:
                    pass
        else:
            rows.append(('Catalog', 'not in the downloaded public catalog'))
        if self.system is systems.HAM:
            rows += ISS_FACTS if self.name.startswith('ISS') else HAM_FACTS
        elif self.system is systems.GNSS:
            if self.name.startswith('Galileo'):
                rows += GALILEO_FACTS
            else:
                if self._gps_block() in GPS_BLOCKS:
                    rows.append(('Block', GPS_BLOCKS[self._gps_block()]))
                rows += GPS_FACTS
            if self.tle:
                rows.insert(0, ('Catalog name', self.cat_name))
        else:
            rows += (ORBCOMM_FACTS if self.block() == 'Orbcomm OG2' else ORBCOMM_OG1_FACTS) if orbcomm else BLOCK_FACTS
        if self.tle:
            l2 = self.tle[1]
            n = float(l2[52:63])
            a = (catalog.MU / (n * 2 * math.pi / 86400) ** 2) ** (1 / 3)
            rows.append(('Orbit (catalog)', '%.0f km up, inclined %.1f&deg;, once around every %.1f min (%.1f orbits a day)'
                         % (a - 6371.0, float(l2[8:16]), 1440 / n, n)))
        if self.system is systems.GNSS:
            oi = self.orbit.info if self.orbit is not None else {}
            mine = [('How identified', 'tracked on its own ranging code - the code is the satellite\'s PRN number, '
                                       'so the name is certain'),
                    ('Seconds tracked', str(i.get('frames', 0))),
                    ('Broadcast orbit', 'decoded (reference time %s)' % self.tracker_fmt(oi['toe']) if oi.get('toe')
                     else 'not decoded yet - needs about 30 s of steady tracking'),
                    ('Doppler curves', str(len(self.curves_for()))),
                    ('Last heard', self.tracker_fmt(i['last']) if i['last'] else '-'),
                    ('Best signal', '%.1f dB-Hz' % i['best'] if i['best'] else '-')]
        elif self.system is systems.HAM:
            ids = [('How identified', 'frames decoded with its own decoder (it is the only satellite with that '
                                      'frequency and signal)' if not name.startswith('ISS') else
                    'APRS packets the ISS digipeater repeated (RS0ISS / NA1SS in the path)'),
                   ('Frames decoded', str(i.get('frames', 0))),
                   ('Callsigns heard', ', '.join(i.get('callsigns', [])) or '-')]
        elif orbcomm:
            ids = [('How identified', 'its own position packets (ephemeris) put it where the catalog has this satellite'
                    if i['fixes'] else '-'),
                   ('Satellite ID', ', '.join('%02X' % x for x in sorted(i['net_ids'])) or '-')]
        else:
            ids = [('How identified', {'position': 'its ring-alert position fixes matched it',
                                       'Doppler': 'its Doppler curve matched it'}.get(next(iter(i['how'])), '-')
                    if len(i['how']) == 1 else 'position fixes and Doppler curve both matched it' if i['how'] else '-'),
                   ('Plane / slot', ', '.join('plane %d, slot %d' % ps for ps in sorted(i['slots'])) or 'not heard yet'),
                   ('Network ID', ', '.join('%03d' % x for x in sorted(i['net_ids'])) or '-')]
        if self.system is not systems.GNSS:
            mine = ids + [
                ('Position fixes', str(i['fixes'])),
                ('Doppler curves', str(len(self.curves_for()))),
                ('Last heard', self.tracker_fmt(i['last']) if i['last'] else '-'),
                ('Best signal', '%.0f dB' % i['best'] if i['best'] else '-')]
        self.about.setHtml('<h3>The satellite</h3>%s<h3>In your observations</h3>%s'
                           '<p style="color:%s">Launch and hardware facts are general information about the block; '
                           'the NORAD number, launch and catalog orbit come from the public CelesTrak list.</p>'
                           % (_facts(rows), _facts(mine), MUTED))

    def tracker_fmt(self, t):
        import iridium_tracker as it
        return it.fmt_time(t)

    def curves_for(self):
        """[(kind, [(t, Hz)])] measured for this satellite"""
        d, name = self.tracker.data, self.name
        if self.system is not systems.IRIDIUM:      # Orbcomm packets / ham frames: every one's frequency
            kind = {'orbcomm': 'packets', 'gnss': 'tracking'}.get(self.system['key'], 'frames')
            out = [(kind, list(g))
                   for g in d.doppler.get(name, []) if len(g) >= 2]
            return sorted(out, key=lambda c: -max(t for t, _ in c[1]))
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
                self.dop_cache[idx] = analyze_curve(self.curves[idx][1], self.cat_name, self.sats, self.orbit, it._HOME_XYZ,
                                                    self.system)
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
            others = [(rms, n) for rms, n in rank if n != self.cat_name]
            txt = 'Matches the public catalog\'s curve for %s to within <b>%.0f Hz</b> rms' % (self.cat_name, r['rms'])
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
        obs = [(t, 'frame', None) for kind, g in self.curves if kind in ('time & location frames', 'packets', 'frames', 'tracking')
               for t, _ in g]
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
                                    'from your own observations: positions it reported (Iridium ring alerts, Orbcomm '
                                    'ephemeris), its own broadcast orbit (GPS / Galileo), and Doppler curves.</p>'
                                    % ('Reason: %s.' % why if why else '', MUTED))
            return
        node, inc = o.plane_at(now)
        a = o.p[4]
        oi = o.info
        np_ = self.tracker.next_pass(self.name)
        if oi.get('ephemeris'):
            self._fill_orbit_gnss(o, now, inc, a, np_)
            return
        rows = [('Height', '%.0f km' % (a - 6371.0)),
                ('Inclination', '%.2f&deg;' % math.degrees(inc)),
                ('Period', '%.2f min' % o.period_min),
                ('Uncertainty now', '&plusmn;%.0f km (at %s)' % (o.sigma_km(now)[0], it.fmt_time(now))),
                ('Plane position', '&plusmn;%.0f km' % o.plane_sigma_km),
                ('Built from', '%d position fixes over %d passes, %d Doppler curves (%d points)'
                 % (oi['fixes'], oi['passes'], oi['doppler_groups'], oi['doppler_points'])),
                ('Fit quality', 'Doppler curves alone (no position fixes), fitted to %.0f Hz rms - accepted only after '
                                'a confidence check that no other orbit fits nearly as well' % oi.get('doppler_rms_hz', 0)
                 if oi.get('doppler_only') else 'position fixes agree to %.1f km rms' % oi['fix_rms_km']),
                ('Observed', '%s to %s' % (it.fmt_time(oi['first_obs']), it.fmt_time(oi['last_obs']))),
                ('Next pass', 'not within the time our orbit is good for' if np_ is None else
                 'now (up to %.0f&deg;)' % np_[2] if np_[3] else
                 '%s, up to %.0f&deg;' % (it.fmt_time(np_[0]), np_[2]))]
        comp = []
        sat = next((s for s in self.sats if s.name == self.cat_name), None)
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

    def _fill_orbit_gnss(self, o, now, inc, a, np_):
        """a GNSS satellite's orbit: its own broadcast ephemeris, checked against the public catalog"""
        import iridium_tracker as it
        oi, e = o.info, o.eph
        rows = [('Source', 'its own broadcast orbit (ephemeris), decoded from its signal by PocketSDR'),
                ('Height', '%.0f km' % (a - 6371.0)),
                ('Inclination', '%.2f&deg;' % math.degrees(inc)),
                ('Eccentricity', '%.4f' % e['e']),
                ('Period', '%.2f min' % o.period_min),
                ('Reference time', '%s (good to a few metres for about 2 hours either side)' % it.fmt_time(oi['toe'])),
                ('Received', it.fmt_time(oi['last_obs'])),
                ('Health', 'OK' if not oi.get('health') else 'marked unhealthy by the satellite'),
                ('Uncertainty now', '%s (at %s)' % (it.fmt_sigma(float(o.sigma_km(now)[0])), it.fmt_time(now))),
                ('Next pass', 'not within the time our orbit is good for' if np_ is None else
                 'now (up to %.0f&deg;)' % np_[2] if np_[3] else '%s, up to %.0f&deg;' % (it.fmt_time(np_[0]), np_[2]))]
        comp = []
        sat = next((s for s in self.sats if s.name == self.cat_name), None)
        if sat is not None:
            diff = float(np.linalg.norm(o.ecef(now)[0] - np.array(sat.ecef(utc(now)))))
            comp = [('Where it is now', 'its broadcast orbit and the public catalog differ by <b>%.1f km</b> - the '
                                        'catalog\'s own error at this height is typically a few to tens of km' % diff),
                    ('Catalog inclination', '%.2f&deg;' % math.degrees(sat.i)),
                    ('Catalog period', '%.2f min' % (2 * math.pi / sat.n / 60))]
        self.orbit_text.setHtml('<h3>Its orbit (as the satellite itself broadcasts it)</h3>%s'
                                '<h3>Compared with the public catalog</h3>%s'
                                '<p style="color:%s">The broadcast orbit is what every GPS / Galileo receiver uses '
                                'to work out its position. The catalog is only used here as a check.</p>'
                                % (_facts(rows), _facts(comp) if comp else '<p>Not matched to the catalog.</p>', MUTED))


def _dur(s):
    s = int(round(s))
    return '%d s' % s if s < 90 else '%d min %02d s' % (s // 60, s % 60) if s < 3600 else '%.1f h' % (s / 3600)
