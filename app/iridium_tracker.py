"""Satellite Tracker - a map of the satellites your own antenna has heard (Iridium, Orbcomm, ISS / ham, GPS / Galileo).

Shows two kinds of data, drawn differently:
  measured   red dots    - positions the satellites reported in ring alerts you decoded
  predicted  lines/icons - where those satellites are, from orbits fitted (orbitfit.py) to
                           nothing but your own position fixes and Doppler measurements
A satellite appears once it has sent you a position fix, and fades / disappears as the
time since you last observed it grows and our orbit for it becomes uncertain.
Satellite names are static reference labels; no public orbit data is used.

Run with 3-tracker.bat. Reads catalog-data.json (from catalog.py) and, while
1-receive.bat is running, the live output-*.live file it writes.
Map tiles come from OpenStreetMap and are cached in the tiles folder.
"""
import collections
import concurrent.futures
import datetime
import glob
import json
import math
import os
import re
import sys
import time
import traceback

import numpy as np
import requests
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt, QPointF

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths     # noqa: E402  (where the data lives)
import catalog   # noqa: E402  (capture reading, satellite names)
import storage   # noqa: E402  (keeping the data small)
import data_dialog  # noqa: E402
import lights  # noqa: E402
import sdr_settings  # noqa: E402
import source_dialog  # noqa: E402
import settings_dialog  # noqa: E402  (location, data and storage)
import signalview  # noqa: E402  (the Signal window)
import spectrum  # noqa: E402
import skyplot  # noqa: E402
import globe  # noqa: E402
import satinfo  # noqa: E402
import systems  # noqa: E402
import theme  # noqa: E402  (light / dark colours)
import sound  # noqa: E402
import orbitfit  # noqa: E402  (orbits from our own observations)
import receiver  # noqa: E402  (the SDR receiver and decoder, run from here)
import location_dialog  # noqa: E402
import popout  # noqa: E402  (pop-out windows, the main view)

UTC = datetime.timezone.utc
HOME_LAT, HOME_LON, HOME_H = catalog.HOME or (0.0, 0.0, 0.0)   # set_home() changes it while running
RE, F = 6378.137, 1 / 298.257223563
E2 = F * (2 - F)
MIN_EL = 8.2          # Iridium serves phones down to about 8 degrees elevation
MAX_SIGMA_KM = 1500   # hide a satellite once our orbit for it is more uncertain than this
OMEGA_EARTH = 7.2921151467e-5   # Earth rotation, rad/s
FAINT_SIGMA_KM = 150  # draw it faded beyond this uncertainty
IRIDIUM_FLOOR_KM = 12.0     # what an Iridium orbit from ring-alert positions is off by while heard
IRIDIUM_DRIFT_KM_H = 2.5    # ...and how fast that grows once it's no longer heard (km per hour)
FIT_DAYS = 7          # orbits are fitted to each satellite's most recent week of observations
PLANE_MAX_SIGMA_KM = 300  # a satellite's orbit plane must be known this well to place its plane
PLANE_JOIN_DEG = 5.0      # a satellite without a slot number joins a plane whose node is this close
PLANE_SPACING_DEG = 31.6  # Iridium's planes, numbered 1-6 in order, are this far apart (design value)
LINK_EVERY_S = 30         # how often an unlinked plane/slot is tried against the satellites while receiving
ORBIT_RADIUS = 6378.137 + 781   # Iridium's design altitude, only used to draw the plane rings
PLANE_COLORS = {1: (77, 148, 255), 2: (176, 120, 240), 3: (40, 200, 200), 4: (240, 100, 180),
                5: (215, 150, 80), 6: (150, 200, 60), None: (150, 150, 150)}

# colours: the window's theme (theme.py, light or dark), set by _use_colours() at start and on a switch
BG = PANEL = SURFACE = BORDER = TEXT = MUTED = ACCENT = ALERT = GOOD = ''
IDLE = UP = ALERT_BG = ROW_HEAD = SEL_BG = ''
COLOUR_NAMES = ('BG', 'PANEL', 'SURFACE', 'BORDER', 'TEXT', 'MUTED', 'ACCENT', 'ALERT', 'GOOD', 'IDLE', 'UP',
                'ALERT_BG', 'ROW_HEAD', 'SEL_BG')


def _use_colours():
    """this module's colour names <- the current theme (paint code reads them when it draws)"""
    globals().update({k: theme.PAL[k] for k in COLOUR_NAMES})


_use_colours()


STYLE = """
QWidget { background: %(BG)s; color: %(TEXT)s; font-family: "Segoe UI"; font-size: 9pt; }
QPushButton { background: %(SURFACE)s; border: 1px solid %(BORDER)s; border-radius: 6px; padding: 5px 12px; }
QPushButton:hover { background: %(HOVER)s; border-color: %(HOVER_BORDER)s; }
QPushButton:pressed { background: %(PRESSED)s; }
QPushButton:checked { background: %(ACCENT)s; border-color: %(ACCENT)s; color: white; font-weight: 600; }
QComboBox { background: %(SURFACE)s; border: 1px solid %(BORDER)s; border-radius: 6px; padding: 4px 8px; }
QComboBox:hover { border-color: %(HOVER_BORDER)s; }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView { background: %(SURFACE)s; border: 1px solid %(BORDER)s;
                              selection-background-color: %(ACCENT)s; outline: none; }
QCheckBox { spacing: 6px; }
QTableWidget, QListWidget { background: %(PANEL)s; alternate-background-color: %(ALT)s;
                            border: 1px solid %(BORDER)s; border-radius: 6px; }
QTableWidget { selection-background-color: transparent; selection-color: %(TEXT)s; }
QHeaderView::section { background: %(SURFACE)s; color: %(MUTED)s; border: none;
                       border-bottom: 1px solid %(BORDER)s; padding: 5px 6px; font-weight: 600; }
QScrollArea { background: %(PANEL)s; border: 1px solid %(BORDER)s; border-radius: 6px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle { background: %(SCROLL)s; border-radius: 3px; min-height: 24px; min-width: 24px; }
QScrollBar::handle:hover { background: %(SCROLL_HOVER)s; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }
QSplitter::handle { background: %(BG)s; }
QStatusBar { background: %(STATUS)s; color: %(MUTED)s; }
QStatusBar QLabel { background: transparent; color: %(MUTED)s; }
QToolTip { background: %(SURFACE)s; color: %(TEXT)s; border: 1px solid %(BORDER)s; padding: 4px; }
QTabWidget::pane { border: 1px solid %(BORDER)s; border-radius: 6px; top: -1px; }
QTabBar::tab { background: %(SURFACE)s; color: %(MUTED)s; border: 1px solid %(BORDER)s; padding: 6px 14px;
               border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }
QTabBar::tab:selected { background: %(PANEL)s; color: %(TEXT)s; border-bottom: 2px solid %(ACCENT)s; }
QTabBar::tab:hover:!selected { color: %(TEXT)s; }
"""


def apply_app_colours(app):
    """the whole window in the current theme: this module's colours, the style sheet and the palette"""
    _use_colours()
    pal = QtGui.QPalette()
    P = theme.PAL
    for role, key in ((QtGui.QPalette.Window, 'BG'), (QtGui.QPalette.WindowText, 'TEXT'), (QtGui.QPalette.Base, 'PANEL'),
                      (QtGui.QPalette.AlternateBase, 'ALT'), (QtGui.QPalette.Text, 'TEXT'),
                      (QtGui.QPalette.Button, 'SURFACE'), (QtGui.QPalette.ButtonText, 'TEXT'),
                      (QtGui.QPalette.Highlight, 'ACCENT'), (QtGui.QPalette.ToolTipBase, 'SURFACE'),
                      (QtGui.QPalette.ToolTipText, 'TEXT')):
        pal.setColor(role, QtGui.QColor(P[key]))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor('#ffffff'))
    app.setPalette(pal)
    app.setStyleSheet(STYLE % P)
TILE_DIR = paths.TILES
TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
TILE_HEADERS = {'User-Agent': 'IridiumTracker/1.0 (personal hobby satellite map)'}

C_PAST = QtGui.QColor(243, 156, 18)
C_FUTURE = QtGui.QColor(235, 225, 20)
C_FIX = QtGui.QColor(220, 30, 30)
C_HOME = QtGui.QColor(200, 0, 0)
C_SELECTED_NAME = QtGui.QColor(31, 98, 200)   # the selected satellite's name in the list


# ---------------------------------------------------------------- geometry (vectorized)
def to_lla(X, Y, Z):
    lon = np.degrees(np.arctan2(Y, X))
    lat_gc = np.arctan2(Z, np.hypot(X, Y))
    lat = np.degrees(np.arctan(np.tan(lat_gc) / (1 - E2)))
    alt = np.sqrt(X * X + Y * Y + Z * Z) - RE * (1 - F * np.sin(lat_gc) ** 2)
    return lat, lon, alt


_HOME_XYZ = catalog.site(HOME_LAT, HOME_LON, HOME_H)


def set_home(lat, lon, alt_km):
    """new antenna location: saved to settings.json and used from now on"""
    global HOME_LAT, HOME_LON, HOME_H, _HOME_XYZ
    HOME_LAT, HOME_LON, HOME_H = lat, lon, alt_km
    _HOME_XYZ = catalog.site(lat, lon, alt_km)
    catalog.HOME = (lat, lon, alt_km)
    catalog.save_home(lat, lon, alt_km)


def from_home(X, Y, Z):
    """elevation, azimuth (deg) and range (km) seen from home"""
    dx, dy, dz = X - _HOME_XYZ[0], Y - _HOME_XYZ[1], Z - _HOME_XYZ[2]
    la, lo = math.radians(HOME_LAT), math.radians(HOME_LON)
    rng = np.sqrt(dx * dx + dy * dy + dz * dz)
    S = math.sin(la) * math.cos(lo) * dx + math.sin(la) * math.sin(lo) * dy - math.cos(la) * dz
    Ea = -math.sin(lo) * dx + math.cos(lo) * dy
    Zu = math.cos(la) * math.cos(lo) * dx + math.cos(la) * math.sin(lo) * dy + math.sin(la) * dz
    return np.degrees(np.arcsin(Zu / rng)), (np.degrees(np.arctan2(Ea, -S)) + 360) % 360, rng


def sun_position(t):
    """(subsolar latitude, subsolar longitude) in degrees"""
    n = 2440587.5 + t / 86400 - 2451545.0
    L, g = math.radians(280.460 + 0.9856474 * n), math.radians(357.528 + 0.9856003 * n)
    lam = L + math.radians(1.915) * math.sin(g) + math.radians(0.020) * math.sin(2 * g)
    eps = math.radians(23.439 - 0.0000004 * n)
    ra = math.degrees(math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam)))
    dec = math.degrees(math.asin(math.sin(eps) * math.sin(lam)))
    gmst = 280.46061837 + 360.98564736629 * n
    return dec, (ra - gmst + 180) % 360 - 180


def footprint_radius(alt_km):
    """angular radius (deg) of the area that sees the satellite above MIN_EL"""
    e = math.radians(MIN_EL)
    return math.degrees(math.acos(RE / (RE + alt_km) * math.cos(e)) - e)


def circle_points(lat, lon, radius_deg, n=72):
    """points around a small circle; longitudes are kept continuous (not wrapped)"""
    p, l, d = math.radians(lat), math.radians(lon), math.radians(radius_deg)
    out = []
    for k in range(n + 1):
        b = 2 * math.pi * k / n
        lat2 = math.asin(math.sin(p) * math.cos(d) + math.cos(p) * math.sin(d) * math.cos(b))
        lon2 = l + math.atan2(math.sin(b) * math.sin(d) * math.cos(p), math.cos(d) - math.sin(p) * math.sin(lat2))
        out.append((math.degrees(lat2), math.degrees(lon2)))
    return out


def fmt_ids(i, empty='-'):
    """a satellite's IDs as its packets show them: Iridium network IDs (decimal), Orbcomm satellite IDs (hex)"""
    f = '%02X' if i.get('system') == 'orbcomm' else '%03d'
    return ', '.join(f % x for x in sorted(i['net_ids'])) or empty


def fmt_sigma(km):
    """an uncertainty: metres while small (GNSS broadcast orbits are good to metres), km otherwise"""
    return '±%.0f m' % (km * 1000) if km < 1 else '±%.0f km' % km


NO_SYSTEM = 'none'      # the Systems filter's "None": no satellites shown (where the app starts)
# the Systems filter: categories, and the systems you can turn on and off under each
CATEGORIES = [('comms', 'Communications', 'Comms', ('iridium', 'orbcomm')),
              ('gnss', 'Navigation (GNSS)', 'Navigation', ('gps', 'galileo', 'glonass', 'beidou', 'waas')),
              ('ham', 'ISS and ham satellites', 'ISS / ham', ('ham',))]
SUB_LABELS = {'iridium': 'Iridium', 'orbcomm': 'Orbcomm', 'gps': 'GPS', 'galileo': 'Galileo', 'glonass': 'GLONASS',
              'beidou': 'BeiDou', 'waas': 'WAAS', 'ham': 'ISS & ham'}
SUB_CATEGORY = {sub: key for key, _, _, subs in CATEGORIES for sub in subs}
GNSS_SUBS = {'GPS': 'gps', 'Galileo': 'galileo', 'GLONASS': 'glonass', 'BeiDou': 'beidou', 'WAAS': 'waas'}


def sub_of(name):
    """the system a satellite belongs to, finely: iridium, orbcomm, ham, gps, galileo, glonass or waas"""
    key = systems.of_name(name)['key']
    if key == 'gnss':
        return GNSS_SUBS.get(name.split()[0], 'gps')
    return key
COL_TRACKS, COL_AT = 3, 5     # satellite list columns: the numeric one (sorts as a number), At this time
# the three middle columns of the satellite list, per Systems filter (None = all systems)
LIST_COLUMNS = {
    None: (('System', 'which system it belongs to'), ('ID', 'its own ID: Iridium slot or network ID, Orbcomm '
           'satellite ID, ham callsign, GNSS block'), ('Received', 'position fixes (Iridium, Orbcomm), frames (ham), '
           'seconds tracked (GNSS)')),
    'iridium': (('Slot', 'orbital plane and slot (P = plane, S = slot)'), ('Net ID', 'network ID in its ring alerts'),
                ('Tracks', 'ring-alert position fixes received')),
    'orbcomm': (('Sat ID', 'the satellite ID in its packets'), ('Passes', 'passes heard (Doppler curves)'),
                ('Fixes', 'position fixes from its ephemeris packets')),
    'ham': (('Callsign', 'callsigns in its frames'), ('Passes', 'passes heard (Doppler curves)'),
            ('Frames', 'frames decoded')),
    'gnss': (('Block', 'satellite generation (GPS IIR / IIR-M / IIF / III, Galileo IOV / FOC), from the public '
                       'catalog name'), ('Orbit', 'its broadcast orbit (ephemeris): ✓ decoded, – not yet'),
             ('Signal', 'best signal, dB-Hz (C/N0) - about 35 or more is needed to decode its orbit')),
}


class NumItem(QtWidgets.QTableWidgetItem):
    """a list cell that shows text but sorts by a number"""
    def __init__(self, text, num):
        super().__init__(text)
        self.num = num

    def __lt__(self, other):
        return self.num < getattr(other, 'num', 0)


def gnss_block(cat_name):
    """'GPS BIIF-6  (PRN 06)' -> 'IIF', 'GSAT0210 (GALILEO 14)' -> 'FOC'"""
    if not cat_name:
        return '-'
    if cat_name.startswith('GPS '):
        code = cat_name.split()[1].split('-')[0]
        return {'BIIR': 'IIR', 'BIIRM': 'IIR-M', 'BIIF': 'IIF', 'BIII': 'III'}.get(code, code)
    if cat_name.startswith('GSAT01'):
        return 'IOV'
    if cat_name.startswith('GSAT02'):
        return 'FOC'
    if cat_name.startswith('COSMOS') and '(' in cat_name:     # GLONASS: 7xx = GLONASS-M, 8xx = GLONASS-K
        num = cat_name[cat_name.rindex('(') + 1:].rstrip(')')
        return {'7': 'M', '8': 'K'}.get(num[:1], '-')
    return 'GEO'


def fmt_time(t, seconds=False):
    return datetime.datetime.fromtimestamp(t).strftime('%b %d  %I:%M:%S %p' if seconds else '%b %d %I:%M %p').replace(' 0', ' ')


def short_time(t):
    """'5:10 PM' today, 'Nov 1' before"""
    d = datetime.datetime.fromtimestamp(t)
    return fmt_clock(t)[:-6] + fmt_clock(t)[-3:] if d.date() == datetime.date.today() else d.strftime('%b %d').replace(' 0', ' ')


def fmt_age(s):
    return '%d s' % s if s < 120 else '%d min' % (s // 60) if s < 7200 else '%.1f h' % (s / 3600)


def fmt_clock(t):
    return datetime.datetime.fromtimestamp(t).strftime('%I:%M:%S %p').lstrip('0')


# ---------------------------------------------------------------- data
class Data:
    """catalog-data.json plus whatever the receiver is decoding right now"""

    def __init__(self):
        self.cat_mtime = None
        self._tles = {}         # system -> public catalog satellites (names only)
        self._gnss_cat = {}     # GNSS satellite -> its public catalog name (info window only)
        self.oc_ids = {}        # Orbcomm satellite ID -> name
        self._ham_names = None  # NORAD number -> catalog name, for ham satellites
        self._ham_fits = {}     # ham satellite -> (data size, time) of its last Doppler-only fit
        self.gnss_eph = {}      # GNSS satellite id (G05...) -> (time, its newest broadcast ephemeris fields)
        self.gnss_fixes = []    # GNSS position fixes of the receiver itself
        self.live_read = {}     # live file -> how far we've read it
        self.live_cap = None    # the newest capture (the one the receiver writes while running)
        self.live_fixes, self.live_heard, self.live_events = [], {}, []
        self.live_signal = {}       # satellite -> deque of (t, signal) heard live: the last SIGNAL_KEEP_S
        self.frame_queue = []   # (kind, line) of new frames in the newest capture, for the activity lights
        self.unlinked = {}      # (capture, (plane, slot)) -> [(t, Hz)] of time & location frames not yet tied to a satellite
        self.live_links = {}    # (capture, (plane, slot)) -> name, matched while receiving
        self.link_tried = {}    # (capture, (plane, slot)) -> (time tried, frames then)
        self.links_version = 0  # goes up when a plane/slot gets linked (the satellite list needs updating)
        self.live_doppler = {}  # (capture, name) -> Doppler curve [(t, Hz), ...]
        self.heartbeat = 0      # last time the newest capture's files changed (its decoder log: every 10 s)
        self.last_burst = 0     # last time that capture received a burst
        self.load_catalog()

    def identify(self, net_id, t, lat, lon):
        """satellite name for a new live fix. Names are static reference labels: first from the
        network IDs already linked in the catalog, otherwise by asking the public catalog which
        satellite that was (the name only - its orbit is never used for display)."""
        for n, i in self.info.items():
            if i['system'] == 'iridium' and net_id in i['net_ids']:
                return n
        hit = catalog.match_position(self.tles(), datetime.datetime.fromtimestamp(t, UTC), lat, lon)
        return hit[0] if hit and hit[1] > 85 else None

    def tles(self, system='iridium'):
        """the public satellite list of a system - for names only"""
        if system not in self._tles:
            try:
                self._tles[system] = catalog.load_tles(system)
            except OSError:
                self._tles[system] = []
        return self._tles[system]

    SIGNAL_KEEP_S = 600

    def heard(self, name, t, value=None):
        """a satellite heard at t, with its signal (SNR dB, or C/N0 dB-Hz for GNSS)"""
        self.live_heard[name] = max(t, self.live_heard.get(name, 0))
        if value:
            dq = self.live_signal.setdefault(name, collections.deque())
            dq.append((t, value))
            while dq and dq[0][0] < t - self.SIGNAL_KEEP_S:
                dq.popleft()

    def signal_now(self, name):
        """(signal, when) - the middle of its receptions in the 5 s before it was last heard; or None"""
        dq = self.live_signal.get(name)
        if not dq:
            return None
        t_last = dq[-1][0]
        vals = [v for t, v in dq if t >= t_last - 5]
        return float(np.median(vals)), t_last

    def gnss_catalog_name(self, n):
        """the public catalog's name of a GNSS satellite ('GPS G06' -> 'GPS BIIF-6  (PRN 06)'), for the info
        window and details only: GPS by its PRN, Galileo (whose catalog names carry no PRN) by where its own
        broadcast orbit puts it"""
        if n in self._gnss_cat:
            return self._gnss_cat[n]
        sat = self.info.get(n, {}).get('sat') or n.split()[-1]
        name = catalog.gnss_catalog_names().get(sat)
        o = self.orbits.get(n)
        if name is None and o is not None:
            t = datetime.datetime.fromtimestamp(o.t0, datetime.timezone.utc)
            waas = sat.startswith('S')
            want = (lambda s: s.name.startswith('COSMOS')) if sat.startswith('R') else \
                (lambda s: s.name.startswith('GSAT')) if not waas else \
                (lambda s: '(PRN' not in s.name and not s.name.startswith(('GSAT', 'BEIDOU', 'COSMOS')))
            cands = [s for s in self.tles('gnss') if want(s)]
            name = catalog.match_ecef(cands, t, o.ecef(o.t0)[0], max_km=500.0 if waas else 300.0)[0]
        if name is not None or o is not None:       # Galileo: try again once its orbit is known
            self._gnss_cat[n] = name
        return name

    def identify_orbcomm(self, sat_id, t, xyz_km):
        """name of the Orbcomm satellite that sent its position: the satellite ID's name once known,
        otherwise the catalog satellite at that spot (the name only)"""
        if sat_id in self.oc_ids:
            return self.oc_ids[sat_id]
        name, _ = catalog.match_ecef(self.tles('orbcomm'), datetime.datetime.fromtimestamp(t, UTC), xyz_km)
        if name:
            self.oc_ids[sat_id] = name
        return name

    def link_slots(self, events):
        """while receiving: tie each plane/slot heard in time & location frames to a satellite by its Doppler
        curve - the same match (and confidence rule) catalog.py uses when a capture is finished"""
        now = time.time()
        linked = []
        for key, pts in list(self.unlinked.items()):
            cap, ps = key
            if ps in self.slot_name or key in self.live_links:
                self.unlinked.pop(key)
                continue
            recent = [p for p in pts if p[0] > pts[-1][0] - 900]       # this pass
            tried_at, tried_n = self.link_tried.get(key, (0, 0))
            if len(recent) < 3 or recent[-1][0] - recent[0][0] < 20 or \
                    (now - tried_at < LINK_EVERY_S and len(recent) < tried_n + 5):
                continue
            self.link_tried[key] = (now, len(recent))
            fits = catalog.match_doppler(self.tles(), [(datetime.datetime.fromtimestamp(t, UTC), hz) for t, hz in recent], [0])
            if not fits:
                continue
            rms, name = fits[0]
            second = fits[1][0] if len(fits) > 1 else 1e9
            if rms < 300 and second > 2.5 * rms:                    # catalog.py's "confident"
                self.live_links[key] = name
                self._apply_link(cap, ps, name)
                linked.append(name)
                events.append('%s  plane %d #%d is %s (matched by its Doppler curve, %.0f Hz rms)'
                              % (fmt_clock(now), ps[0], ps[1], name, rms))
        if linked:
            self.links_version += 1
            self.fit_orbits(set(linked))

    def _apply_link(self, cap, ps, name):
        self.slot_name[ps] = name
        i = self._info(name)
        i['slots'].add(ps)
        i['how'].add('Doppler')
        pts = self.unlinked.pop((cap, ps), [])
        if pts:                     # its frames so far become a Doppler curve for the orbit fit
            g = self.live_doppler.setdefault((cap, name), [])
            if not g:
                self.doppler.setdefault(name, []).append(g)
            g.extend(pts)
            i['last'] = max(i['last'], pts[-1][0])

    def fit_orbits(self, names=None):
        """compute orbits from our own fixes and Doppler curves"""
        if names is None:
            self.orbits, self.no_orbit = {}, {}
            self._ham_fits = {}     # every orbit is redone, Doppler-only ones too
            names = set(self.info)
        if catalog.HOME is None:      # first run: nothing to fit until the antenna location is set
            return
        for n in names:
            try:
                self._fit_one(n)
            except Exception as e:      # one satellite's bad fit must never stop the tracker
                self.orbits.pop(n, None)
                self.no_orbit[n] = 'orbit fit failed (%s)' % type(e).__name__
        # a single fix can't tell whether the satellite was heading north or south; once the other
        # orbits are in, keep the direction that lines up with one of the planes we have measured
        for n in names:
            if 'direction' in self.no_orbit.get(n, ''):
                self._fit_one(n, lambda t, n=n: [p['node'] for p in self.planes(t) if n not in p['members']])

    def _fit_one(self, n, plane_nodes_at=None):
        sys_ = systems.of_name(n)
        fixes = [(f['t'], f['xyz_km']) for f in self.all_fixes() if f['name'] == n and f.get('xyz_km')]
        groups = list(self.doppler.get(n, []))
        by_cap = {}
        for f in self.all_fixes():   # the ring alerts' own frequencies are Doppler measurements too
            if f['name'] == n and f.get('hz') and sys_ is systems.IRIDIUM:
                by_cap.setdefault(f['cap'], []).append((f['t'], f['hz']))
        groups += list(by_cap.values())
        # only the latest week counts: older observations don't improve today's orbit, and this keeps
        # the fit quick however much history builds up
        if fixes:
            since = max(t for t, _ in fixes) - FIT_DAYS * 86400
            fixes = [fx for fx in fixes if fx[0] >= since]
            groups = [g for g in groups if g and max(t for t, _ in g) >= since]
        if sys_ is systems.GNSS:
            orbit, why = self._gnss_orbit(n)
        elif sys_ is systems.HAM:
            got = self._fit_ham(n, groups)
            if got is None:
                return          # nothing new since the last fit
            orbit, why = got
        else:
            orbit, why = orbitfit.fit(fixes, groups, catalog.HOME, plane_nodes_at,
                                      carrier=sys_['carrier_hz'], inc_hint=sys_['inc_hint'])
        self.orbits.pop(n, None)
        self.no_orbit.pop(n, None)
        if orbit and sys_ is systems.IRIDIUM:
            # the +- the map shows, calibrated against the public orbits (Nov 2026, 65 satellites, up to 3 days
            # unheard): with these 2/3 of the real errors fall inside it and 98% inside three times it
            orbit.floor_km, orbit.drift_km_h = IRIDIUM_FLOOR_KM, IRIDIUM_DRIFT_KM_H
        if orbit:
            self.orbits[n] = orbit
        else:
            self.no_orbit[n] = why

    def _gnss_orbit(self, n):
        """GNSS satellites broadcast their own orbit (ephemeris): its position comes from that, like Orbcomm's
        reported positions - only once it has been decoded (needs ~30 s of continuous tracking)"""
        sat = self.info.get(n, {}).get('sat')
        got = self.gnss_eph.get(sat)
        if not got:
            return None, 'its broadcast orbit (ephemeris) has not been decoded yet - needs about 30 s of steady tracking'
        try:
            import gnss
            t, fields = got
            if fields.startswith('SBAS'):       # a WAAS satellite: its own position from its message type 9
                return gnss.SBASOrbit.from_fields(fields, sat, t), None
            if fields.startswith('GLO'):        # GLONASS: its own position / velocity, integrated
                return gnss.GlonassOrbit.from_fields(fields, sat, t), None
            return gnss.EphemerisOrbit(gnss.parse_eph('$EPH,%.3f,%s,%s' % (t, sat, fields.replace(' ', ',')))), None
        except Exception as e:      # (gnss.py missing, or an ephemeris it can't read)
            return None, 'could not use its ephemeris (%s)' % e

    def _fit_ham(self, n, groups):
        """ISS / ham satellites send no position: an orbit from their Doppler curves alone, kept only if
        orbitfit's confidence gate accepts it (it needs passes on more than one orbit). A fit takes ~2 s, so
        it is redone only when there is new data worth it. Returns (orbit or None, reason), or None if skipped.
        Passes heard on 2 m and 70 cm are scaled to one reference frequency so they fit together."""
        groups = [g for g in groups if len(g) >= 3]
        key = (len(groups), sum(len(g) for g in groups))
        last = self._ham_fits.get(n)
        if last and (key == last[0] or (key[0] == last[0][0] and key[1] - last[0][1] < 5 and time.time() - last[1] < 30)):
            return None
        ref = 436.5e6
        scaled = []
        for g in groups:
            f0 = float(np.median([h for _, h in g]))
            scaled.append([(t, ref + (h - f0) * ref / f0) for t, h in g])
        orbit, why, _ = orbitfit.fit_doppler(scaled, catalog.HOME, ref)
        self._ham_fits[n] = (key, time.time())
        return orbit, why

    def load_catalog(self):
        path = catalog.DATA
        try:
            self.cat_mtime = os.path.getmtime(path)
            caps = json.load(open(path))
        except (OSError, ValueError):
            caps = {}
        self.captures, self.fixes = [], []
        self.gnss_eph, self.gnss_fixes = {}, []
        self.doppler = {}       # name -> list of Doppler curves [(t, Hz), ...]
        self.info = {}          # name -> details
        self.slot_name = {}     # (plane, slot) -> name
        self.slots_heard = set()
        ts = lambda iso: datetime.datetime.fromisoformat(iso).timestamp()
        for cap_name, c in sorted(caps.items()):
            if not sum(c['counts'].values()):
                continue
            if systems.get(c.get('system')).get('system') == 'gnss':
                for ps in c.get('passes', []):
                    systems.register(ps['name'], 'gnss')
                for sat, (t, fields) in c.get('ephemerides', {}).items():
                    if t >= self.gnss_eph.get(sat, (0, ''))[0]:
                        self.gnss_eph[sat] = (t, fields)
                self.gnss_fixes += c.get('gnss_fixes', [])
            if systems.get(c.get('system')).get('system') == 'ham':
                if self._ham_names is None:
                    self._ham_names = catalog.norad_names()
                for ps in c.get('passes', []):      # named the catalog's way, even if analyzed while offline
                    ps['name'] = catalog.ham_name(ps.get('norad'), ps['name'], self._ham_names)
                    systems.register(ps['name'], 'ham')
            start = ts(c['start_utc'])
            self.captures.append({'name': cap_name, 'start': start, 'end': start + c['hours'] * 3600,
                                  'fixes': len(c['positions']), 'system': c.get('system', 'iridium')})
            for p in c['positions']:
                fx = dict(p, t=ts(p['time']), cap=cap_name, live=False)
                self.fixes.append(fx)
                if p['name']:
                    i = self._info(p['name'])
                    i['fixes'] += 1
                    i['net_ids'].add(p['net_id'])
                    i['last'] = max(i['last'], fx['t'])
                    i['best'] = max(i['best'], p['snr'])
                    i['how'].add('position')
                    if c.get('system') == 'orbcomm':
                        self.oc_ids[p['net_id']] = p['name']
            for ps in c.get('passes', []):       # Orbcomm / ham: each satellite's packets over a pass
                i = self._info(ps['name'])
                i['last'] = max(i['last'], ts(ps['last']))
                i['best'] = max(i['best'], ps['best_snr'])
                if ps.get('sat_id') is not None:
                    i['net_ids'].add(ps['sat_id'])
                if 'sat' in ps:             # GNSS: tracked by its own code
                    i['how'].add('tracked')
                    i['sat'] = ps['sat']
                    i['frames'] = i.get('frames', 0) + ps['frames']
                if 'norad' in ps:           # ham: identified by its decoded frames
                    i['how'].add('frames')
                    i['norad'] = ps['norad']
                    i['callsigns'] = sorted(set(i.get('callsigns', [])) | set(ps.get('callsigns', [])))
                    i['frames'] = i.get('frames', 0) + ps['frames']
                self.doppler.setdefault(ps['name'], []).append(ps['doppler'])
            for s in c['slots']:
                self.slots_heard.add((s['plane'], s['slot']))
                if s['name'] and s['confidence'] == 'confident':
                    i = self._info(s['name'])
                    i['slots'].add((s['plane'], s['slot']))
                    i['last'] = max(i['last'], ts(s['last']))
                    i['best'] = max(i['best'], s['best_snr'])
                    i['how'].add('Doppler')
                    self.slot_name[(s['plane'], s['slot'])] = s['name']
                    if s.get('doppler'):
                        self.doppler.setdefault(s['name'], []).append(s['doppler'])
        # live data from captures that have now been decoded into the catalog isn't needed twice
        done = {c['name'] for c in self.captures}
        self.live_fixes = [f for f in self.live_fixes if f['cap'] not in done]
        self.live_doppler = {k: g for k, g in self.live_doppler.items() if k[0] not in done}
        for fx in self.live_fixes:
            self._add_live_fix_info(fx)
        for (_, n), g in self.live_doppler.items():
            self.doppler.setdefault(n, []).append(g)
        self.live_links = {k: n for k, n in self.live_links.items() if k[0] not in done}
        for (cap, ps), n in self.live_links.items():     # slots matched while receiving (not in the catalog yet)
            if ps not in self.slot_name:
                self.slot_name[ps] = n
                self._info(n)['slots'].add(ps)
        self.fit_orbits()

    def _info(self, name):
        if name not in self.info:
            self.info[name] = {'fixes': 0, 'net_ids': set(), 'slots': set(), 'last': 0, 'best': 0, 'how': set(),
                               'system': systems.of_name(name)['key']}
        return self.info[name]

    def _add_live_fix_info(self, fx):
        if fx['name']:
            i = self._info(fx['name'])
            i['fixes'] += 1
            i['net_ids'].add(fx['net_id'])
            i['last'] = max(i['last'], fx['t'])
            i['best'] = max(i['best'], fx['snr'])
            i['how'].add('position')

    @property
    def tracked(self):
        """satellites we have our own orbit for"""
        return [n for n in self.info if n in self.orbits]

    @property
    def doppler_only(self):
        """satellites identified by their Doppler curve (Iridium) or their decoded frames (ISS, ham) that we
        have no orbit for (listed, not drawn)"""
        return [n for n, i in self.info.items() if n not in self.orbits and ({'Doppler', 'frames', 'tracked'} & i['how'])]

    def all_fixes(self):
        return self.fixes + self.live_fixes

    def planes(self, t):
        """where each orbital plane is at time t, from our own orbits. A satellite joins the plane
        its ITL plane/slot number names; one without a slot number joins whichever plane its own
        orbit lines up with. Returns a list of {plane, node, inc, members, spread, sigma}."""
        sats = []
        for n, o in self.orbits.items():
            if systems.of_name(n) is not systems.IRIDIUM:
                continue
            if o.plane_sigma_km > PLANE_MAX_SIGMA_KM:
                continue
            node, inc = o.plane_at(t)
            nums = {ps[0] for ps in self.info[n]['slots']}
            sats.append({'name': n, 'node': node, 'inc': inc, 'w': 1 / o.plane_sigma_km ** 2,
                         'plane': nums.pop() if len(nums) == 1 else None})
        groups = {}
        for s in sats:
            if s['plane'] is not None:
                groups.setdefault(s['plane'], []).append(s)

        def mean_node(g):
            return math.atan2(sum(s['w'] * math.sin(s['node']) for s in g), sum(s['w'] * math.cos(s['node']) for s in g))

        def gap(a, b):
            return abs((math.degrees(a - b) + 180) % 360 - 180)

        unknown = 0
        for s in (s for s in sats if s['plane'] is None):
            near = min(groups, key=lambda k: gap(mean_node(groups[k]), s['node']), default=None)
            if near is not None and gap(mean_node(groups[near]), s['node']) < PLANE_JOIN_DEG:
                groups[near].append(s)
            else:
                unknown -= 1          # a plane we can't number yet
                groups[unknown] = [s]
        # a plane none of whose satellites has a slot number yet: number it from a numbered plane, since
        # planes 1-6 are PLANE_SPACING_DEG apart in order
        numbered = {k: mean_node(g) for k, g in groups.items() if k > 0}
        for k in [k for k in groups if k < 0]:
            node = mean_node(groups[k])
            best = None
            for ref, ref_node in numbered.items():
                d = (math.degrees(node - ref_node) + 180) % 360 - 180
                n = ref + round(d / PLANE_SPACING_DEG)
                miss = abs(d - (n - ref) * PLANE_SPACING_DEG)
                if 1 <= n <= 6 and miss < 6 and (best is None or miss < best[1]):
                    best = (n, miss)
            if best and best[0] not in groups:
                groups[best[0]] = groups.pop(k)
        out = []
        for k, g in groups.items():
            node = mean_node(g)
            wsum = sum(s['w'] for s in g)
            out.append({'plane': k if k > 0 else None, 'node': node,
                        'inc': sum(s['w'] * s['inc'] for s in g) / wsum,
                        'members': sorted(s['name'] for s in g),
                        'spread': max(gap(node, s['node']) for s in g),
                        'sigma': 1 / math.sqrt(wsum)})
        return sorted(out, key=lambda p: p['plane'] or 99)

    def poll(self):
        """read new catalog data and newly decoded live frames; returns new event texts"""
        events = []
        try:
            if os.path.getmtime(catalog.DATA) != self.cat_mtime:
                self.load_catalog()
                events.append('catalog reloaded')
        except OSError:
            pass
        # is a receiver writing right now? Its decoder log changes every 10 s even when nothing is heard,
        # so the newest of a capture's files is its heartbeat; bursts only touch .live / .bits
        current = [f for p in systems.capture_prefixes() for f in glob.glob(os.path.join(paths.CAPTURES, p + '*'))
                   if f.endswith(('.live', '.log', '.bits'))]
        if current:
            newest = max(current, key=os.path.getmtime)
            self.live_cap = os.path.basename(newest).rsplit('.', 1)[0]
            self.heartbeat = os.path.getmtime(newest)
            stem = os.path.join(paths.CAPTURES, self.live_cap)
            self.last_burst = max((os.path.getmtime(stem + e) for e in ('.live', '.bits')
                                   if os.path.exists(stem + e) and os.path.getsize(stem + e) > 200), default=0)
        # every capture the receiver wrote that hasn't been decoded into the catalog yet
        lives = sorted((f for p in systems.capture_prefixes() for f in glob.glob(os.path.join(paths.CAPTURES, p + '*.live'))),
                       key=os.path.getmtime)
        if not lives:
            return events
        done = {c['name'] for c in self.captures}
        refit = set()
        for path in lives:
            cap = os.path.basename(path)[:-5]
            if cap in done:
                continue
            try:
                with open(path, errors='replace') as f:
                    f.seek(self.live_read.get(path, 0))
                    text = f.read()
                    self.live_read[path] = f.tell()
            except OSError:
                continue
            if not text:
                continue
            newest = path == lives[-1]
            n_before = len(self.live_fixes)
            if systems.of_capture(cap) is systems.ORBCOMM:
                evs = self._read_live_orbcomm(text, cap, newest, refit)
            elif systems.of_capture(cap).get('system') == 'ham':
                evs = self._read_live_ham(text, cap, newest, refit)
            elif systems.of_capture(cap).get('system') == 'gnss':
                evs = self._read_live_gnss(text, cap, newest, refit)
            elif systems.of_capture(cap).get('system') in ('meteor', 'radio'):
                evs = []        # Meteor: only its signal strength while receiving; pictures in Images after Stop
            else:
                evs = self._read_live(text, cap, catalog.capture_start(path[:-5])[0].timestamp(), newest, refit)
            if newest:
                events += evs
            elif len(self.live_fixes) > n_before:
                events.append('%d position fixes from %s (not decoded yet - run 2-decode.bat to keep them)'
                              % (len(self.live_fixes) - n_before, cap))
        if refit:
            self.fit_orbits(refit)
        self.link_slots(events)
        return events

    def _read_live(self, text, cap, start, newest, refit):
        """parse frames from a capture's live file; adds names needing a new orbit to refit"""
        events = []
        for line in text.splitlines():
            f = line.split()
            if len(f) < 6 or not f[0].endswith(':'):
                continue
            try:
                kind, t, hz = f[0][:-1], start + float(f[2]) / 1000, int(f[3])
                snr = float(f[5].split('|')[2]) if '|' in f[5] else 0.0
            except ValueError:
                continue
            if newest:
                self.frame_queue.append((kind, line))
            m = re.search(r'sat:(\d+) beam:(\d+).*?pos=\(([-+\d.]+)/([-+\d.]+)\) alt=(-?\d+)', line)
            x = re.search(r'xyz=\(([-+\d]+),([-+\d]+),([-+\d]+)\)', line)
            if kind == 'IRA' and m and x and int(m.group(5)) > 100:
                lat, lon = float(m.group(3)), float(m.group(4))
                name = self.identify(int(m.group(1)), t, lat, lon)
                fx = {'t': t, 'lat': lat, 'lon': lon, 'name': name, 'net_id': int(m.group(1)), 'beam': int(m.group(2)),
                      'alt_km': int(m.group(5)), 'snr': snr, 'cap': cap, 'live': newest, 'hz': hz,
                      'xyz_km': [4 * int(v) for v in x.groups()]}
                self.live_fixes.append(fx)
                self._add_live_fix_info(fx)
                if name:
                    refit.add(name)
                    self.heard(name, t, snr)
                events.append('%s  POSITION  %s  %+.2f, %+.2f  (%.0f dB)' % (fmt_clock(t), name or 'net ID %03d' % fx['net_id'], lat, lon, snr))
            pm = re.search(r' P(\d) S(\d+) ', line)
            if kind == 'ITL' and pm:
                ps = (int(pm.group(1)), int(pm.group(2)))
                name = self.slot_name.get(ps)
                if name:
                    self.heard(name, t, snr)
                    self.info[name]['last'] = max(self.info[name]['last'], t)
                    if not self.live_doppler.get((cap, name)):
                        self.live_doppler[(cap, name)] = []
                        self.doppler.setdefault(name, []).append(self.live_doppler[(cap, name)])
                    self.live_doppler[(cap, name)].append((t, hz))
                    refit.add(name)
                else:                   # not tied to a satellite yet: kept for link_slots
                    self.unlinked.setdefault((cap, ps), []).append((t, hz))
                events.append('%s  heard plane %d #%d%s  (%.0f dB)' % (fmt_clock(t), ps[0], ps[1],
                                                                     '  = ' + name if name else '', snr))
        return events

    def _read_live_orbcomm(self, text, cap, newest, refit):
        """Orbcomm packets from a capture's live file: positions (ephemeris) and the Doppler of every packet"""
        events = []
        for line in text.splitlines():
            pk = catalog.read_orbcomm([line])
            if not pk:
                continue
            kind, t, hz, sid, snr, rest = pk[0]
            if newest:
                self.frame_queue.append((kind, line))
            if kind == 'OBAD' or sid is None:
                continue
            if kind == 'OEP' and len(rest) >= 6:
                xyz = [float(v) for v in rest[:3]]
                name = self.identify_orbcomm(sid, t, xyz)
                lat, lon, alt = catalog.ecef_to_lla(*xyz)
                fx = {'t': t, 'lat': lat, 'lon': lon, 'name': name, 'net_id': sid, 'beam': None, 'alt_km': round(alt),
                      'snr': snr, 'cap': cap, 'live': newest, 'hz': hz, 'xyz_km': xyz,
                      'vel_kms': [float(v) for v in rest[3:6]]}
                self.live_fixes.append(fx)
                self._add_live_fix_info(fx)
                if name:
                    refit.add(name)
                events.append('%s  POSITION  %s  %+.2f, %+.2f  (%.0f dB)' % (fmt_clock(t), name or 'Orbcomm ID %02X' % sid,
                                                                            lat, lon, snr))
            name = self.oc_ids.get(sid)
            if name:                    # every packet's frequency is a point on its Doppler curve
                self.heard(name, t, snr)
                self._info(name)['last'] = max(self._info(name)['last'], t)
                if not self.live_doppler.get((cap, name)):
                    self.live_doppler[(cap, name)] = []
                    self.doppler.setdefault(name, []).append(self.live_doppler[(cap, name)])
                self.live_doppler[(cap, name)].append((t, hz))
        return events

    def _read_live_ham(self, text, cap, newest, refit):
        """ISS / ham frames from a capture's live file: each identified frame is a point on its satellite's
        Doppler curve; frames that can't be tied to one satellite are only shown in the log"""
        events = []
        if self._ham_names is None:
            self._ham_names = catalog.norad_names()
        for line in text.splitlines():
            fr = catalog.read_ham([line])
            if not fr:
                continue
            kind, t, hz, norad, name, snr, call, cands = fr[0]
            if newest:
                self.frame_queue.append((kind, line))
            if kind != 'HFR':
                continue
            if not name:
                events.append('%s  frame from %s on %.4f MHz - could be %s (they share a frequency)'
                              % (fmt_clock(t), call, hz / 1e6, ', '.join(cands[:4]) + (' ...' if len(cands) > 4 else '')))
                continue
            name = catalog.ham_name(norad, name, self._ham_names)
            systems.register(name, 'ham')
            i = self._info(name)
            i['how'].add('frames')
            i['norad'] = norad
            i['frames'] = i.get('frames', 0) + 1
            if call != '-':
                i['callsigns'] = sorted(set(i.get('callsigns', [])) | {call})
            i['last'] = max(i['last'], t)
            i['best'] = max(i['best'], snr)
            self.heard(name, t, snr)
            if not self.live_doppler.get((cap, name)):
                self.live_doppler[(cap, name)] = []
                self.doppler.setdefault(name, []).append(self.live_doppler[(cap, name)])
            self.live_doppler[(cap, name)].append((t, hz))
            refit.add(name)
            events.append('%s  FRAME  %s%s  %.4f MHz  (%.0f dB)' % (fmt_clock(t), name, ' from ' + call if call != '-' else '',
                                                                    hz / 1e6, snr))
        return events


    def _read_live_gnss(self, text, cap, newest, refit):
        """GNSS lines from a capture's live file: tracked satellites (Doppler, signal), their broadcast orbits,
        and position fixes of the receiver"""
        events = []
        for kind, t, f in catalog.read_gnss(text.splitlines()):
            if newest:
                self.frame_queue.append((kind, '%s: %.3f %s' % (kind, t, ' '.join(f))))
            if kind == 'GCH' and len(f) >= 4:
                sat = f[0]
                name = catalog.gnss_name(sat)
                systems.register(name, 'gnss')
                i = self._info(name)
                new = 'tracked' not in i['how']
                i['how'].add('tracked')
                i['sat'] = sat
                i['frames'] = i.get('frames', 0) + 1
                i['last'] = max(i['last'], t)
                i['best'] = max(i['best'], float(f[2]))
                self.heard(name, t, float(f[2]))
                if not self.live_doppler.get((cap, name)):
                    self.live_doppler[(cap, name)] = []
                    self.doppler.setdefault(name, []).append(self.live_doppler[(cap, name)])
                self.live_doppler[(cap, name)].append((t, catalog.GNSS_L1 + float(f[3])))
                # its latest live signal (the details panel shows it while it is being tracked)
                now = {'t': t, 'cn0': float(f[2]), 'dop': float(f[3]), 'lock': float(f[4]) if len(f) > 4 else 0.0}
                if len(f) > 8:
                    now.update(sync=f[5], nav=int(f[6]), err=int(f[7]), lol=int(f[8]))
                i['now'] = now
                if new:
                    events.append('%s  TRACKING  %s  (%.0f dB-Hz, Doppler %+.0f Hz)' % (fmt_clock(t), name, float(f[2]), float(f[3])))
            elif kind == 'GEPH' and len(f) >= 2:
                self.gnss_eph[f[0]] = (t, ' '.join(f[1:]))
                refit.add(catalog.gnss_name(f[0]))
                events.append('%s  ORBIT  %s broadcast its ephemeris' % (fmt_clock(t), catalog.gnss_name(f[0])))
            elif kind == 'GPOS' and len(f) >= 4:
                fx = {'t': t, 'lat': float(f[0]), 'lon': float(f[1]), 'height_m': float(f[2]), 'nsat': int(f[3])}
                self.gnss_fixes.append(fx)
                events.append('%s  GNSS FIX  %+.5f, %+.5f, %.0f m (%d satellites)' % (fmt_clock(t), fx['lat'], fx['lon'],
                                                                                  fx['height_m'], fx['nsat']))
        return events


# ---------------------------------------------------------------- map tiles
# invert, then turn the hues back around (like CSS "invert(1) hue-rotate(180deg)"): a dark map that keeps
# water blue and parks green; the last factor dims it a little
_DARK_MAP = np.array([[-0.574, 1.430, 0.144],
                      [0.426, 0.430, 0.144],
                      [0.426, 1.430, -0.856]]) * 0.82


def style_tile(data, map_theme='dark'):
    """OpenStreetMap tile bytes -> QImage in the map's theme: as OpenStreetMap draws it (light), or
    darkened (safe to call off the GUI thread)"""
    img = QtGui.QImage.fromData(data)
    if img.isNull():
        return None
    img = img.convertToFormat(QtGui.QImage.Format_RGB32)
    if map_theme == 'light':
        return img
    w, h = img.width(), img.height()
    ptr = img.constBits()
    ptr.setsize(img.byteCount())
    bgra = np.frombuffer(ptr, np.uint8).reshape(h, img.bytesPerLine() // 4, 4)[:, :w]
    rgb = 255.0 - bgra[..., 2::-1].astype(np.float32)
    out = np.empty((h, w, 4), np.uint8)
    out[..., 2::-1] = np.clip(rgb @ _DARK_MAP.T + 8, 0, 255).astype(np.uint8)
    out[..., 3] = 255
    return QtGui.QImage(out.tobytes(), w, h, 4 * w, QtGui.QImage.Format_RGB32).copy()


def load_tile_rgb(z, x, y, map_theme='dark'):
    """one map tile in the map's theme as an RGB array (from the cache, downloading it if needed) - for the globe"""
    path = os.path.join(TILE_DIR, str(z), str(x), '%d.png' % y)
    try:
        if os.path.exists(path):
            data = open(path, 'rb').read()
        else:
            r = requests.get(TILE_URL.format(z=z, x=x, y=y), timeout=15, headers=TILE_HEADERS)
            r.raise_for_status()
            data = r.content
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, 'wb').write(data)
    except Exception:
        return None
    img = style_tile(data, map_theme)
    if img is None:
        return None
    img = img.convertToFormat(QtGui.QImage.Format_RGB32)
    ptr = img.constBits()
    ptr.setsize(img.byteCount())
    bgra = np.frombuffer(ptr, np.uint8).reshape(img.height(), img.bytesPerLine() // 4, 4)[:, :img.width()]
    return bgra[..., 2::-1].copy()


class TileLoader(QtCore.QObject):
    loaded = QtCore.pyqtSignal(tuple, object)

    def __init__(self):
        super().__init__()
        self.pool = concurrent.futures.ThreadPoolExecutor(4)
        self.pending, self.failed = set(), {}
        self.session = requests.Session()
        self.session.headers.update(TILE_HEADERS)

    def request(self, key):
        if key in self.pending or time.time() - self.failed.get(key, 0) < 60:
            return
        self.pending.add(key)
        self.pool.submit(self._fetch, key)

    def _fetch(self, key):
        """key: (z, x, y, map theme)"""
        z, x, y, map_theme = key
        path = os.path.join(TILE_DIR, str(z), str(x), '%d.png' % y)
        try:
            if os.path.exists(path):
                data = open(path, 'rb').read()
                os.utime(path)      # recently used: the cache limit removes the oldest-used pictures first
            else:
                r = self.session.get(TILE_URL.format(z=z, x=x, y=y), timeout=15)
                r.raise_for_status()
                data = r.content
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, 'wb').write(data)
            img = style_tile(data, map_theme)
            if img is not None:
                self.loaded.emit(key, img)
        except Exception:
            self.failed[key] = time.time()
        finally:
            self.pending.discard(key)


def merc(lon, lat):
    """lon/lat -> world coordinates 0..1 (x continues past 1 for unwrapped longitudes)"""
    lat = max(-85.0, min(85.0, lat))
    s = math.sin(math.radians(lat))
    return (lon + 180) / 360, 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)


def unmerc(wx, wy):
    lon = wx * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * wy))))
    return lon, lat


C_SELECT = QtGui.QColor(56, 217, 255)       # the selected satellite's corner brackets (map, globe, key)
# the ring round a satellite your receiver is hearing: its colour is the signal's strength (red weak -
# yellow - green strong, on its system's own scale, systems.py 'signal'), and it fades as the satellite goes
# quiet, gone after HEARD_S
HEARD_S = 60.0
SIGNAL_COLOURS = [(0.0, (235, 70, 60)), (0.5, (245, 200, 40)), (1.0, (30, 210, 80))]


def signal_quality(name, value):
    """a signal on its system's scale: 0 = barely decodes, 1 = strong"""
    lo, hi, _ = systems.of_name(name).get('signal', (10.0, 30.0, 'dB'))
    return min(1.0, max(0.0, (value - lo) / (hi - lo)))


def strength_colour(q, alpha=255):
    """red (weak) - yellow - green (strong) for a quality 0..1"""
    for (q0, c0), (q1, c1) in zip(SIGNAL_COLOURS, SIGNAL_COLOURS[1:]):
        if q <= q1:
            k = (q - q0) / (q1 - q0)
            return QtGui.QColor(*(int(a + (b - a) * k) for a, b in zip(c0, c1)), alpha)
    return QtGui.QColor(*SIGNAL_COLOURS[-1][1], alpha)


def heard_colour(age, q=None):
    """the ring for a satellite heard `age` s ago with signal quality q: its strength colour, fading from
    4 s (the receiver's lines are read every 2 s) to faint at HEARD_S; None after that (or never heard)"""
    if age is None or age > HEARD_S:
        return None
    alpha = 255 if age <= 4 else int(255 - (age - 4) / (HEARD_S - 4) * 190)
    return strength_colour(0.5 if q is None else q, alpha)


def draw_selection(p, x, y, r=17.0):
    """corner brackets around the selected satellite (its body keeps its own colour: up / down)"""
    p.save()
    p.setPen(QtGui.QPen(C_SELECT, 2.2, Qt.SolidLine, Qt.RoundCap))
    p.setBrush(Qt.NoBrush)
    a = r * 0.45
    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        cx, cy = x + sx * r, y + sy * r
        p.drawLine(QPointF(cx, cy), QPointF(cx - sx * a, cy))
        p.drawLine(QPointF(cx, cy), QPointF(cx, cy - sy * a))
    p.restore()


def draw_satellite(p, x, y, color, scale=1.0, system='iridium'):
    """a satellite icon; the body colour shows its state, the solar panels' edge its system"""
    p.save()
    p.translate(x, y)
    p.scale(scale, scale)
    p.setPen(QtGui.QPen(Qt.white if system == 'iridium' else QtGui.QColor(*systems.get(system)['colour']),
                        1 if system == 'iridium' else 1.6))
    p.setBrush(QtGui.QColor(40, 40, 40))
    p.drawRect(QtCore.QRectF(-11, -3, 7, 6))     # solar panels
    p.drawRect(QtCore.QRectF(4, -3, 7, 6))
    p.setPen(QtGui.QPen(QtGui.QColor(120, 120, 120), 0.8))
    for k in (-9, -7, -5, 6, 8, 10):
        p.drawLine(QPointF(k, -3), QPointF(k, 3))
    p.setPen(QtGui.QPen(Qt.white, 1))
    p.setBrush(color)
    p.drawRect(QtCore.QRectF(-3.5, -6, 7, 12))   # body
    p.drawLine(QPointF(-4, 0), QPointF(-11, 0))
    p.restore()


class MapView(QtWidgets.QWidget):
    satelliteClicked = QtCore.pyqtSignal(str)
    locationPicked = QtCore.pyqtSignal(float, float)   # lat, lon clicked while picking

    def __init__(self):
        super().__init__()
        self.setMinimumSize(400, 240)
        self.setMouseTracking(True)
        self.zoom = 2.3
        self.cx, self.cy = merc(-100, 25)
        self.tiles = {}             # (z, x, y, map theme) -> picture
        self.map_theme = 'dark'
        self.loader = TileLoader()
        self.loader.loaded.connect(self._tile_loaded)
        self.scene = None
        self.hits = []
        self.drag = None
        self.picking = False
        self.legend_open = bool(theme.load_ui().get('key_open', False))   # the map key: a chip, or open
        self.legend_rect = QtCore.QRectF()

    def set_picking(self, on):
        """pick mode: the next click on the map (not a drag) sets the antenna location"""
        self.picking = on
        self.setCursor(Qt.CrossCursor if on else Qt.ArrowCursor)
        self.update()

    def lonlat_at(self, pos):
        wx = self.cx + (pos.x() - self.width() / 2) / self.W
        wy = min(max(self.cy + (pos.y() - self.height() / 2) / self.W, 0.0), 1.0)
        lon, lat = unmerc(wx % 1.0, wy)
        return (lon + 180) % 360 - 180, lat

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if getattr(self, 'corner', None) is not None:      # the map switches, top right
            self.corner.adjustSize()
            self.corner.move(self.width() - self.corner.width() - 10, 10)

    def _tile_loaded(self, key, img):
        if key[3] == self.map_theme:
            self.tiles[key] = QtGui.QPixmap.fromImage(img)
            self.update()

    def set_map_theme(self, map_theme):
        """light or dark map: its pictures are styled again (from the tile cache on disk)"""
        self.map_theme = map_theme
        self.tiles = {k: v for k, v in self.tiles.items() if k[3] == map_theme}
        self.update()

    # ---- coordinates
    @property
    def W(self):
        return 256 * 2 ** self.zoom

    def to_screen(self, wx, wy):
        return self.width() / 2 + (wx - self.cx) * self.W, self.height() / 2 + (wy - self.cy) * self.W

    def copies(self):
        """horizontal offsets (px) where the wrapped world is visible"""
        W = self.W
        x0 = self.width() / 2 - self.cx * W
        ks = range(math.floor(-x0 / W) - 1, math.ceil((self.width() - x0) / W) + 1)
        return [k * W for k in ks]

    def center_on(self, lon, lat):
        self.cx, self.cy = merc(lon, lat)
        self._clamp()
        self.update()

    def _clamp(self):
        half = self.height() / 2 / self.W
        self.cy = min(max(self.cy, min(half, 0.5)), max(1 - half, 0.5))
        self.cx %= 1.0

    # ---- interaction
    def wheelEvent(self, e):
        old = self.zoom
        self.zoom = min(9.0, max(1.0, self.zoom + (0.25 if e.angleDelta().y() > 0 else -0.25)))
        # keep the point under the mouse fixed
        mx, my = e.pos().x() - self.width() / 2, e.pos().y() - self.height() / 2
        f = 1 / (256 * 2 ** old) - 1 / self.W
        self.cx += mx * f
        self.cy += my * f
        self._clamp()
        self.update()

    def mousePressEvent(self, e):
        if self.legend_rect.contains(QPointF(e.pos())):
            self.legend_open = not self.legend_open
            theme.save_ui(key_open=self.legend_open)
            self.drag = None
            self.update()
            return
        self.drag = (e.pos(), self.cx, self.cy, False)

    def mouseMoveEvent(self, e):
        if self.drag and e.buttons() & Qt.LeftButton:
            p0, cx, cy, _ = self.drag
            d = e.pos() - p0
            if abs(d.x()) + abs(d.y()) > 3:
                self.drag = (p0, cx, cy, True)
                self.cx, self.cy = cx - d.x() / self.W, cy - d.y() / self.W
                self._clamp()
                self.update()
            return
        if self.picking:
            lon, lat = self.lonlat_at(e.pos())
            QtWidgets.QToolTip.showText(e.globalPos(), 'Click to put your antenna here<br>%.5f, %.5f' % (lat, lon), self)
            return
        hit = self._hit(e.pos())
        if hit:
            QtWidgets.QToolTip.showText(e.globalPos(), hit[1], self)
        else:
            QtWidgets.QToolTip.hideText()

    def mouseReleaseEvent(self, e):
        if self.drag and not self.drag[3] and self.picking and e.button() == Qt.LeftButton:
            lon, lat = self.lonlat_at(e.pos())
            self.locationPicked.emit(lat, lon)
        elif self.drag and not self.drag[3] and not self.legend_rect.contains(QPointF(e.pos())):
            hit = self._hit(e.pos())
            self.satelliteClicked.emit(hit[0] if hit and hit[0] else '')    # ('': empty space - deselect)
        self.drag = None

    def _hit(self, pos):
        best = None
        for x, y, name, tip in self.hits:
            d = math.hypot(x - pos.x(), y - pos.y())
            if d < 12 and (best is None or d < best[0]):
                best = (d, name, tip)
        return best[1:] if best else None

    # ---- drawing
    def path_from(self, pts, closed=False):
        """None entries break the line (stretches where our orbit is too uncertain)"""
        path = QtGui.QPainterPath()
        pen_down = False
        for pt in pts:
            if pt is None:
                pen_down = False
                continue
            x, y = self.to_screen(*merc(pt[1], pt[0]))
            path.lineTo(x, y) if pen_down else path.moveTo(x, y)
            pen_down = True
        if closed:
            path.closeSubpath()
        return path

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor(14, 17, 22))
        self._draw_tiles(p)
        sc = self.scene
        self.hits = []
        if sc:
            offs = self.copies()
            if sc['night']:
                self._draw_night(p, sc, offs)
            self._draw_planes(p, sc, offs)
            for fp in sc['footprints']:
                path = self.path_from(circle_points(fp['lat'], fp['lon'], fp['radius']), closed=True)
                p.setPen(QtGui.QPen(QtGui.QColor(160, 20, 40) if fp['selected'] else QtGui.QColor(200, 110, 0), 1))
                p.setBrush(QtGui.QColor(255, 60, 60, 55) if fp['selected'] else QtGui.QColor(255, 160, 0, 30))
                for o in offs:
                    p.drawPath(path.translated(o, 0))
            for tr in sc['tracks']:
                for pts, col in ((tr['past'], C_PAST), (tr['future'], C_FUTURE)):
                    path = self.path_from(pts)
                    pen = QtGui.QPen(col, 3 if tr['selected'] else 2)
                    pen.setCapStyle(Qt.RoundCap)
                    p.setPen(pen)
                    p.setBrush(Qt.NoBrush)
                    for o in offs:
                        p.drawPath(path.translated(o, 0))
            # measured: what your antenna decoded
            for line in sc['fix_lines']:
                path = self.path_from(line['pts'])
                p.setPen(QtGui.QPen(QtGui.QColor(200, 0, 0, int(220 * line['alpha'])), 2, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
                for o in offs:
                    p.drawPath(path.translated(o, 0))
            for fx in sc['fixes']:
                x, y = self.to_screen(*merc(fx['lon'], fx['lat']))
                c = QtGui.QColor(C_FIX)
                c.setAlpha(int(255 * fx['alpha']))
                p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, int(255 * fx['alpha'])), 1.5))
                p.setBrush(c)
                r = 6 if fx.get('live') else 4.5
                ident = ('net ID %03d  beam %d' % (fx['net_id'], fx['beam']) if fx.get('beam') is not None
                         else 'satellite ID %02X' % fx['net_id'])        # Iridium ring alert / Orbcomm ephemeris
                tip = ('<b>Measured position</b> (decoded by your antenna)<br>%s<br>%s  %s<br>'
                       'lat %+.2f  lon %+.2f  alt %d km<br>signal %.1f dB%s'
                       % (fmt_time(fx['t'], True), fx['name'] or 'unmatched', ident,
                          fx['lat'], fx['lon'], fx['alt_km'], fx['snr'], '<br><b>LIVE</b>' if fx.get('live') else ''))
                for o in offs:
                    p.drawEllipse(QPointF(x + o, y), r, r)
                    self.hits.append((x + o, y, fx['name'], tip))
            # home
            hx, hy = self.to_screen(*merc(HOME_LON, HOME_LAT))
            for o in (offs if catalog.HOME else []):
                p.setPen(QtGui.QPen(Qt.white, 1.5))
                p.setBrush(C_HOME)
                p.drawEllipse(QPointF(hx + o, hy), 4, 4)
                p.setPen(QtGui.QColor(255, 150, 150))
                p.drawText(QPointF(hx + o + 7, hy + 4), 'You')
            # sun
            sx, sy = self.to_screen(*merc(sc['sun'][1], sc['sun'][0]))
            for o in offs:
                p.setPen(QtGui.QPen(QtGui.QColor(230, 120, 0), 1.5))
                p.setBrush(QtGui.QColor(255, 210, 0))
                p.drawEllipse(QPointF(sx + o, sy), 6, 6)
                for k in range(8):
                    a = k * math.pi / 4
                    p.drawLine(QPointF(sx + o + 8 * math.cos(a), sy + 8 * math.sin(a)),
                               QPointF(sx + o + 11 * math.cos(a), sy + 11 * math.sin(a)))
            # predicted satellite positions
            font = p.font()
            font.setPointSize(8)
            p.setFont(font)
            for s in sorted(sc['sats'], key=lambda s: s['selected']):
                x, y = self.to_screen(*merc(s['lon'], s['lat']))
                col = QtGui.QColor(40, 190, 90) if s['el'] > 0 else QtGui.QColor(120, 126, 138)
                tip = ('<b>%s</b> (predicted from our own orbit)<br>%s<br>lat %+.2f  lon %+.2f  alt %.0f km<br>'
                       'uncertainty about &plusmn;%.0f km<br>from you: %s'
                       % (s['name'], s['ident'], s['lat'], s['lon'], s['alt'], s['sigma'],
                          'elevation %.0f&deg;, azimuth %.0f&deg;' % (s['el'], s['az']) if s['el'] > 0 else 'below horizon'))
                faint = s['sigma'] > FAINT_SIGMA_KM
                p.setOpacity(0.45 if faint and not s['selected'] else 1.0)
                # dotted ring = how far off our predicted position could be
                ring = s['sigma'] / 111.2 * self.W / 360 / max(0.1, math.cos(math.radians(s['lat'])))
                for o in offs:
                    if faint and ring > 10:
                        p.setPen(QtGui.QPen(QtGui.QColor(160, 165, 175), 1, Qt.DotLine))
                        p.setBrush(Qt.NoBrush)
                        p.drawEllipse(QPointF(x + o, y), ring, ring)
                    heard = heard_colour(s.get('heard_age'), s.get('sig_q'))
                    if heard is not None:
                        p.setPen(QtGui.QPen(heard, 2))
                        p.setBrush(Qt.NoBrush)
                        p.drawEllipse(QPointF(x + o, y), 15, 15)
                    draw_satellite(p, x + o, y, col, 1.25 if s['selected'] else 0.9, s['system'])
                    if s['selected']:
                        draw_selection(p, x + o, y)
                    if s['selected'] or s['el'] > 0:
                        p.setPen(QtGui.QColor(theme.MAP_INK[self.map_theme]['text']))
                        p.drawText(QPointF(x + o + 12, y - 8), systems.short_name(s['name']))
                    self.hits.append((x + o, y, s['name'], tip))
            p.setOpacity(1.0)
        self._draw_legend(p)
        p.end()

    def _draw_tiles(self, p):
        z = int(min(9, max(1, round(self.zoom))))
        n = 2 ** z
        ts = 256 * 2 ** (self.zoom - z)
        ox, oy = self.width() / 2 - self.cx * self.W, self.height() / 2 - self.cy * self.W
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
        for i in range(math.floor(-ox / ts), math.floor((self.width() - ox) / ts) + 1):
            for j in range(max(0, math.floor(-oy / ts)), min(n, math.floor((self.height() - oy) / ts) + 1)):
                key = (z, i % n, j, self.map_theme)
                rect = QtCore.QRectF(ox + i * ts, oy + j * ts, ts + 0.5, ts + 0.5)
                pm = self.tiles.get(key)
                if pm:
                    p.drawPixmap(rect, pm, QtCore.QRectF(pm.rect()))
                else:
                    self.loader.request(key)
                    # use the parent tile, scaled up, until this one arrives
                    parent = self.tiles.get((z - 1, (i % n) // 2, j // 2, self.map_theme))
                    if parent:
                        src = QtCore.QRectF((i % n) % 2 * 128, j % 2 * 128, 128, 128)
                        p.drawPixmap(rect, parent, src)

    def _draw_planes(self, p, sc, offs):
        font = p.font()
        font.setPointSize(9)
        font.setBold(True)
        p.setFont(font)
        for pl in sc['planes']:
            col = QtGui.QColor(*PLANE_COLORS[pl['plane'] if pl['plane'] in PLANE_COLORS else None])
            col.setAlpha(230 if pl['selected'] else 150)
            pen = QtGui.QPen(col, 4 if pl['selected'] else 2.5)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            pts = pl['ring']
            path = self.path_from(pts)
            scr = [self.to_screen(*merc(lon, lat)) for lat, lon in pts]
            for o in offs:
                p.drawPath(path.translated(o, 0))
            # chevrons pointing the way the satellites move
            for k in range(8, len(pts) - 1, 15):
                if abs(pts[k][0]) > 75:
                    continue
                (x0, y0), (x1, y1) = scr[k], scr[k + 1]
                d = math.hypot(x1 - x0, y1 - y0)
                if d < 1e-6:
                    continue
                ux, uy = (x1 - x0) / d, (y1 - y0) / d
                for o in offs:
                    tip = QPointF(x0 + o + 5 * ux, y0 + 5 * uy)
                    p.drawLine(tip, QPointF(x0 + o - 4 * ux - 5 * uy, y0 - 4 * uy + 5 * ux))
                    p.drawLine(tip, QPointF(x0 + o - 4 * ux + 5 * uy, y0 - 4 * uy - 5 * ux))
            # labels on the northbound and southbound sides, staggered by plane so they don't overlap
            name = 'Plane %d' % pl['plane'] if pl['plane'] else 'Plane ?'
            tip = ('<b>%s</b> (from our own orbits)<br>%d satellite%s: %s<br>'
                   'their orbits agree within %.1f&deg; &middot; plane placed to about &plusmn;%.0f km<br>'
                   'The line is the whole orbit ring at this moment; every satellite in this plane is somewhere on it, '
                   'moving the way the arrows point.'
                   % (name, len(pl['members']), '' if len(pl['members']) == 1 else 's',
                      ', '.join(systems.short_name(m) for m in pl['members']), pl['spread'], pl['sigma']))
            k = 4 * (pl['plane'] or 7)
            fill = QtGui.QColor(col).darker(150)    # white text reads on the bright plane colours
            fill.setAlpha(235)
            for idx, arrow in ((k, '▲'), (len(pts) // 2 + k, '▼')):
                x, y = scr[idx]
                text = '%s %s' % (name, arrow)
                w = p.fontMetrics().width(text)
                for o in offs:
                    r = QtCore.QRectF(x + o - w / 2 - 4, y - 9, w + 8, 18)
                    p.setPen(QtGui.QPen(Qt.white, 1))
                    p.setBrush(fill)
                    p.drawRoundedRect(r, 4, 4)
                    p.drawText(r, Qt.AlignCenter, text)
                    self.hits.append((r.center().x(), y, None, tip))
        font.setBold(False)
        p.setFont(font)

    def _draw_night(self, p, sc, offs):
        # shade each narrow screen column from the terminator to the pole that's in darkness
        dec, slon = sc['sun']
        if abs(dec) < 0.1:
            dec = 0.1 if dec >= 0 else -0.1
        top_dark = dec < 0          # the pole tilted away from the sun is in darkness
        y_top = self.to_screen(0, 0)[1]
        y_bot = self.to_screen(0, 1)[1]
        shade = QtGui.QColor(0, 0, 0, 120)
        step = 3
        for x in range(0, self.width(), step):
            wx = self.cx + (x + step / 2 - self.width() / 2) / self.W
            lon = (wx % 1.0) * 360 - 180
            lat = math.degrees(math.atan(-math.cos(math.radians(lon - slon)) / math.tan(math.radians(dec))))
            y = self.to_screen(*merc(lon, lat))[1]
            if top_dark:
                p.fillRect(QtCore.QRectF(x, y_top, step, y - y_top), shade)
            else:
                p.fillRect(QtCore.QRectF(x, y, step, y_bot - y), shade)

    def _draw_legend(self, p):
        items = [('fix', 'Position it reported'),
                 ('past', 'Track: past'),
                 ('future', 'Track: future'),
                 ('plane', 'Orbital plane (Iridium)'),
                 ('orbcomm', 'Panel edge: orange Orbcomm, green ham'),
                 ('sat', 'Predicted (faded = uncertain)'),
                 ('view', 'Green = above your horizon'),
                 ('live', 'Ring: signal (red weak - green strong)'),
                 ('select', 'Brackets = selected')]
        font = p.font()
        font.setPointSize(8)
        p.setFont(font)
        ink = theme.MAP_INK[self.map_theme]
        fm = p.fontMetrics()
        rows = (len(items) + 1) // 2            # two columns
        row_h = 20
        h = row_h * rows + 30
        open_ = self.legend_open and self.height() > h + 90      # folds itself on a short map
        if not open_:
            r = QtCore.QRectF(10, self.height() - 52, fm.width('Key \u25b8') + 20, 24)
            self.legend_rect = r
            p.setPen(QtGui.QPen(QtGui.QColor(*ink['box_edge'])))
            p.setBrush(QtGui.QColor(*ink['box']))
            p.drawRoundedRect(r, 8, 8)
            p.setPen(QtGui.QColor(ink['text']))
            p.drawText(r, Qt.AlignCenter, 'Key \u25b8')
        else:
            col_w = max(fm.width(t) for _, t in items) + 44
            r = QtCore.QRectF(10, self.height() - h - 24, 2 * col_w + 12, h)
            self.legend_rect = r
            p.setPen(QtGui.QPen(QtGui.QColor(*ink['box_edge'])))
            p.setBrush(QtGui.QColor(*ink['box']))
            p.drawRoundedRect(r, 8, 8)
            p.setPen(QtGui.QColor(ink['muted']))
            p.drawText(QPointF(r.left() + 12, r.top() + 15), 'KEY')
            p.drawText(QtCore.QRectF(r.right() - 22, r.top() + 2, 18, 16), Qt.AlignCenter, '\u2715')
        for k, (kind, text) in enumerate(items if open_ else []):
            y = r.top() + 32 + row_h * (k % rows)
            x = r.left() + 22 + (k // rows) * (col_w + 6)
            if kind == 'fix':
                p.setPen(QtGui.QPen(Qt.white, 1.5))
                p.setBrush(C_FIX)
                p.drawEllipse(QPointF(x, y), 4.5, 4.5)
            elif kind in ('past', 'future'):
                p.setPen(QtGui.QPen(C_PAST if kind == 'past' else C_FUTURE, 3))
                p.drawLine(QPointF(x - 9, y), QPointF(x + 9, y))
            elif kind == 'sat':
                draw_satellite(p, x, y, QtGui.QColor(120, 126, 138), 0.65)
            elif kind == 'orbcomm':
                draw_satellite(p, x, y, QtGui.QColor(120, 126, 138), 0.65, 'orbcomm')
            elif kind == 'plane':
                p.setPen(QtGui.QPen(QtGui.QColor(*PLANE_COLORS[1]), 2.5))
                p.drawLine(QPointF(x - 9, y), QPointF(x + 9, y))
            elif kind == 'select':
                draw_satellite(p, x, y, QtGui.QColor(40, 190, 90), 0.5)
                draw_selection(p, x, y, 7.5)
            elif kind == 'live':        # three small rings: weak, medium, strong
                p.setBrush(Qt.NoBrush)
                for j, q in enumerate((0.0, 0.5, 1.0)):
                    p.setPen(QtGui.QPen(strength_colour(q), 1.6))
                    p.drawEllipse(QPointF(x - 7 + 7 * j, y), 3.5, 3.5)
            else:
                draw_satellite(p, x, y, QtGui.QColor(40, 190, 90), 0.65)
            p.setPen(QtGui.QColor(ink['text']))
            p.drawText(QPointF(x + 16, y + 4), text)
        p.setPen(QtGui.QColor(ink['muted']))
        credit = '© OpenStreetMap contributors'
        p.drawText(QPointF(self.width() - p.fontMetrics().width(credit) - 8, self.height() - 6), credit)


# ---------------------------------------------------------------- timeline
class Timeline(QtWidgets.QWidget):
    timeChanged = QtCore.pyqtSignal(float)

    STEPS = [60, 300, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400, 7 * 86400]

    def __init__(self):
        super().__init__()
        self.setFixedHeight(78)
        self.setCursor(Qt.SizeHorCursor)
        self.setMouseTracking(True)
        now = time.time()
        self.v0, self.v1 = now - 12 * 3600, now + 3 * 3600
        self.t = now
        self.captures, self.fix_times = [], []
        self.setToolTip('Drag to move through time.  Mouse wheel: zoom.  Shift + wheel: scroll.')

    def x_of(self, t):
        return (t - self.v0) / (self.v1 - self.v0) * self.width()

    def t_of(self, x):
        return self.v0 + x / max(1, self.width()) * (self.v1 - self.v0)

    def set_time(self, t, keep_visible=True):
        self.t = t
        if keep_visible:
            span = self.v1 - self.v0
            if t > self.v1 - span * 0.03:
                self.v0, self.v1 = t - span * 0.7, t + span * 0.3
            elif t < self.v0 + span * 0.03:
                self.v0, self.v1 = t - span * 0.3, t + span * 0.7
        self.update()

    def show_range(self, a, b):
        pad = (b - a) * 0.05 + 300
        self.v0, self.v1 = a - pad, b + pad
        self.update()

    def mousePressEvent(self, e):
        self.timeChanged.emit(self.t_of(e.pos().x()))

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton:
            self.timeChanged.emit(self.t_of(min(max(e.pos().x(), 0), self.width())))

    def wheelEvent(self, e):
        span = self.v1 - self.v0
        if e.modifiers() & Qt.ShiftModifier:
            d = -span * 0.1 * (1 if e.angleDelta().y() > 0 else -1)
            self.v0 += d
            self.v1 += d
        else:
            f = 0.8 if e.angleDelta().y() > 0 else 1.25
            new = min(max(span * f, 600), 60 * 86400)
            tm = self.t_of(e.pos().x())
            r = e.pos().x() / max(1, self.width())
            self.v0, self.v1 = tm - new * r, tm + new * (1 - r)
        self.update()

    TRACK_TOP, TRACK_H = 14, 18     # the bar holding receiver runs and fixes

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QtGui.QColor(BG))
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(PANEL))
        p.drawRoundedRect(QtCore.QRectF(0, 0, w, h), 8, 8)
        top, th = self.TRACK_TOP, self.TRACK_H
        mid = top + th / 2
        p.setBrush(QtGui.QColor(SURFACE))
        p.drawRoundedRect(QtCore.QRectF(8, top, w - 16, th), th / 2, th / 2)
        span = self.v1 - self.v0
        font = p.font()
        font.setPointSize(8)
        p.setFont(font)
        fm = p.fontMetrics()

        # time grid and labels
        step = next((s for s in self.STEPS if s / span * w >= 90), self.STEPS[-1])
        local_off = -time.altzone if time.localtime(self.v0).tm_isdst else -time.timezone
        t = math.floor((self.v0 + local_off) / step) * step - local_off
        while t <= self.v1:
            x = self.x_of(t)
            d = datetime.datetime.fromtimestamp(t)
            midnight = d.hour == 0 and d.minute == 0
            label = d.strftime('%a %b %d') if midnight or step >= 86400 else d.strftime('%I:%M %p').lstrip('0')
            p.setPen(QtGui.QPen(QtGui.QColor(IDLE if midnight else BORDER), 1))
            p.drawLine(QPointF(x, top + 2), QPointF(x, top + th - 2))
            lx = x - fm.width(label) / 2
            if 4 <= lx <= w - fm.width(label) - 4:
                p.setPen(QtGui.QColor(TEXT if midnight else MUTED))
                p.drawText(QPointF(lx, top + th + 16), label)
            t += step

        # receiver runs
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(46, 160, 95, 200))
        for c in self.captures:
            x0, x1 = self.x_of(c['start']), self.x_of(c['end'])
            if x1 < 0 or x0 > w:
                continue
            p.drawRoundedRect(QtCore.QRectF(x0, top + 4, max(4, x1 - x0), th - 8), 3, 3)
        # position fixes
        pen = QtGui.QPen(QtGui.QColor(ALERT), 2)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        for t in self.fix_times:
            x = self.x_of(t)
            if 0 <= x <= w:
                p.drawLine(QPointF(x, top + 2), QPointF(x, top + th - 2))

        # now
        x = self.x_of(time.time())
        if 0 <= x <= w:
            p.setPen(QtGui.QPen(QtGui.QColor(ACCENT), 1.5, Qt.DashLine))
            p.drawLine(QPointF(x, 3), QPointF(x, top + th + 3))
            p.setPen(QtGui.QColor(ACCENT))
            p.drawText(QPointF(x + 4, 10), 'now')

        # cursor: line, knob, and a time bubble underneath
        x = self.x_of(self.t)
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 200), 2))
        p.drawLine(QPointF(x, 2), QPointF(x, top + th + 4))
        p.setPen(QtGui.QPen(QtGui.QColor('#ffffff'), 2))
        p.setBrush(QtGui.QColor(ALERT))
        p.drawEllipse(QPointF(x, mid), 6, 6)
        label = fmt_clock(self.t)
        bw = fm.width(label) + 14
        bx = min(max(x - bw / 2, 4), w - bw - 4)
        bubble = QtCore.QRectF(bx, h - 21, bw, 17)
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(ALERT))
        p.drawRoundedRect(bubble, 8, 8)
        p.setPen(QtGui.QColor('#ffffff'))
        p.drawText(bubble, Qt.AlignCenter, label)

        # key
        x = 12
        for color, text, kind in ((QtGui.QColor(46, 160, 95), 'receiver running', 'bar'),
                                  (QtGui.QColor(ALERT), 'reported position', 'tick'),
                                  (QtGui.QColor(ACCENT), 'now', 'tick')):
            y = h - 12
            if kind == 'bar':
                p.setPen(Qt.NoPen)
                p.setBrush(color)
                p.drawRoundedRect(QtCore.QRectF(x, y - 4, 14, 8), 2, 2)
                x += 18
            else:
                p.setPen(QtGui.QPen(color, 2))
                p.drawLine(QPointF(x + 2, y - 5), QPointF(x + 2, y + 5))
                x += 8
            p.setPen(QtGui.QColor(MUTED))
            p.drawText(QPointF(x, y + 4), text)
            x += fm.width(text) + 16
        p.end()


class StatBox(QtWidgets.QFrame):
    """one number with a caption under it, in a small box"""

    def __init__(self):
        super().__init__()
        self.setObjectName('stat')
        theme.style(self, 'QFrame#stat { background: %(SURFACE)s; border: 1px solid %(BORDER)s; border-radius: 6px; }'
                          ' QFrame#stat QLabel { background: transparent; border: none; }')
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 5, 8, 5)
        lay.setSpacing(0)
        self.value = QtWidgets.QLabel()
        theme.style(self.value, 'font-size: 18px; font-weight: 700; color: %(TEXT)s;')
        self.caption = QtWidgets.QLabel()
        theme.style(self.caption, 'font-size: 10px; color: %(MUTED)s;')
        self.caption.setWordWrap(True)
        lay.addWidget(self.value)
        lay.addWidget(self.caption)

    def set(self, value, caption, tip=''):
        self.value.setText(str(value))
        self.caption.setText(caption)
        self.setToolTip(tip)


class ElidedLabel(QtWidgets.QLabel):
    """a one-line label that shortens its text with ... when there isn't room (full text in its tooltip)"""

    def __init__(self, text=''):
        super().__init__(text)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.setMinimumWidth(60)

    def setText(self, text):
        super().setText(text)
        self.setToolTip(text)

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setPen(self.palette().color(QtGui.QPalette.WindowText))
        p.drawText(self.rect(), Qt.AlignLeft | Qt.AlignVCenter,
                   self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.width()))


# ---------------------------------------------------------------- main window
class Tracker(QtWidgets.QMainWindow):
    SPEEDS = [('1x  (real time)', 1), ('10x', 10), ('60x  (1 min/s)', 60), ('300x  (5 min/s)', 300), ('1000x', 1000)]

    def __init__(self, replay=None):
        super().__init__()
        self.setWindowTitle('Satellite Tracker')
        avail = QtWidgets.QApplication.primaryScreen().availableGeometry()
        self.resize(int(min(1700, avail.width() * 0.95)), int(min(1000, avail.height() * 0.92)))
        geo = theme.load_ui().get('window')       # the size and place you left it at (and maximised or not)
        if geo:
            try:
                self.restoreGeometry(QtCore.QByteArray.fromBase64(geo.encode()))
            except (TypeError, ValueError):
                pass
            # a saved size bigger than the screen (saved while the window had spilled over it): back to the default
            n = self.normalGeometry()
            if not n.isValid() or n.width() > avail.width() or n.height() > avail.height():
                self.setGeometry(QtWidgets.QStyle.alignedRect(QtCore.Qt.LeftToRight, QtCore.Qt.AlignCenter, QtCore.QSize(
                    int(min(1700, avail.width() * 0.95)), int(min(1000, avail.height() * 0.92))), avail))
        self.data = Data()
        self.t = time.time()
        self.playing, self.following = False, True
        self.speed = 1
        self.selected = None
        self.last_tick = time.time()
        self.pass_cache = (None, 0, None)
        self._ham_known = None      # how many satellites gr-satellites can decode (counted once)

        self.map = MapView()
        self.map.satelliteClicked.connect(self.select)
        self.sat_info = None
        self.timeline = Timeline()
        self.timeline.timeChanged.connect(self.scrub)

        # controls under the map
        self.time_label = QtWidgets.QLabel()
        self.time_label.setStyleSheet('font-size: 16px; font-weight: 600; min-width: 270px;')
        self.play_btn = QtWidgets.QPushButton('Play')
        self.play_btn.clicked.connect(self.toggle_play)
        self.live_btn = QtWidgets.QPushButton('Live (now)')
        self.live_btn.setCheckable(True)
        self.live_btn.clicked.connect(self.go_live)
        self.speed_box = QtWidgets.QComboBox()
        for label, _ in self.SPEEDS:
            self.speed_box.addItem(label)
        self.speed_box.setCurrentIndex(2)
        self.speed_box.currentIndexChanged.connect(lambda i: setattr(self, 'speed', self.SPEEDS[i][1]))
        self.speed = 60
        back = QtWidgets.QPushButton('-10 min')
        back.clicked.connect(lambda: self.scrub(self.t - 600))
        fwd = QtWidgets.QPushButton('+10 min')
        fwd.clicked.connect(lambda: self.scrub(self.t + 600))
        fit = QtWidgets.QPushButton('Show all history')
        fit.clicked.connect(self.fit_history)
        self.replay_box = QtWidgets.QComboBox()
        self.replay_box.activated.connect(self.replay)

        row = QtWidgets.QHBoxLayout()
        for wdg in (self.time_label, self.live_btn, self.play_btn, self.speed_box, back, fwd, fit,
                    QtWidgets.QLabel('  Replay:'), self.replay_box):
            row.addWidget(wdg)
        row.addStretch(1)

        opts = QtWidgets.QHBoxLayout()
        self.sys_box = QtWidgets.QComboBox()      # which satellite systems to show
        self.sys_box.addItem('Systems: none', NO_SYSTEM)
        self.sys_box.addItem('Systems: all', None)
        for key, label, _, _ in CATEGORIES:
            self.sys_box.addItem(label, key)
        self.sys_box.setToolTip('Show no satellites, every one, or one category (and pick its systems)')
        self.sub_off = set()            # systems turned off under their category (e.g. {'glonass'})
        self.sub_chips = []             # (system, button, its row) in every window's filter
        self.tracks_box = QtWidgets.QComboBox()
        # (the order is what refresh() reads: 0 overhead + selected, 1 selected, 2 all, 3 none)
        self.tracks_box.addItems(['Tracks: overhead + selected', 'Tracks: selected only', 'Tracks: all satellites',
                                  'Tracks: none'])
        self.tracks_box.setToolTip('Which satellites get their orbit track drawn on the map and globe:\n'
                                   'overhead (above your horizon now) plus the one you selected, only the selected one,\n'
                                   'every satellite with an orbit, or none')
        self.tracks_box.setCurrentIndex(1)          # starts on selected only, every time
        self.fp_box = QtWidgets.QComboBox()
        self.fp_box.addItems(['Footprint: selected', 'Footprints: all in view', 'Footprints: none'])
        self.fix_box = QtWidgets.QComboBox()
        self.fix_box.addItems(['Reported positions: last hour', 'Reported positions: all', 'Reported positions: none'])
        self.fix_box.setToolTip('Positions the satellites reported themselves and your antenna decoded\n'
                                '(Iridium ring alerts, Orbcomm ephemeris packets) - red dots on the map')
        self.planes_box = QtWidgets.QComboBox()
        self.planes_box.addItems(['Planes: all', "Planes: selected satellite's", 'Planes: none'])
        self.night_chk = QtWidgets.QCheckBox('Day/night', self)      # (driven by the switch on the map)
        self.night_chk.setChecked(theme.load_ui().get('night', True))
        self.night_chk.hide()
        self.night_chk.stateChanged.connect(lambda _: theme.save_ui(night=self.night_chk.isChecked()))
        self.sys_box.currentIndexChanged.connect(self.reload_lists)
        for wdg in (self.sys_box, self.tracks_box, self.fp_box, self.fix_box, self.planes_box, self.night_chk):
            if wdg is not self.night_chk:
                opts.addWidget(wdg)
            sig = wdg.stateChanged if isinstance(wdg, QtWidgets.QCheckBox) else wdg.currentIndexChanged
            sig.connect(self.refresh)
        self.map_theme_box = QtWidgets.QComboBox(self)  # the map (and the globe's Earth): dark or light (the switch)
        self.map_theme_box.hide()
        self.map_theme_box.addItem('Map: dark', 'dark')
        self.map_theme_box.addItem('Map: light', 'light')
        self.map_theme_box.setToolTip('The map and the globe\'s Earth: dark, or light as OpenStreetMap draws it')
        self.map_theme_box.setCurrentIndex(self.map_theme_box.findData(theme.load()[1]))
        self.map.set_map_theme(self.map_theme_box.currentData())
        self.map_theme_box.currentIndexChanged.connect(lambda _: self.set_map_theme(self.map_theme_box.currentData()))
        opts.addStretch(1)
        self.map.corner, self.map_theme_btn, self.night_btn = self._map_switches(self.map)

        # receiver bar: start / stop the SDR receiver and decoder
        self.receiver = receiver.Receiver(replay)
        self.receiver.status.connect(self._receiver_status)
        self.receiver.running.connect(self._receiver_running)
        self.receiver.decoded.connect(self.poll)
        self.receiver.frame.connect(self._frame)
        self.activity = lights.ActivityLights()
        self.rx_btn = QtWidgets.QPushButton()
        self.rx_btn.setMinimumWidth(170)
        self.rx_btn.clicked.connect(self.toggle_receiver)
        self.rx_btn.setToolTip('Runs the SDR receiver and decoder. Close SDR++ and SatDump first - only one '
                               'program can use the SDR. When you stop, the capture is added to the catalog.')
        self.rx_system = QtWidgets.QComboBox()       # which system to receive (also in Source)
        for sy in systems.ORDER:
            self.rx_system.addItem('%s  %s' % (sy['label'], systems.band_tag(sy)), sy['key'])
        self.rx_system.setCurrentIndex(max(0, self.rx_system.findData(sdr_settings.load()['system'])))
        self.rx_system.currentIndexChanged.connect(self._rx_system_changed)
        self._rx_system_tip()
        self.src_btn = QtWidgets.QPushButton('\U0001F4E1  Source')
        self.src_btn.setToolTip('Choose the SDR and how it is tuned: sample rate, frequency, gain, PPM, bias-T...')
        self.src_btn.clicked.connect(self.show_source)
        self.spec_btn = QtWidgets.QPushButton('Spectrum')
        self.spec_btn.setCheckable(True)
        self.spec_btn.setChecked(True)
        self.spec_btn.setToolTip('Show or hide the live spectrum and waterfall')
        self.sky_btn = QtWidgets.QPushButton('\U0001F9ED  Sky')
        self.sky_btn.setToolTip('Sky plot: where in your sky the satellites were when your antenna heard them -\n'
                                'shows blind spots, e.g. overhead or behind the house')
        self.sky_btn.clicked.connect(self.show_sky)
        self.sky = None
        self.signal_btn = QtWidgets.QPushButton('\U0001F4F6  Signal')
        self.signal_btn.setToolTip('How strongly your receiver hears each satellite: live bars, and the selected '
                                   'one\'s signal over the last ten minutes')
        self.signal_btn.clicked.connect(self.show_signal)
        self.signal_win = None
        self.globe_btn = QtWidgets.QPushButton('\U0001F310  Globe')
        self.globe_btn.setToolTip('A 3D globe: the satellites, their orbital planes and your fixes around the Earth')
        self.globe_btn.clicked.connect(self.show_globe)
        self.globe = None
        self.rx_dot = QtWidgets.QLabel()
        self.rx_status = ElidedLabel('Receiver off')
        theme.style(self.rx_status, 'color: %(MUTED)s;')
        title = QtWidgets.QLabel('SATELLITE TRACKER')
        theme.style(title, 'font-size: 13px; font-weight: 700; letter-spacing: 2px; color: %(TEXT)s;')
        header = QtWidgets.QFrame()
        theme.style(header, 'QFrame#hdr { background: %(PANEL)s; border: 1px solid %(BORDER)s; border-radius: 8px; }'
                            ' QFrame#hdr QLabel { background: transparent; }')
        header.setObjectName('hdr')
        # the name on the left and every button together on the right; under them, the receiver's lights and
        # status across the whole width
        hv = QtWidgets.QVBoxLayout(header)
        hv.setContentsMargins(12, 6, 8, 6)
        hv.setSpacing(4)
        hl = QtWidgets.QHBoxLayout()
        hv.addLayout(hl)
        status_row = QtWidgets.QHBoxLayout()
        hv.addLayout(status_row)
        hl.addWidget(title)
        # the two sections: the tracker (map, satellites) and the pictures received (Meteor-M weather images)
        self.section_btns = []
        for k, text in enumerate(('\U0001F6F0  Tracker', '\U0001F5BC  Images', '\U0001F4FB  Radio',
                                  '\U0001F6E1  Integrity')):
            b = QtWidgets.QPushButton(text)
            b.setCheckable(True)
            b.setChecked(k == 0)
            b.clicked.connect(lambda _, k=k: self.show_section(k))
            theme.style(b, 'QPushButton { padding: 4px 12px; } QPushButton:checked { background: %(SEL_BG)s; '
                           'border: 1px solid %(ACCENT)s; color: %(TEXT)s; font-weight: 600; }')
            b.setMinimumWidth(b.sizeHint().width() + 16)
            self.section_btns.append(b)
            hl.addWidget(b)
        self.section_btns[1].setToolTip('Weather pictures from the Meteor-M satellites: receive a pass, view and '
                                        'save the pictures')
        self.section_btns[2].setToolTip('Listen anywhere the SDR reaches, like SDR++: big spectrum and waterfall, '
                                        'frequency digits, mode and bandwidth')
        self.section_btns[3].setToolTip('Jamming and spoofing watch on GNSS L1 (GPS, Galileo, WAAS): noise, signal '
                                        'strength, position, time and Doppler checks, with a snapshot of each event')
        self.integrity_level = -1
        hl.addStretch(1)
        hl.addWidget(self.rx_btn)
        hl.addWidget(self.rx_system)
        hl.addWidget(self.src_btn)
        hl.addWidget(self.spec_btn)
        hl.addWidget(self.sky_btn)
        hl.addWidget(self.globe_btn)
        hl.addWidget(self.signal_btn)
        hl.addSpacing(8)
        status_row.addWidget(self.rx_dot)
        status_row.addWidget(self.activity)
        status_row.addWidget(self.rx_status, 1)
        # location and storage live in Settings now (these keep their texts for its tooltip)
        self.loc_btn = QtWidgets.QPushButton(self)
        self.loc_btn.hide()
        self.data_btn = QtWidgets.QPushButton(self)
        self.data_btn.hide()
        self.chime = sound.Chime(self)
        self.known_sats = None          # every satellite ever listed (set at the first poll): new ones chime
        self.chime_btn = QtWidgets.QPushButton('\U0001F514')
        self.chime_btn.setCheckable(True)
        self.chime_btn.setChecked(theme.load_ui().get('chime', True))
        self.chime_btn.setToolTip('Chime when a satellite you have never heard before shows up\n'
                                  '(it is also noted in the log under the list). Click to turn it off or on.')
        self.chime_btn.toggled.connect(self._chime_toggled)
        self._chime_toggled(self.chime_btn.isChecked(), save=False)
        hl.addWidget(self.chime_btn)
        self.theme_btn = QtWidgets.QPushButton()      # the whole window: light or dark
        self.theme_btn.setToolTip('Switch the whole window between dark and light\n'
                                  '(the map has its own setting under it; plots stay dark like an instrument)')
        self.theme_btn.clicked.connect(lambda: self.set_app_theme('light' if theme.name == 'dark' else 'dark'))
        self._theme_btn_text()
        hl.addWidget(self.theme_btn)
        self.settings_btn = QtWidgets.QPushButton('\u2699')
        self.settings_btn.setStyleSheet('font-size: 15px; padding: 3px 10px;')
        self.settings_btn.clicked.connect(lambda: self.show_settings(0))
        hl.addWidget(self.settings_btn)
        self._show_location()
        self._receiver_running(False)

        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(4, 4, 4, 4)
        self.pick_bar = QtWidgets.QFrame()
        self.pick_bar.setObjectName('pick')
        theme.style(self.pick_bar, 'QFrame#pick { background: %(PICK)s; border: 1px solid %(ACCENT)s; border-radius: 8px; }'
                                   ' QFrame#pick QLabel { background: transparent; }')
        pl = QtWidgets.QHBoxLayout(self.pick_bar)
        pl.setContentsMargins(12, 6, 8, 6)
        pick_text = QtWidgets.QLabel(
            '<b>\U0001F4CD Click your antenna\u2019s location on the map.</b> &nbsp;Zoom in with the mouse wheel '
            '(and drag to move) for a precise spot. The height there is looked up automatically '
            '(the point is sent to open-meteo.com).')
        pick_text.setWordWrap(True)
        pl.addWidget(pick_text, 1)
        typed = QtWidgets.QPushButton('Type coordinates...')
        typed.clicked.connect(self.type_location)
        self.gps_loc_btn = QtWidgets.QPushButton()
        self.gps_loc_btn.clicked.connect(self.use_gnss_location)
        pl.addWidget(self.gps_loc_btn)
        self.pick_cancel = QtWidgets.QPushButton('Cancel')
        self.pick_cancel.clicked.connect(self.cancel_pick)
        pl.addWidget(typed)
        pl.addWidget(self.pick_cancel)
        self.pick_bar.hide()
        self.map.locationPicked.connect(self._picked)
        lv.addWidget(self.pick_bar)
        # map above, live spectrum + waterfall below (drag the divider; the Spectrum button hides it)
        self.spectrum = spectrum.SpectrumPanel()
        self.receiver.spectrum_port = self.spectrum.view.port
        self.sound = sound.SoundPlayer(self)
        self.receiver.audio_port = self.sound.port
        self.spectrum.attach_sound(self.sound)
        # the map's options (tracks, footprints, fixes, planes, day/night, map theme) in a bar right on top of it
        map_pane = QtWidgets.QWidget()
        mp = QtWidgets.QVBoxLayout(map_pane)
        mp.setContentsMargins(0, 0, 0, 0)
        mp.setSpacing(0)
        bar = QtWidgets.QFrame()
        bar.setObjectName('mapbar')
        theme.style(bar, 'QFrame#mapbar { background: %(PANEL)s; border: 1px solid %(BORDER)s; border-bottom: none;'
                         ' border-top-left-radius: 8px; border-top-right-radius: 8px; }')
        opts.setContentsMargins(8, 4, 8, 4)
        bar.setLayout(opts)
        mp.addWidget(bar)
        mp.addWidget(self.map, 1)
        # the main view: the map, or the globe, sky or signal in its place (each can also be popped out)
        self.deck = popout.ViewDeck([
            ('map', '\U0001F5FA  Map', '2D map', lambda: map_pane),
            ('globe', '\U0001F30D  Globe', 'Globe', self._make_globe),
            ('sky', '✦  Sky', 'Sky plot', self._make_sky),
            ('signal', '\U0001F4F6  Signal', 'Signal', self._make_signal),
        ], shown=self._view_shown)
        self.map_split = QtWidgets.QSplitter(Qt.Vertical)
        self.map_split.addWidget(self.deck)
        self.map_split.addWidget(self.spectrum)
        self.map_split.setStretchFactor(0, 3)
        self.map_split.setStretchFactor(1, 2)
        theme.remember_splitter(self.map_split, 'map_spectrum', [560, 330])     # (where you last dragged it)
        self.spec_btn.toggled.connect(self.spectrum.setVisible)
        lv.addWidget(self.map_split, 1)
        # the timeline and its controls: can be popped out too
        self.timeline_box = QtWidgets.QWidget()
        tb = QtWidgets.QVBoxLayout(self.timeline_box)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.addWidget(self.timeline)
        tb.addLayout(row)
        tl_pop = QtWidgets.QToolButton()
        tl_pop.setText(popout.POP)
        tl_pop.setToolTip('Pop the timeline out into a window of its own (close it to bring it back)')
        tl_pop.clicked.connect(self.pop_timeline)
        row.addWidget(tl_pop)
        self.timeline_win = None
        self.timeline_note = None
        lv.addWidget(self.timeline_box)
        self.left_layout = lv

        # side panel: which systems to show (drives the Systems filter), then numbers about them.
        # The Sky and Globe windows get the same buttons; they all drive (and follow) sys_box.
        self.sys_groups = []
        sys_row, self.sys_buttons = self._sys_bar(38)
        self.sys_box.hide()         # the buttons above the satellite list replace it
        self.stats = [StatBox() for _ in range(4)]
        stat_row = QtWidgets.QHBoxLayout()
        stat_row.setSpacing(4)
        for b in self.stats:
            stat_row.addWidget(b, 1)
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet('font-size: 10px;')
        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(['Satellite', 'Slot', 'Net ID', 'Tracks', 'Heard', 'At this time'])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        # no built-in selection: it would paint over the cells' own colours (blue name, green "up").
        # A click picks the satellite and _update_table highlights it.
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.cellClicked.connect(self._table_clicked)
        self.sat_lights = lights.SatelliteLights(self)     # a light by each satellite, flashing when it's heard
        self.table.setItemDelegateForColumn(0, lights.SatelliteLightDelegate(self.sat_lights, self.table))
        self.sat_lights.changed.connect(self.table.viewport().update)
        pal = self.table.palette()      # no row-wide selection colour: _update_table highlights the name
        for group in (QtGui.QPalette.Active, QtGui.QPalette.Inactive):
            pal.setColor(group, QtGui.QPalette.Highlight, QtGui.QColor(0, 0, 0, 0))
            pal.setColor(group, QtGui.QPalette.HighlightedText, QtGui.QColor(255, 255, 255))
        self.table.setPalette(pal)
        self.detail = QtWidgets.QLabel('Click a satellite on the map or in the list.')
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.RichText)
        self.detail.setAlignment(Qt.AlignTop)
        theme.style(self.detail, 'background: %(PANEL)s; padding: 8px;')
        self.detail_scroll = QtWidgets.QScrollArea()     # the details can be longer than the space
        self.detail_scroll.setWidget(self.detail)
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setMinimumHeight(260)
        btns = QtWidgets.QHBoxLayout()
        b1 = QtWidgets.QPushButton('Center on map')
        b1.clicked.connect(self.center_selected)
        b2 = QtWidgets.QPushButton('Jump to next pass')
        b2.clicked.connect(self.jump_next_pass)
        b3 = QtWidgets.QPushButton('Deselect')
        b3.setToolTip('Deselect the satellite (or click empty space on the map, globe or sky, or press Esc)')
        b3.clicked.connect(lambda: self.select(None))
        QtWidgets.QShortcut(QtGui.QKeySequence(Qt.Key_Escape), self, activated=lambda: self.select(None))
        for b in (b1, b2, b3):
            btns.addWidget(b)
        self.events = QtWidgets.QListWidget()
        self.events.setMaximumHeight(170)

        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.addLayout(sys_row)
        rv.addLayout(stat_row)
        rv.addWidget(self.summary)
        rv.addWidget(self.table, 1)
        rv.addWidget(self.detail_scroll, 1)
        rv.addLayout(btns)
        rv.addWidget(QtWidgets.QLabel('<span style="color:%s; font-weight:600">LIVE FROM THE RECEIVER</span>' % MUTED))
        rv.addWidget(self.events)

        split = QtWidgets.QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        right.setMinimumWidth(380)
        theme.remember_splitter(split, 'tracker_list', [max(600, self.width() - 520), 520])
        # the header (receiver, sections) over both sections; the tracker and the Images section under it
        self.sections = QtWidgets.QStackedWidget()
        self.sections.addWidget(split)
        import images_view
        self.images_view = images_view.ImagesView(self)
        self.sections.addWidget(self.images_view)
        import radio_view
        self.radio_view = radio_view.RadioView(self)
        self.sections.addWidget(self.radio_view)
        import integrity
        self.integrity = integrity.IntegrityView(self, self._gnss_orbit_of, lambda: (HOME_LAT, HOME_LON, HOME_H))
        self.sections.addWidget(self.integrity)
        self.receiver.frame.connect(self.integrity.on_line)
        self.receiver.gnss_line.connect(self.integrity.on_line)
        self.receiver.running.connect(self._integrity_running)
        self.integrity.changed.connect(self._integrity_level)
        central = QtWidgets.QWidget()
        cv = QtWidgets.QVBoxLayout(central)
        cv.setContentsMargins(4, 4, 4, 0)
        cv.setSpacing(2)
        cv.addWidget(header)
        cv.addWidget(self.sections, 1)
        # A fixed minimum for the whole content: without it the window's minimum is the sum of every panel's, which
        # grew past a 1080p screen's height (the window then spilled under the taskbar and out of maximised); now the
        # panels share whatever room the screen has
        central.setMinimumSize(900, 560)
        self.setCentralWidget(central)
        self.status = QtWidgets.QLabel()
        self.status.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)   # (long text)
        self.statusBar().addWidget(self.status, 1)

        self.reload_lists()
        self.go_live()
        main = theme.load_ui().get('main_view', 'map')        # the main view you had last time
        self.deck.show_view(main if main in self.deck.views else 'map')
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(100)
        self.poll_timer = QtCore.QTimer(self)
        self.poll_timer.timeout.connect(self.poll)
        self.poll_timer.start(2000)
        self.data_timer = QtCore.QTimer(self)      # keep the data size on the button current
        self.data_timer.timeout.connect(self._show_data_size)
        self.data_timer.start(60000)
        QtCore.QTimer.singleShot(3000, self._housekeeping)
        self.poll()
        if catalog.HOME is None:
            self.start_pick()

    # ---- activity lights
    def _frame(self, kind, line):
        ok = kind not in receiver.DAMAGED
        self.activity.flash(ok)
        if ok:      # (a damaged burst can't say which satellite sent it)
            name = self._satellite_of(line)
            if name:
                self.sat_lights.flash(name)

    def _satellite_of(self, line):
        """which satellite sent a frame: by plane/slot (time & location) or network ID (ring alerts, broadcasts);
        Orbcomm packets carry their satellite's ID"""
        pk = catalog.read_orbcomm([line]) if line.startswith('O') else None
        if pk:
            return self.data.oc_ids.get(pk[0][3])
        m = re.search(r' P(\d) S(\d+) ', line)
        if m:
            return self.data.slot_name.get((int(m.group(1)), int(m.group(2))))
        m = re.search(r'sat:(\d+)', line)
        if m:
            net = int(m.group(1))
            return next((n for n, i in self.data.info.items() if i['system'] == 'iridium' and net in i['net_ids']), None)
        return None

    # ---- globe
    def _make_globe(self):
        """the globe (made once): shown in the main view or in its own window"""
        self.globe = globe.GlobeWindow(self, (HOME_LAT, HOME_LON), self._globe_tile, self.map.map_theme)
        self.globe.setWindowFlags(Qt.Widget)
        self.globe.view.satelliteClicked.connect(self.select)
        self.globe.sysbar = self._sys_window_bar()
        self.globe.layout().insertWidget(0, self.globe.sysbar)
        self.globe.layout().insertWidget(1, self._globe_options())
        self.globe.view.corner = self._map_switches(self.globe.view)[0]
        self.globe_info = QtWidgets.QLabel()
        self.globe_info.setTextFormat(Qt.RichText)
        self.globe_info.setContentsMargins(10, 4, 10, 6)
        self.globe.layout().addWidget(self.globe_info)
        return self.globe

    def show_globe(self):
        """the Globe button: the globe in a window of its own"""
        self.deck.pop_out('globe')

    def _map_switches(self, parent):
        """the map's two switches, for the top right corner of a map or globe: light / dark map and day / night
        shading - tied to the (hidden) map theme box and night checkbox, so every copy stays in step"""
        box = QtWidgets.QFrame(parent)
        box.setObjectName('switches')
        theme.style(box, 'QFrame#switches { background: %(PANEL)s; border: 1px solid %(BORDER)s; border-radius: 8px; }')
        lay = QtWidgets.QHBoxLayout(box)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        mt = QtWidgets.QPushButton()
        mt.setToolTip('Switch the map (and the globe\'s Earth) between dark and light, as OpenStreetMap draws it')
        mt.clicked.connect(lambda: self.map_theme_box.setCurrentIndex(1 - self.map_theme_box.currentIndex()))
        dn = QtWidgets.QPushButton('\U0001F317  Day / night')
        dn.setCheckable(True)
        dn.setToolTip('Shade the night side of the Earth (from the Sun\'s real position)')
        dn.setChecked(self.night_chk.isChecked())
        dn.toggled.connect(self.night_chk.setChecked)
        self.night_chk.toggled.connect(dn.setChecked)

        def label(_=None):
            mt.setText('\u2600  Light map' if self.map_theme_box.currentData() == 'dark' else '\u263e  Dark map')
        label()
        self.map_theme_box.currentIndexChanged.connect(label)
        cov = QtWidgets.QPushButton('\u25ce  Coverage')
        cov.setCheckable(True)
        cov.setToolTip('Show or hide the coverage area - the ground that can see the satellite\n'
                       '(the selected one, or every one in view: the Footprint box above the map)')
        cov.setChecked(self.fp_box.currentIndex() != 2)
        cov.toggled.connect(self._coverage_switch)
        self.fp_box.currentIndexChanged.connect(lambda i: (cov.blockSignals(True), cov.setChecked(i != 2),
                                                           cov.blockSignals(False)))
        lay.addWidget(mt)
        lay.addWidget(dn)
        lay.addWidget(cov)
        box.adjustSize()
        return box, mt, dn

    def _mirror(self, src):
        """a copy of one of the map's option boxes for another window, kept in step with it both ways"""
        c = QtWidgets.QComboBox()
        for k in range(src.count()):
            c.addItem(src.itemText(k), src.itemData(k))
        c.setToolTip(src.toolTip())
        c.setCurrentIndex(src.currentIndex())
        c.currentIndexChanged.connect(src.setCurrentIndex)
        src.currentIndexChanged.connect(c.setCurrentIndex)
        return c

    def _globe_options(self):
        """the map's options for the globe window: tracks, footprints, fixes, planes"""
        w = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(w)
        row.setContentsMargins(8, 4, 8, 0)
        for src in (self.tracks_box, self.fp_box, self.fix_box, self.planes_box):
            row.addWidget(self._mirror(src))
        row.addStretch(1)
        return w

    def _globe_info_text(self):
        """the line under the globe: what it shows, and the selected satellite"""
        sc = self.map.scene or {}
        sats = sc.get('sats', [])
        up = sum(1 for s in sats if s['el'] > 0)
        heard = sum(1 for s in sats if s['live'])
        txt = ('<b>%d</b> satellites shown &middot; <b>%d</b> above your horizon%s &middot; %s'
               % (len(sats), up, ' &middot; <b>%d</b> heard in the last minute' % heard if self.following else '',
                  ('<b style="color:%s">LIVE</b> ' % GOOD if self.following else '') + fmt_time(self.t, True)))
        s = next((s for s in sats if s['selected']), None)
        if s:
            txt += ('<br><span style="color:%s">\u25a0</span> <b>%s</b> &middot; %.0f km up &middot; %s &middot; '
                    'lat %+.1f, lon %+.1f' % (C_SELECT.name(), s['name'], s['alt'],
                                              '%.0f\u00b0 up, azimuth %.0f\u00b0' % (s['el'], s['az']) if s['el'] > 0
                                              else 'below your horizon', s['lat'], s['lon']))
        elif self.selected:
            txt += '<br><b>%s</b>: not on the globe now (no orbit, or too uncertain at this time)' % self.selected
        return txt

    def _make_signal(self):
        """the Signal view (made once): live bars of how strongly each satellite is heard"""
        self.signal_win = signalview.SignalWindow(self)
        self.signal_win.setWindowFlags(Qt.Widget)
        self.signal_win.sysbar = self._sys_window_bar()
        self.signal_win.layout().insertWidget(0, self.signal_win.sysbar)
        self.sys_box.currentIndexChanged.connect(lambda _: self.signal_win.refresh())
        return self.signal_win

    def show_signal(self):
        """the Signal button: the Signal view in a window of its own"""
        self.deck.pop_out('signal')

    def _coverage_switch(self, on):
        """coverage areas on (as they were: the selected satellite's, or all in view) or off"""
        if on:
            self.fp_box.setCurrentIndex(getattr(self, '_fp_was', 0))
        else:
            if self.fp_box.currentIndex() != 2:
                self._fp_was = self.fp_box.currentIndex()
            self.fp_box.setCurrentIndex(2)

    def _globe_tile(self, z, x, y):
        """a map tile for the globe's Earth, in the map's theme"""
        return load_tile_rgb(z, x, y, self.map.map_theme)

    # ---- the new-satellite chime
    def _chime_toggled(self, on, save=True):
        self.chime_btn.setText('🔔' if on else '🔕')
        if save:
            theme.save_ui(chime=on)

    def _check_new_satellites(self):
        """a satellite listed for the first time ever (not in the catalog when the tracker started, nor in the
        capture it was receiving then): note it in the log and chime"""
        listed = set(self.data.tracked + self.data.doppler_only)
        if self.known_sats is None:         # the first look: everything there now was heard before
            self.known_sats = listed
            return
        new = sorted(listed - self.known_sats)
        self.known_sats |= listed
        for n in new:
            self.events.insertItem(0, '%s  NEW SATELLITE  %s - heard for the first time' % (fmt_clock(time.time()), n))
        if new and self.chime_btn.isChecked():
            self.chime.play()

    # ---- light / dark
    def _theme_btn_text(self):
        self.theme_btn.setText('\u2600  Light' if theme.name == 'dark' else '\u263e  Dark')

    def set_app_theme(self, name, save=True):
        """the whole window light or dark, straight away (and next time)"""
        theme.set_theme(name)
        apply_app_colours(QtWidgets.QApplication.instance())
        self._theme_btn_text()
        self.reload_lists()
        self.refresh()
        for w in QtWidgets.QApplication.topLevelWidgets():
            w.update()
        if save:
            theme.save(theme.name, self.map.map_theme)

    def set_map_theme(self, name, save=True):
        """the map and the globe's Earth light or dark (and next time)"""
        self.map.set_map_theme(name)
        if self.globe is not None:
            self.globe.view.set_theme(name, self._globe_tile)
        if save:
            theme.save(theme.name, name)

    # ---- sky plot
    def show_section(self, k):
        """0: the tracker, 1: Images, 2: Radio, 3: Integrity (the map's own buttons are hidden in the others; the
        spectrum comes along, except to Integrity)"""
        # the spectrum / waterfall / sound panel goes with the section shown (one panel, one set of settings)
        visible = self.spec_btn.isChecked()
        self.spectrum.view.pan_retune = None        # (dragging along the band: the Radio section only)
        self.spectrum.view.set_section(('tracker', 'images', 'radio', 'tracker')[k])  # (its own trace / waterfall split)
        if k == 1:
            self.images_view.take_spectrum(self.spectrum)
        elif k == 2:
            self.radio_view.take_spectrum(self.spectrum)
        elif self.map_split.indexOf(self.spectrum) < 0:
            self.map_split.insertWidget(1, self.spectrum)
            theme.restore_splitter(self.map_split, 'map_spectrum', [560, 330])
        self.spectrum.setVisible(visible)
        self.sections.setCurrentIndex(k)
        for i, b in enumerate(self.section_btns):
            b.setChecked(i == k)
        for b in (self.sky_btn, self.globe_btn, self.signal_btn):
            b.setVisible(k == 0)
        if k == 1:
            self.images_view.refresh()

    def _gnss_orbit_of(self, sat):
        """a GNSS satellite's own broadcast orbit, if decoded and still fresh (for the Integrity checks)"""
        import gnss
        o = self.data.orbits.get(catalog.gnss_name(sat))
        if not isinstance(o, (gnss.EphemerisOrbit, gnss.SBASOrbit, gnss.GlonassOrbit)):
            return None
        try:
            return o if float(np.asarray(o.sigma_km(time.time())).reshape(-1)[0]) < 20 else None
        except Exception:
            return None

    def _integrity_running(self, on):
        """the receiver started / stopped: the Integrity watch follows (with the receiver's settings, which go into
        any event snapshot)"""
        s = self.receiver.sdr
        g, auto = sdr_settings.gain(s)
        meta = {'device': s.get('label', ''), 'device_args': sdr_settings.device_args(s), 'gain_db': g,
                'auto_gain': auto, 'if_gain_db': sdr_settings.if_gain(s), 'ppm': s.get('ppm', 0),
                'center_hz': sdr_settings.center_hz(s), 'sample_rate': sdr_settings.sample_rate(s),
                'capture': getattr(self.receiver, 'capname', '')}
        self.integrity.on_running(on, getattr(self.receiver, 'system', None), auto, meta)

    def _integrity_level(self, lv):
        """the Integrity section's button shows a watch / alert from any section"""
        if lv == self.integrity_level:
            return
        self.integrity_level = lv
        b = self.section_btns[3]
        b.setText('\U0001F6E1  Integrity' + {1: '  ●', 2: '  ⚠'}.get(lv, ''))
        col = {1: 'WARN', 2: 'ALERT'}.get(lv)
        extra = ('QPushButton { color: %%(%s)s; font-weight: 700; border: 1px solid %%(%s)s; }' % (col, col)) if col else ''
        theme.style(b, 'QPushButton { padding: 4px 12px; } QPushButton:checked { background: %(SEL_BG)s; '
                       'border: 1px solid %(ACCENT)s; color: %(TEXT)s; font-weight: 600; }' + extra)
        b.setMinimumWidth(b.sizeHint().width() + 16)

    def _make_sky(self):
        """the sky plot (made once)"""
        self.sky = skyplot.SkyDialog(self, self.data, from_home, self._run_start, self.shows)
        self.sky.setWindowFlags(Qt.Widget)
        self.sky.sysbar = self._sys_window_bar()
        self.sky.layout().insertWidget(0, self.sky.sysbar)
        self.sys_box.currentIndexChanged.connect(self.sky.refresh)
        return self.sky

    def show_sky(self):
        """the Sky button: the sky plot in a window of its own"""
        self.deck.pop_out('sky')

    def _view_shown(self, key):
        """a view came on screen (main view or its window): bring it up to date. The system buttons inside the
        globe / sky / signal are for their own windows - in the main view the side panel's are right there."""
        w = self.deck.content.get(key)
        if hasattr(w, 'sysbar'):
            w.sysbar.setVisible(key in self.deck.windows)
        if key == 'globe':
            self.globe.view.home = (HOME_LAT, HOME_LON)
            self.globe.view.set_scene(self.map.scene)
        elif key == 'sky':
            self.sky.refresh()
        elif key == 'signal':
            self.signal_win.refresh()

    def pop_timeline(self):
        """the timeline and its controls in a window of their own (closing it brings them back)"""
        if self.timeline_win is not None:
            self.timeline_win.raise_()
            return
        i = self.left_layout.indexOf(self.timeline_box)
        self.left_layout.removeWidget(self.timeline_box)
        self.timeline_note = popout.placeholder('The timeline is in its own window.', self._dock_timeline)
        self.timeline_note.setMaximumHeight(48)
        self.left_layout.insertWidget(i, self.timeline_note)
        self.timeline_win = popout.PopWindow('timeline', 'Timeline', self.timeline_box, size=(1100, 170))
        self.timeline_win.closed.connect(lambda: self._dock_timeline(from_window=True))
        self.timeline_win.show()

    def _dock_timeline(self, from_window=False):
        win, self.timeline_win = self.timeline_win, None
        if win is None:
            return
        i = self.left_layout.indexOf(self.timeline_note)
        self.timeline_note.hide()
        self.left_layout.removeWidget(self.timeline_note)
        self.timeline_note.deleteLater()
        self.timeline_note = None
        self.left_layout.insertWidget(i, self.timeline_box)
        self.timeline_box.show()
        if not from_window:
            win.close()
        win.deleteLater()

    def _run_start(self):
        """start of the run being received now, or of the latest one"""
        if self.receiver.busy:
            return self.receiver.t_start
        starts = [c['start'] for c in self.data.captures] + [f['t'] for f in self.data.live_fixes]
        return max(c['start'] for c in self.data.captures) if self.data.captures else (min(starts) if starts else None)

    # ---- receiver
    def show_source(self):
        d = source_dialog.SourceDialog(self, receiving=self.receiver.busy)
        if d.exec_():
            self.rx_system.blockSignals(True)        # the system may have been changed there
            self.rx_system.setCurrentIndex(max(0, self.rx_system.findData(sdr_settings.load()['system'])))
            self.rx_system.blockSignals(False)
            self._rx_system_tip()
            if not self.receiver.busy:
                self.rx_status.setText(self._source_text())

    def _rx_system_changed(self, _):
        """the system picked beside the Start button: saved like Source's 'Receive', with an antenna reminder"""
        s = sdr_settings.load()
        s['system'] = self.rx_system.currentData()
        sdr_settings.save(s)
        sy = systems.get(s['system'])
        self._rx_system_tip()
        self.rx_status.setText(self._source_text())
        self.events.insertItem(0, '%s  Receiving %s next - antenna: %s' % (fmt_clock(time.time()), sy['label'], sy['antenna']))

    def _rx_system_tip(self):
        sy = systems.get(self.rx_system.currentData())
        self.rx_system.setToolTip('Which satellite system the receiver listens to (one at a time).\n'
                                  'Antenna for %s: %s.\nMore settings in Source.' % (sy['label'], sy['antenna']))

    def _source_text(self):
        s = sdr_settings.load()
        return 'Receiver off  \u00b7  %s on %s  \u00b7  %.4f MHz, %g MS/s' % (
            systems.get(s['system'])['label'], s['label'], sdr_settings.center_hz(s) / 1e6, sdr_settings.sample_rate(s) / 1e6)

    def toggle_receiver(self):
        if self.receiver.state == 'running':
            self.receiver.stop()
        elif catalog.HOME is None:
            self.start_pick()
            self.rx_status.setText('Set your location first - click it on the map')
        elif not self.receiver.busy:
            if not self.following:
                self.go_live()
            self.receiver.start()

    def _receiver_running(self, on):
        self.rx_btn.setText('■  Stop receiver' if on else '▶  Start receiver')
        color = '#d64550' if on else '#2e9e5b'
        theme.style(self.rx_btn, 'QPushButton { background: %s; border: none; color: white; font-weight: 700;'
                                 ' padding: 7px 14px; } QPushButton:hover { background: %s; }'
                                 ' QPushButton:disabled { background: %%(DISABLED_BG)s; color: %%(DISABLED_TEXT)s; }'
                                 % (color, QtGui.QColor(color).lighter(115).name()))
        self.rx_dot.setText('<span style="color:%s; font-size:16px">●</span>' % (ALERT if on else IDLE))
        self.rx_btn.setEnabled(True)
        self.rx_system.setEnabled(not on)        # switch systems while stopped

    def _receiver_status(self, text):
        self.rx_status.setText(text)
        busy = self.receiver.state in ('stopping', 'decoding')
        self.rx_btn.setEnabled(not busy)
        if busy:
            self.rx_btn.setText('Finishing...')
        if self.receiver.state != 'running' or text.startswith('Starting'):
            self.events.insertItem(0, '%s  %s' % (fmt_clock(time.time()), text))

    # ---- data
    def _show_data_size(self):
        self.data_btn.setText('\U0001F4BE  %s' % storage.fmt_bytes(storage.usage()['total']))
        self._settings_tip()

    def _settings_tip(self):
        if hasattr(self, 'settings_btn'):
            self.settings_btn.setToolTip('Settings\n  Location: %s\n  Data and storage: %s'
                                         % (self.loc_btn.text()[2:].strip(), self.data_btn.text()[2:].strip()))

    def _housekeeping(self):
        """apply the storage limits at startup (raw recordings past their time, map cache size)"""
        protect = {'output-%s' % self.receiver.stamp} if self.receiver.busy else set()
        n, freed = storage.enforce(data_dialog.catalogued(), protect=protect)
        t = storage.prune_tiles()
        if n or t:
            self.events.insertItem(0, '%s  Storage clean-up: %d old raw capture files (%s)%s removed' % (
                fmt_clock(time.time()), n, storage.fmt_bytes(freed), ', %d map pictures' % t if t else ''))
        self._show_data_size()

    def show_data(self):
        self.show_settings(1)

    def show_settings(self, page=0):
        """the Settings window: location (page 0), data and storage (page 1)"""
        protect = {'output-%s' % self.receiver.stamp} if self.receiver.busy else set()
        home = (HOME_LAT, HOME_LON, HOME_H) if catalog.HOME else None
        settings_dialog.SettingsDialog(self, home, protect, page).exec_()
        self._show_data_size()

    # ---- location
    def _show_location(self):
        if catalog.HOME is None:
            self.loc_btn.setText('\U0001F4CD  Set your location')
        else:
            self.loc_btn.setText('\U0001F4CD  %.4f, %.4f  \u00b7  %.0f m' % (HOME_LAT, HOME_LON, HOME_H * 1000))
        self._settings_tip()

    def start_pick(self):
        """the next click on the map sets the antenna location"""
        self.pick_cancel.setVisible(catalog.HOME is not None)
        fix = self.gnss_location()
        self.gps_loc_btn.setVisible(fix is not None)
        if fix:
            self.gps_loc_btn.setText('\U0001F6F0  Use GPS-tracked position')
            self.gps_loc_btn.setToolTip('%.5f, %.5f, %.0f m - the middle of your %d most recent GNSS position fixes\n'
                                        '(worked out from the GPS / Galileo satellites your antenna tracked)'
                                        % (fix[0], fix[1], fix[2] * 1000, fix[3]))
        self.pick_bar.show()
        self.map.set_picking(True)

    def cancel_pick(self):
        self.pick_bar.hide()
        self.map.set_picking(False)

    def gnss_location(self):
        """(lat, lon, height km, n) from your most recent GNSS position fixes (their median), or None"""
        fixes = sorted(self.data.gnss_fixes, key=lambda f: f['t'])[-20:]
        if not fixes:
            return None
        med = lambda k: float(np.median([f[k] for f in fixes]))     # noqa: E731
        return med('lat'), med('lon'), med('height_m') / 1000, len(fixes)

    def use_gnss_location(self):
        fix = self.gnss_location()
        if fix:
            self.cancel_pick()
            self.apply_location(fix[:3])
            self.events.insertItem(0, '%s  Location from your GNSS position fixes (%d of them)'
                                   % (fmt_clock(time.time()), fix[3]))

    def type_location(self):
        home = (HOME_LAT, HOME_LON, HOME_H) if catalog.HOME else None
        new = location_dialog.ask_location(home, first_run=home is None, parent=self)
        if new:
            self.cancel_pick()
            self.apply_location(new)

    def _picked(self, lat, lon):
        self.cancel_pick()
        self.apply_location((lat, lon, HOME_H if catalog.HOME else 0.0))
        # the ground height there, looked up in the background (Open-Meteo terrain model)
        self.height_job = location_dialog._Job(lambda: location_dialog.ground_height_m(lat, lon), self)
        self.height_job.done.connect(lambda h, at=(lat, lon): self._got_height(at, h))
        self.height_job.start()

    def _got_height(self, at, h):
        if isinstance(h, Exception):
            self.events.insertItem(0, '%s  Could not look up the height there (%s) - set it with '
                                      '"Type coordinates" if it matters' % (fmt_clock(time.time()), str(h)[:80]))
            return
        if (HOME_LAT, HOME_LON) != at:
            return          # picked somewhere else meanwhile
        set_home(HOME_LAT, HOME_LON, h / 1000)
        self._show_location()
        self.events.insertItem(0, '%s  Height at your location: %.0f m' % (fmt_clock(time.time()), h))
        self.data.fit_orbits()
        self.refresh()

    def apply_location(self, new):
        first = catalog.HOME is None
        old = (HOME_LAT, HOME_LON, HOME_H)
        set_home(*new)
        self._show_location()
        moved = 0.0 if first else location_dialog.distance_km(old, new)
        self.events.insertItem(0, '%s  Location set to %.5f, %.5f%s' % (
            fmt_clock(time.time()), new[0], new[1], '' if first else ' (moved %.1f km) - orbits refitted' % moved))
        self.data.fit_orbits()          # Doppler fits depend on where you are
        self.pass_cache = (None, 0, None)
        self.reload_lists()
        self.refresh()
        if moved > 2 and self.data.captures and not self.receiver.busy:
            ask = QtWidgets.QMessageBox.question(
                self, 'Re-analyze your captures?',
                'You moved the location by %.1f km. Satellites named from their Doppler curves were matched '
                'using the old location.\n\nRe-analyze all %d captures with the new location now? '
                '(takes a minute or two; the tracker keeps working)' % (moved, len(self.data.captures)))
            if ask == QtWidgets.QMessageBox.Yes:
                self.redo = QtCore.QProcess(self)
                self.redo.setWorkingDirectory(HERE)
                self.redo.finished.connect(lambda *_: (self.events.insertItem(
                    0, '%s  Captures re-analyzed with the new location' % fmt_clock(time.time())), self.poll()))
                self.redo.start(receiver.PY, [os.path.join(HERE, 'catalog.py'), '--redo'])
                self.events.insertItem(0, '%s  Re-analyzing all captures...' % fmt_clock(time.time()))

    def closeEvent(self, e):
        theme.save_ui(window=bytes(self.saveGeometry().toBase64()).decode())   # (dividers save as they are dragged)
        self.receiver.shutdown()
        self.deck.close_windows()           # popped-out views (they go back in, remembering where their windows were)
        self.spectrum.close_windows()
        if self.timeline_win is not None:
            self.timeline_win.close()
        if self.sat_info is not None:       # the satellite info window
            self.sat_info.close()
        super().closeEvent(e)

    # ---- lists
    def reload_lists(self):
        d = self.data
        self.timeline.captures = d.captures
        self.timeline.fix_times = [f['t'] for f in d.all_fixes()]
        self.replay_box.blockSignals(True)
        self.replay_box.clear()
        self.replay_box.addItem('choose a capture...')
        for c in reversed(d.captures):
            if c['end'] - c['start'] > 120:
                self.replay_box.addItem('%s  (%.1f h, %d fixes)' % (fmt_time(c['start']), (c['end'] - c['start']) / 3600, c['fixes']), c)
        self.replay_box.blockSignals(False)
        names = sorted((n for n in d.tracked + d.doppler_only if self.shows(n)),
                       key=lambda n: (systems.DISPLAY.index(systems.of_name(n)), int(re.sub(r'\D', '', n) or 0), n))
        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        cols = LIST_COLUMNS.get(self.view_key(), LIST_COLUMNS[None])
        for c, (label, tip) in enumerate(cols, 1):
            head = QtWidgets.QTableWidgetItem(label)
            head.setToolTip(tip)
            self.table.setHorizontalHeaderItem(c, head)
        self.table.setRowCount(len(names))
        for r, n in enumerate(names):
            i = d.info[n]
            mid = self._list_cells(n, i, self.view_key())
            vals = [n] + mid[:2] + [mid[2][0], short_time(i['last']) if i['last'] else '-', '']
            for c, v in enumerate(vals):
                item = NumItem(v, mid[2][1]) if c == COL_TRACKS else \
                    QtWidgets.QTableWidgetItem(systems.short_name(v) if c == 0 else v)
                if c == 0:
                    item.setData(Qt.UserRole, n)
                    item.setToolTip(n)
                self.table.setItem(r, c, item)
        self.table.setSortingEnabled(True)
        self.table.blockSignals(False)
        self._fill_summary(names)
        self.tle_note = 'Orbits: computed from your own observations only'

    def _sys_bar(self, height):
        """the Systems filter's buttons: the categories (None / All / Comms / Navigation / ISS & ham, with
        satellite counts), and under them the chosen category's systems to turn on and off - (layout, group)"""
        group = QtWidgets.QButtonGroup(self)
        group.setExclusive(True)
        box = QtWidgets.QVBoxLayout()
        box.setSpacing(4)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        box.addLayout(row)
        cat_colour = {'comms': systems.IRIDIUM['colour'], 'gnss': systems.GNSS['colour'], 'ham': systems.HAM['colour']}
        for i in range(self.sys_box.count()):
            key = self.sys_box.itemData(i)
            b = QtWidgets.QPushButton()
            b.setCheckable(True)
            b.setMinimumHeight(height)
            col = QtGui.QColor(*cat_colour[key]).name() if key in cat_colour else \
                ('#7a8494' if key == NO_SYSTEM else ACCENT)
            theme.style(b, 'QPushButton { padding: 2px 4px; font-size: 11px; border: 1px solid %%(BORDER)s;'
                           ' border-radius: 6px; background: %%(SURFACE)s; color: %%(TEXT)s; }'
                           ' QPushButton:checked { background: %s; border: 2px solid %s; color: white; font-weight: 700; }'
                           ' QPushButton:hover:!checked { border-color: %s; }'
                           % (QtGui.QColor(col).darker(180).name(), col, col))
            b.setToolTip('Show %s on the map, globe, sky plot and in the list'
                         % ('every satellite system' if key is None else 'no satellites' if key == NO_SYSTEM
                            else self.sys_box.itemText(i)))
            group.addButton(b, i)
            row.addWidget(b, 1)
        group.button(max(0, self.sys_box.currentIndex())).setChecked(True)
        group.idClicked.connect(self.sys_box.setCurrentIndex)
        self.sys_box.currentIndexChanged.connect(lambda i, g=group: g.button(i).setChecked(True))
        # the systems under each category: small on / off chips, shown for the chosen category
        for key, _, _, subs in CATEGORIES:
            if len(subs) < 2:
                continue
            chips = QtWidgets.QWidget()
            cl = QtWidgets.QHBoxLayout(chips)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(4)
            col = QtGui.QColor(*cat_colour[key]).name()
            for sub in subs:
                c = QtWidgets.QPushButton(SUB_LABELS[sub])
                c.setCheckable(True)
                c.setChecked(sub not in self.sub_off)
                c.setToolTip('Show or hide %s (within %s)' % (SUB_LABELS[sub], self.sys_box.itemText(
                    self.sys_box.findData(key))))
                theme.style(c, 'QPushButton { padding: 2px 10px; font-size: 10px; border: 1px solid %%(BORDER)s;'
                               ' border-radius: 10px; background: %%(SURFACE)s; color: %%(MUTED)s; }'
                               ' QPushButton:checked { background: %s; border: 1px solid %s; color: white; }'
                               % (QtGui.QColor(col).darker(200).name(), col))
                c.toggled.connect(lambda on, s=sub: self._set_sub(s, on))
                cl.addWidget(c)
                self.sub_chips.append((sub, c, chips))
            cl.addStretch(1)
            box.addWidget(chips)
            chips.setProperty('category', key)
        self.sys_box.currentIndexChanged.connect(lambda _: self._show_chips())
        self.sys_groups.append(group)
        self._show_chips()
        if self.sys_groups[1:]:             # a new window's buttons: give them their counts straight away
            self._label_sys_buttons()
        return box, group

    def _show_chips(self):
        """only the chosen category's system chips are shown"""
        want = self.sys_box.currentData()
        for _, _, row in self.sub_chips:
            row.setVisible(row.property('category') == want)

    def _set_sub(self, sub, on):
        """a system turned on or off under its category (every window's chip follows)"""
        (self.sub_off.discard if on else self.sub_off.add)(sub)
        for s, c, _ in self.sub_chips:
            if s == sub and c.isChecked() != on:
                c.blockSignals(True)
                c.setChecked(on)
                c.blockSignals(False)
        self.reload_lists()
        self.refresh()
        if self.sky is not None:
            self.sky.refresh()

    def _sys_window_bar(self):
        """the system buttons for the Sky / Globe windows, in a strip of their own"""
        row, _ = self._sys_bar(34)
        w = QtWidgets.QWidget()
        row.setContentsMargins(8, 6, 8, 0)
        w.setLayout(row)
        return w

    def _label_sys_buttons(self):
        """each system button: its name and how many of its satellites are listed"""
        listed = self.data.tracked + self.data.doppler_only
        subs = collections.Counter(sub_of(x) for x in listed)
        short = {key: s for key, _, s, _ in CATEGORIES}
        for i in range(self.sys_box.count()):
            key = self.sys_box.itemData(i)
            if key == NO_SYSTEM:
                for g in self.sys_groups:
                    g.button(i).setText('None\n\u2013')
                continue
            n = len(listed) if key is None else sum(v for s, v in subs.items() if SUB_CATEGORY.get(s) == key)
            for g in self.sys_groups:
                g.button(i).setText('%s\n%d' % ('All' if key is None else short[key], n))
        for sub, c, _ in self.sub_chips:
            c.setText('%s  %d' % (SUB_LABELS[sub], subs.get(sub, 0)))
            c.setMinimumWidth(c.fontMetrics().width(c.text()) + 28)     # (grow with the count)

    def _list_cells(self, n, i, want):
        """the three middle cells of a satellite's row: two texts and (text, number) for the numeric one"""
        d, sysk = self.data, i['system']
        passes = len([g for g in d.doppler.get(n, []) if len(g) >= 2])
        if sysk == 'iridium':
            ids = [', '.join('P%d S%02d' % ps for ps in sorted(i['slots'])) or '-', fmt_ids(i)]
            num = (str(i['fixes']), i['fixes'])
        elif sysk == 'orbcomm':
            ids = [fmt_ids(i), str(passes)]
            num = (str(i['fixes']), i['fixes'])
        elif sysk == 'ham':
            ids = [', '.join(i.get('callsigns', [])) or '-', str(passes)]
            num = (str(i.get('frames', 0)), i.get('frames', 0))
        else:       # gnss
            ids = ['GEO' if n.startswith('WAAS') else gnss_block(d.gnss_catalog_name(n)),
                   '✓' if n in d.orbits else '–']
            num = ('%.1f' % i['best'] if i['best'] else '-', i['best'])
        if want is not None:
            return ids + [num]
        # all systems: the system, its most telling ID, and what was received
        label = {'iridium': 'Iridium', 'orbcomm': 'Orbcomm', 'ham': 'ISS / ham'}.get(sysk) or n.split()[0]
        ident = {'iridium': ids[1] if ids[1] != '-' else ids[0], 'orbcomm': ids[0], 'ham': ids[0],
                 'gnss': ids[0]}[sysk]
        got = {'iridium': ('%d fixes' % i['fixes'], i['fixes']), 'orbcomm': ('%d fixes' % i['fixes'], i['fixes']),
               'ham': ('%d frames' % i.get('frames', 0), i.get('frames', 0)),
               'gnss': ('%d s' % i.get('frames', 0), i.get('frames', 0))}[sysk]
        return [label, ident, got]

    def _constellation(self, key):
        """how many satellites a system has, for 'heard of total': {label: (heard, total)}"""
        d = self.data
        heard = [n for n in d.tracked + d.doppler_only if systems.of_name(n)['key'] == key]
        if key == 'orbcomm':
            return {'Orbcomm': (len(heard), len(d.tles('orbcomm')))}
        if key == 'gnss':
            tl = d.tles('gnss')
            return {'GPS': (sum(1 for n in heard if n.startswith('GPS')), sum(1 for x in tl if x.name.startswith('GPS '))),
                    'Galileo': (sum(1 for n in heard if n.startswith('Galileo')),
                                sum(1 for x in tl if x.name.startswith('GSAT'))),
                    'GLONASS': (sum(1 for n in heard if n.startswith('GLONASS')),
                                sum(1 for x in tl if x.name.startswith('COSMOS'))),
                    'BeiDou': (sum(1 for n in heard if n.startswith('BeiDou')),
                               sum(1 for x in tl if x.name.startswith('BEIDOU')))}
        if key == 'ham':
            if self._ham_known is None:
                import satellites
                self._ham_known = len(glob.glob(os.path.join(os.path.dirname(satellites.__file__), 'satyaml', '*.yml')))
            return {'ham': (len(heard), self._ham_known)}
        return {}

    def _fill_summary(self, names):
        """the system buttons' counts, the number boxes and the note under them, for the systems shown"""
        d = self.data
        self._label_sys_buttons()
        want = self.view_key()
        if want == NO_SYSTEM:           # nothing shown: the boxes count every system
            names = d.tracked + d.doppler_only
        orbit = sum(1 for x in names if x in d.orbits)
        heard = len(names) - orbit
        fixes = [f for f in d.all_fixes() if want == NO_SYSTEM or self.shows(f['name'], f['cap'])]
        boxes = [(len(names), 'satellites listed', 'satellites you have received'),
                 (orbit, 'with our own orbit', 'on the map: an orbit worked out from your own receptions'),
                 (heard, 'heard, no orbit yet', 'in the list but not on the map yet (see each one for why)')]
        if want == 'iridium':
            boxes = [(orbit, 'with our own orbit', boxes[1][2]),
                     (heard, 'Doppler only', 'identified by their Doppler curve, no orbit yet'),
                     (len(fixes), 'position fixes', 'ring alerts that reported a satellite position'),
                     ('%d/66' % len(d.slots_heard), 'plane slots heard', 'of the 66 plane/slot positions in the constellation')]
        elif want == 'gnss':
            c = self._constellation('gnss')
            boxes = [(orbit, 'with broadcast orbit', 'their own orbit (ephemeris) decoded: on the map\n(%d GNSS '
                                                     'position fixes of your own so far)' % len(d.gnss_fixes)),
                     ]
            notes = {'Galileo': 'list (a few of them are not in service)'}
            # three boxes for the constellations: the ones you have heard most (GPS, Galileo, GLONASS first on a tie)
            for k in sorted(c, key=lambda k: -c[k][0])[:3]:
                boxes.append(('%d/%d' % c[k], '%s heard' % k, '%s satellites tracked, of the %d in the public %s'
                              % (k, c[k][1], notes.get(k, 'operational list'))))
        elif want == 'orbcomm':
            c = self._constellation('orbcomm')['Orbcomm']
            boxes = [('%d/%d' % c, 'satellites heard', 'Orbcomm satellites heard, of the %d in the public list '
                                                       '(old first-generation ones included)' % c[1]),
                     (orbit, 'with our own orbit', boxes[1][2]),
                     (heard, 'heard, no orbit yet', boxes[2][2]),
                     (len(fixes), 'reported positions', 'positions from their ephemeris packets')]
        elif want == 'ham':
            c = self._constellation('ham')['ham']
            boxes = [('%d/%d' % c, 'satellites heard', 'ISS and CubeSats identified by their decoded frames, of the '
                                                       '%d satellites gr-satellites can decode' % c[1]),
                     (orbit, 'orbit from Doppler', 'accepted Doppler-only orbits: on the map'),
                     (heard, 'waiting for passes', 'need passes on more than one orbit'),
                     (sum(d.info[x].get('frames', 0) for x in names), 'frames decoded', 'frames that named their satellite')]
        else:
            boxes.append((len(fixes), 'reported positions', 'positions the satellites reported (Iridium ring alerts, '
                                                          'Orbcomm ephemeris)'))
        for box, (v, cap, tip) in zip(self.stats, boxes):
            box.set(v, cap, tip)
        waiting = sorted(n for n, why in d.no_orbit.items() if 'direction' in why and self.shows(n))
        if want == NO_SYSTEM:
            self.summary.setText('<span style="color:%s">No satellites shown - pick a system above to see them '
                                 '(the boxes count every system).</span>' % MUTED)
            return
        self.summary.setText('<span style="color:%s">Every position and track comes only from your own receptions.%s</span>'
                             % (MUTED, ' Needs another fix: %s.' % ', '.join(waiting) if waiting else ''))

    def shows(self, name_or_system, cap=None):
        """is this satellite (or system key, or a fix from capture `cap`) shown by the Systems filter -
        its category chosen, and its system not turned off there?"""
        want = self.sys_box.currentData()
        if want is None:
            return True
        if want == NO_SYSTEM:
            return False
        if cap is not None and not name_or_system:
            band = systems.of_capture(cap)
            subs = (('glonass',) if band['key'] == 'glo1' else ('beidou',) if band['key'] == 'bds1'
                    else ('gps', 'galileo', 'waas')) \
                if band['system'] == 'gnss' else (band['system'],)
        elif name_or_system in systems.SYSTEMS:
            key = systems.SYSTEMS[name_or_system].get('system', name_or_system)
            subs = [s for s, c in SUB_CATEGORY.items() if c == key] if key == 'gnss' else [key]
        else:
            subs = [sub_of(name_or_system)]
        return any(SUB_CATEGORY.get(s) == want and s not in self.sub_off for s in subs)

    def view_key(self):
        """the system the list's columns and the number boxes are about: iridium / orbcomm / gnss / ham, None for
        a mix (All, or Comms with both systems on), or NO_SYSTEM"""
        want = self.sys_box.currentData()
        if want == 'comms':
            on = [s for s in ('iridium', 'orbcomm') if s not in self.sub_off]
            return on[0] if len(on) == 1 else None
        return want

    def _table_clicked(self, row, _col):
        self.select(self.table.item(row, 0).data(Qt.UserRole), from_table=True)
        self.show_sat_info(self.selected)

    def show_sat_info(self, name):
        """the pop-up with everything about a satellite (one window, switched to whichever was clicked)"""
        if not name:
            return
        if self.sat_info is None:
            self.sat_info = satinfo.SatInfoDialog(self)
        self.sat_info.show_satellite(name)
        self.sat_info.show()
        self.sat_info.raise_()

    def select(self, name, from_table=False):
        name = name or None             # (a click on empty space: nothing selected)
        self.selected = name
        if not from_table:
            self.table.blockSignals(True)
            for r in range(self.table.rowCount()):
                if self.table.item(r, 0).data(Qt.UserRole) == name:
                    self.table.scrollToItem(self.table.item(r, 0))
            self.table.blockSignals(False)
        self.pass_cache = (None, 0, None)
        self.refresh()

    # ---- time control
    def scrub(self, t):
        self.following = False
        self.live_btn.setChecked(False)
        self.t = t
        self.refresh()

    def toggle_play(self):
        self.playing = not self.playing
        if self.playing:
            self.following = False
            self.live_btn.setChecked(False)
        self.play_btn.setText('Pause' if self.playing else 'Play')

    def go_live(self):
        self.following, self.playing = True, False
        self.live_btn.setChecked(True)
        self.play_btn.setText('Play')
        self.t = time.time()
        self.timeline.show_range(self.t - 6 * 3600, self.t + 2 * 3600)
        self.refresh()

    def fit_history(self):
        d = self.data
        if d.captures:
            self.timeline.show_range(min(c['start'] for c in d.captures), max(time.time(), max(c['end'] for c in d.captures)))

    def replay(self, idx):
        c = self.replay_box.itemData(idx)
        if not c:
            return
        self.timeline.show_range(c['start'], c['end'])
        self.fix_box.setCurrentIndex(0)
        self.scrub(c['start'])
        self.speed_box.setCurrentIndex(3)
        if not self.playing:
            self.toggle_play()

    def tick(self):
        now = time.time()
        dt, self.last_tick = now - self.last_tick, now
        if self.following:
            self.t = now
        elif self.playing:
            self.t += dt * self.speed
        else:
            return
        self.refresh()

    def poll(self):
        before = (len(self.data.all_fixes()), len(self.data.info), self.data.cat_mtime, self.data.links_version)
        for ev in self.data.poll():
            self.events.insertItem(0, ev)
        self._check_new_satellites()
        queue, self.data.frame_queue = self.data.frame_queue, []
        if queue and not self.receiver.busy:
            # another program is receiving (e.g. the Satellite Console): its frames arrive here in batches,
            # so spread their flashes over the time until the next look
            queue = queue[-200:]
            for i, (kind, line) in enumerate(queue):
                QtCore.QTimer.singleShot(int(i * 1900 / len(queue)), lambda k=kind, l=line: self._frame(k, l))
        if (len(self.data.all_fixes()), len(self.data.info), self.data.cat_mtime, self.data.links_version) != before:
            self.reload_lists()
        d, now = self.data, time.time()
        # what the receiver hears, in the system's own terms: Iridium bursts, Orbcomm packets, ham frames,
        # and GNSS tracking results (PocketSDR, live)
        unit = {'iridium': 'burst', 'orbcomm': 'packet', 'ham': 'frame', 'gnss': 'tracking update'}.get(
            systems.of_capture(d.live_cap or '')['system'], 'signal')
        burst = ('last %s %s ago' % (unit, fmt_age(now - d.last_burst))) if d.last_burst else 'no %ss yet' % unit
        # another program receiving: by its marker file, not by how fresh the files are (a capture that has
        # just stopped looks fresh for a while)
        other = receiver.active_capture()
        writing = other is not None and other[1] != os.getpid()
        if self.receiver.state == 'running':        # this window's receiver: it knows its own state
            live = 'LIVE: receiving %s  (%s)' % (d.live_cap, burst)
        elif self.receiver.busy:
            live = 'Receiver finishing - adding the capture to the catalog'
        elif writing:
            live = 'LIVE: another receiver is writing %s  (%s)' % (other[0], burst)
        else:
            live = 'Receiver not running'
        if not self.receiver.busy:
            if writing:
                self.rx_status.setText('Another receiver is running (%s) - stop it (Satellite Console or another '
                                       'tracker window) before starting the receiver here' % other[0])
            elif not self.receiver.stamp or self.rx_status.text().startswith('Another receiver'):
                self.rx_status.setText(self._source_text())     # (after a run here, its result message stays up)
        self.status.setText('%s    |    %s    |    %s' % (live, self.tle_note, 'You: %.4f, %.4f' % (HOME_LAT, HOME_LON) if catalog.HOME else 'Location not set'))
        self.refresh()

    # ---- scene
    def refresh(self, *_):
        t = self.t
        d = self.data
        self.time_label.setText(fmt_time(t, True) + ('   LIVE' if self.following else ''))
        self.timeline.set_time(t, keep_visible=self.playing or self.following)
        sats = []
        for n in d.tracked:
            if not self.shows(n):
                continue
            orbit = d.orbits[n]
            sigma = float(orbit.sigma_km(t)[0])
            if sigma > MAX_SIGMA_KM:
                continue        # too long since we observed it to know where it is
            X, Y, Z = orbit.ecef(t).T
            lat, lon, alt = to_lla(X, Y, Z)
            el, az, rng = from_home(X, Y, Z)
            i = d.info[n]
            ident = ' &middot; '.join(filter(None, [', '.join('plane %d #%d' % ps for ps in sorted(i['slots'])),
                                                    ', '.join('net ID %03d' % x for x in sorted(i['net_ids']))]))
            sats.append({'name': n, 'system': i['system'], 'lat': float(lat[0]), 'lon': float(lon[0]), 'alt': float(alt[0]), 'el': float(el[0]),
                         'az': float(az[0]), 'rng': float(rng[0]), 'selected': n == self.selected, 'ident': ident,
                         'sigma': sigma, 'live': self.following and time.time() - d.live_heard.get(n, 0) < 60,
                         'heard_age': max(0.0, time.time() - d.live_heard[n]) if self.following and n in d.live_heard
                         else None, 'sig_q': self._sig_q(n)})
        by_name = {s['name']: s for s in sats}

        tmode = self.tracks_box.currentIndex()
        want = {s['name'] for s in sats if (tmode == 2) or (tmode == 0 and s['el'] > 0)}
        if self.selected and tmode != 3:
            want.add(self.selected)
        tracks = []
        for n in want:
            if n not in by_name:
                continue
            orbit = d.orbits[n]
            # one whole orbit of this satellite: half back, half ahead (Iridium ~100 min, GPS ~12 h), 201 points
            half = orbit.period_min * 30.0
            span = np.linspace(-half, half, 201)
            X, Y, Z = orbit.ecef(t + span).T
            lat, lon, _ = to_lla(X, Y, Z)
            lon = np.degrees(np.unwrap(np.radians(lon)))
            lon -= round((lon[100] - by_name[n]['lon']) / 360) * 360
            ok = orbit.sigma_km(t + span) <= MAX_SIGMA_KM
            pts = [pt if good else None for pt, good in zip(zip(lat.tolist(), lon.tolist()), ok)]
            # the globe shows the orbit in space as it is now: undo Earth's turn since t, so the ground
            # track (a weave for slow GPS orbits) becomes the orbit's own circle around the planet
            slon = lon + np.degrees(OMEGA_EARTH * span)
            ring = [pt if good else None for pt, good in zip(zip(lat.tolist(), slon.tolist()), ok)]
            tracks.append({'past': pts[:101], 'future': pts[100:], 'selected': n == self.selected,
                           'space_past': ring[:101], 'space_future': ring[100:],
                           'alt_km': by_name[n]['alt']})       # the globe draws the orbit at this height

        fmode = self.fp_box.currentIndex()
        footprints = []
        for s in sats:
            if fmode == 2:
                break
            if s['selected'] or (fmode == 1 and s['el'] > 0):
                footprints.append({'lat': s['lat'], 'lon': s['lon'], 'radius': footprint_radius(s['alt']), 'selected': s['selected']})

        xmode = self.fix_box.currentIndex()
        fixes = []
        for f in d.all_fixes():
            if xmode == 2:
                break
            if not self.shows(f['name'], f['cap']):
                continue
            if xmode == 0:
                age = t - f['t']
                if age < -30 or age > 3600:
                    continue
                alpha = 1.0 - 0.75 * max(0.0, age) / 3600
            else:
                alpha = 0.85
            fixes.append(dict(f, alpha=alpha))
        fix_lines = []
        groups = {}
        for f in sorted(fixes, key=lambda f: f['t']):
            groups.setdefault((f['name'] or f['net_id'], f['cap']), []).append(f)
        for g in groups.values():
            seg = [g[0]]
            for f in g[1:]:
                if f['t'] - seg[-1]['t'] < 300:
                    seg.append(f)
                else:
                    if len(seg) > 1:
                        fix_lines.append({'pts': [(x['lat'], x['lon']) for x in seg], 'alpha': seg[-1]['alpha']})
                    seg = [f]
            if len(seg) > 1:
                fix_lines.append({'pts': [(x['lat'], x['lon']) for x in seg], 'alpha': seg[-1]['alpha']})

        planes = []
        pmode = self.planes_box.currentIndex() if self.shows('iridium') else 2     # planes are Iridium's
        u = np.radians(np.arange(0, 361, 2.0))
        for pl in (d.planes(t) if pmode != 2 else []):
            sel = self.selected in pl['members']
            if pmode == 1 and not sel:
                continue
            cO, sO, ci, si = math.cos(pl['node']), math.sin(pl['node']), math.cos(pl['inc']), math.sin(pl['inc'])
            eci = ORBIT_RADIUS * np.stack([cO * np.cos(u) - sO * np.sin(u) * ci, sO * np.cos(u) + cO * np.sin(u) * ci,
                                           np.sin(u) * si], -1)
            lat, lon, _ = to_lla(*orbitfit.eci_to_ecef(eci, t).T)
            lon = np.degrees(np.unwrap(np.radians(lon)))
            planes.append(dict(pl, ring=list(zip(lat.tolist(), lon.tolist())), selected=sel))

        self.map.scene = {'sats': sats, 'planes': planes, 'tracks': tracks, 'footprints': footprints, 'fixes': fixes, 'fix_lines': fix_lines,
                          'sun': sun_position(t), 'night': self.night_chk.isChecked(), 't': t}
        self.map.update()
        if self.globe is not None and self.globe.isVisible():
            self.globe.view.set_scene(self.map.scene)
            self.globe_info.setText(self._globe_info_text())
        if self.sky is not None and self.sky.isVisible():
            self.sky.update_now()
        if self.signal_win is not None and self.signal_win.isVisible():
            self.signal_win.refresh()
        self._update_table(by_name)
        self._update_detail(by_name.get(self.selected))

    def _update_table(self, by_name):
        for r in range(self.table.rowCount()):
            n = self.table.item(r, 0).data(Qt.UserRole)
            s = by_name.get(n)
            item = self.table.item(r, COL_AT)
            if item is None:
                continue
            if s is None:
                item.setText('position unknown' if n in self.data.orbits else 'Doppler only, no orbit')
                item.setBackground(QtGui.QBrush())
                item.setForeground(QtGui.QColor(MUTED))
            else:
                item.setText(('%.0f° up' % s['el'] if s['el'] > 0 else 'below') + '  ' + fmt_sigma(s['sigma']))
                item.setBackground(QtGui.QColor(UP) if s['el'] > 0 else QtGui.QBrush())
                item.setForeground(QtGui.QColor(TEXT))
            # the selected satellite: its name highlighted blue and the row in bold; the green
            # "above your horizon" cell keeps its colour
            sel = n == self.selected
            for c in range(self.table.columnCount()):
                cell = self.table.item(r, c)
                if cell is None:
                    continue
                if c == 0:
                    cell.setBackground(C_SELECTED_NAME if sel else QtGui.QBrush())
                elif c != COL_AT:
                    cell.setBackground(QtGui.QBrush())
                font = cell.font()
                if font.bold() != sel:
                    font.setBold(sel)
                    cell.setFont(font)

    def next_pass(self, name):
        """(rise, set, max elevation) of the next pass above MIN_EL, from the current time"""
        key, t_at, res = self.pass_cache
        if key == name and abs(self.t - t_at) < 60:
            return res
        orbit = self.data.orbits[name]
        tt = self.t + np.arange(0, 36 * 3600, 15.0)
        tt = tt[orbit.sigma_km(tt) <= MAX_SIGMA_KM]     # only as far ahead as our orbit is good
        if not len(tt):
            self.pass_cache = (name, self.t, None)
            return None
        el = from_home(*orbit.ecef(tt).T)[0]
        up = el >= MIN_EL
        res = None
        if up.any():
            i = int(np.argmax(up))
            j = i + int(np.argmax(~up[i:])) if (~up[i:]).any() else len(tt) - 1
            res = (float(tt[i]), float(tt[j]), float(el[i:j + 1].max()), i == 0)
        self.pass_cache = (name, self.t, res)
        return res

    # ---- the selected satellite's details
    def _badge(self, text, fg, bg):
        return '<span style="background-color:%s; color:%s">&nbsp;%s&nbsp;</span>' % (bg, fg, text)

    def _card(self, name, i, s=None, extra=''):
        """the details' header, in the selection's colour: name, system, what it is, and badges for where it is
        and whether it is being heard"""
        sysd = systems.of_name(name)
        col = QtGui.QColor(*sysd['colour']).name()
        label = sysd['label'].split(' (')[0]
        if i.get('system') == 'gnss':
            cat = self.data.gnss_catalog_name(name)
            sub = cat or 'not matched to the public catalog yet'
        elif i.get('system') == 'iridium':
            sub = ', '.join('plane %d, slot %d' % ps for ps in sorted(i['slots'])) or 'plane / slot not heard yet'
        elif i.get('system') == 'orbcomm':
            sub = 'satellite ID %s' % fmt_ids(i, 'unknown')
        else:
            sub = 'callsign %s' % (', '.join(i.get('callsigns', [])) or 'not heard')
        badges = []
        if s is None:
            badges.append(self._badge('not on the map', TEXT, ROW_HEAD))
        elif s['el'] > 0:
            badges.append(self._badge('▲ %.0f° up' % s['el'], '#ffffff', '#1f8a4c'))
        else:
            badges.append(self._badge('▼ below your horizon', TEXT, ROW_HEAD))
        age = time.time() - self.data.live_heard.get(name, 0)
        got = self.data.signal_now(name)
        q = signal_quality(name, got[0]) if got else None
        ring = heard_colour(age, q) if self.following else None
        if ring is not None:
            unit = systems.of_name(name).get('signal', (0, 0, 'dB'))[2]
            badges.append(self._badge('\u25cf %s%s' % ('%.0f %s' % (got[0], unit) if got else 'heard',
                                                        '' if age <= 4 else ', %d s ago' % age),
                                      '#ffffff', strength_colour(0.5 if q is None else q).darker(140).name()))
        elif i.get('last'):
            badges.append(self._badge('last heard %s ago' % fmt_age(max(0, time.time() - i['last'])), TEXT, ROW_HEAD))
        badges.append(extra)
        return ('<table width="100%%" cellspacing="0" cellpadding="0" bgcolor="%s"><tr>'
                '<td width="5" bgcolor="%s"></td>'
                '<td><table cellspacing="0" cellpadding="7"><tr><td>'
                '<span style="font-size:17px; font-weight:700">%s</span> &nbsp;'
                '<span style="color:%s; font-weight:600">● %s</span><br>'
                '<span style="color:%s">%s</span><br>%s'
                '</td></tr></table></td></tr></table>'
                % (SEL_BG, C_SELECT.name(), name, col, label, MUTED, sub, ' '.join(b for b in badges if b)))

    def _section(self, title, rows=None, html=''):
        """a titled block: label / value rows, then any html"""
        out = ('<p style="margin-top:10px; margin-bottom:3px; color:%s; font-weight:700; font-size:11px">%s</p>'
               % (ACCENT, title.upper()))
        if rows:
            out += '<table cellspacing="0" cellpadding="2">%s</table>' % ''.join(
                '<tr><td style="color:%s; padding-right:10px" valign="top">%s</td><td>%s</td></tr>' % (MUTED, k, v)
                for k, v in rows if v is not None)
        return out + html

    def _where_rows(self, s, nxt):
        return [('At %s' % fmt_clock(self.t), 'lat %+.2f, lon %+.2f &middot; %.0f km up' % (s['lat'], s['lon'], s['alt'])),
                ('From you', ('<b>%.0f&deg; up</b>, azimuth %.0f&deg; &middot; %.0f km away' % (s['el'], s['az'], s['rng']))
                 if s['el'] > 0 else 'below your horizon (%.0f km away)' % s['rng']),
                ('Next pass', nxt)]

    def _update_detail(self, s):
        if not s:
            name = self.selected
            i = self.data.info.get(name) if name else None
            if not i:
                self.detail.setText('<span style="color:%s">Click a satellite on the map or in the list to see where '
                                    'it is, how you identified it, how good our orbit for it is, and its next pass '
                                    'over you.</span>' % MUTED)
                return
            nd = len(self.data.doppler.get(name, []))
            if name in self.data.orbits:
                o = self.data.orbits[name]
                html = (self._card(name, i) + self._section('Our orbit', [
                    ('Uncertainty', '&plusmn;%.0f km at %s - too uncertain to draw' % (o.sigma_km(self.t)[0], fmt_time(self.t))),
                    ('Last observed', fmt_time(o.info['last_obs']))],
                    '<span style="color:%s">Move the timeline closer to when you heard it, or catch another fix.</span>'
                    % MUTED) + '<br>' + self._fixes_html(name))
            elif i['system'] == 'gnss':
                why = self.data.no_orbit.get(name, '')
                html = (self._card(name, i, extra=self._badge('… no orbit yet', TEXT, ROW_HEAD)) +
                        self._gnss_live_html(i) + self._section('Tracking', [
                            ('Tracked', 'on its own ranging code, %d s in all' % i.get('frames', 0)),
                            ('Best signal', '%.1f dB-Hz' % i['best']), ('Doppler curves', str(nd)),
                            ('Last heard', fmt_time(i['last']) if i['last'] else '-')],
                            '<span style="color:%s">Not on the map: %s. That takes about 30 s of steady tracking at '
                            'roughly 35 dB-Hz or better - give the antenna more open sky.</span>'
                            % (MUTED, why or 'its broadcast orbit (ephemeris) has not been decoded yet')))
            elif 'frames' in i['how']:          # ISS / ham: heard, no orbit yet
                html = (self._card(name, i) + self._section('Heard', [
                    ('Identified by', 'its decoded frames (%d)' % i.get('frames', 0)),
                    ('Best signal', '%.0f dB' % i['best']), ('Doppler curves', str(nd)),
                    ('Last heard', fmt_time(i['last']) if i['last'] else '-')],
                    '<span style="color:%s">Not on the map: it doesn\'t send its position, so its orbit has to come '
                    'from its Doppler curves - that needs passes on more than one orbit.</span>' % MUTED))
            else:                               # identified by Doppler, but no orbit yet
                why = self.data.no_orbit.get(name, '')
                html = (self._card(name, i) + self._section('Heard', [
                    ('Identified by', 'its Doppler curve: the way its frequency shifted as it passed matched it'),
                    ('Best signal', '%.0f dB' % i['best']),
                    ('Last heard', fmt_time(i['last']) if i['last'] else '-')],
                    '<span style="color:%s">Not on the map: we have no orbit for it yet%s. A position fix from it '
                    '(or a longer Doppler curve) will put it there.</span>' % (MUTED, ' (%s)' % why if why else '')))
            self.detail.setText(html)
            return
        i = self.data.info[s['name']]
        o = self.data.orbits[s['name']]
        oi = o.info
        np_ = self.next_pass(s['name'])
        if np_ is None:
            nxt = 'not within the time our orbit is good for'
        else:
            spread = float(o.sigma_km(np_[0])[0]) / 7.0      # km of uncertainty -> seconds of timing
            plus = ' (&plusmn;%s)' % ('%.0f s' % spread if spread < 120 else '%.0f min' % (spread / 60))
            if np_[3]:
                nxt = '<b style="color:%s">in view now</b> - sets %s%s' % (GOOD, fmt_clock(np_[1]), plus)
            else:
                nxt = '%s to %s, up to %.0f&deg;%s' % (fmt_time(np_[0]), fmt_clock(np_[1]), np_[2], plus)
        if i['system'] == 'gnss':
            self._update_detail_gnss(s, i, o, nxt)
            return
        if oi.get('doppler_only'):      # ISS / ham: from Doppler curves alone
            orbit_rows = [('Built from', '%d Doppler curves alone (%d frames), no position fixes'
                           % (oi['doppler_groups'], oi['doppler_points'])),
                          ('Fit', '&plusmn;%.0f Hz &middot; period %.2f min' % (oi.get('doppler_rms_hz', 0), o.period_min))]
        else:
            orbit_rows = [('Built from', '%d position fix%s over %d pass%s + %d Doppler curve%s (%d frames)'
                           % (oi['fixes'], '' if oi['fixes'] == 1 else 'es', oi['passes'], '' if oi['passes'] == 1 else 'es',
                              oi['doppler_groups'], '' if oi['doppler_groups'] == 1 else 's', oi['doppler_points'])),
                          ('Fit', 'your fixes to &plusmn;%.1f km &middot; period %.2f min' % (oi['fix_rms_km'], o.period_min)),
                          ('Heading', 'from the measured orbital plane it lines up with')
                          if oi.get('direction_from_plane') else ('', None)]
        orbit_rows.append(('Uncertainty now', '<b>%s</b>' % fmt_sigma(s['sigma'])))
        heard_rows = [('Identified by', ' and '.join(sorted(i['how'])) or '-')]
        if i['system'] == 'ham':
            heard_rows.append(('Frames decoded', str(i.get('frames', 0))))
        else:
            heard_rows.append(('Position fixes', str(i['fixes'])))
        heard_rows += [('Best signal', '%.1f dB' % i['best']), ('Last heard', fmt_time(i['last']) if i['last'] else '-')]
        self.detail.setText(
            self._card(s['name'], i, s) +
            self._section('Where it is', self._where_rows(s, nxt)) +
            self._section('Heard by you', heard_rows) +
            self._section('Our orbit', orbit_rows) +
            ('<br>' + self._fixes_html(s['name']) if i['system'] != 'ham' else
             '<p style="color:%s">It sends no position: everything here comes from the Doppler shifts of its frames.</p>'
             % MUTED))

    def _sig_q(self, name):
        """the satellite's signal now on its system's scale (0..1), or None"""
        got = self.data.signal_now(name)
        return signal_quality(name, got[0]) if got else None

    def _gnss_live_html(self, i):
        """the satellite's signal right now (from the receiver, every second) - or when it was last tracked"""
        now = i.get('now')
        age = time.time() - now['t'] if now else None
        if not now or not self.following or age > 10:
            return self._section('Live signal', [('Now', '<span style="color:%s">not being tracked%s</span>'
                                                  % (MUTED, ' (last heard %s ago)' % fmt_age(age) if age is not None else ''))])
        level = max(0, min(10, int(round((now['cn0'] - 25) / 2))))      # 25 .. 45 dB-Hz
        bar = '<span style="color:%s">%s</span><span style="color:%s">%s</span>' % (
            GOOD if now['cn0'] >= 35 else ACCENT if now['cn0'] >= 30 else ALERT, '\u25ae' * level,
            MUTED, '\u25af' * (10 - level))
        rows = [('Signal', '<b>%.1f dB-Hz</b> %s<br><span style="color:%s">%s</span>'
                 % (now['cn0'], bar, MUTED, 'strong enough to read its orbit' if now['cn0'] >= 35 else
                    'weak: its orbit may not decode' if now['cn0'] >= 30 else 'very weak')),
                ('Doppler', '<b>%+.0f Hz</b> (%s)' % (now['dop'], 'coming closer' if now['dop'] > 0 else 'moving away')),
                ('Locked for', fmt_age(now['lock']))]
        sync = now.get('sync')
        if sync:
            # (bit sync and frame sync together = its data message is being read; the first flag stays 0)
            steps = [('bits', sync[1] != '-'), ('frames', sync[2] != '-')]
            ticks = ' '.join('<span style="color:%s">%s %s</span>' % (GOOD if ok else MUTED, '\u2714' if ok else '\u2026', n)
                             for n, ok in steps)
            reading = all(ok for _, ok in steps)
            rows += [('Data message', '%s &nbsp;%s' % (ticks, '<b style="color:%s">reading it</b>' % GOOD if reading else
                                                       '<span style="color:%s">lining up...</span>' % MUTED)),
                     ('Frames decoded', '<b>%d</b> &middot; errors %d &middot; lock lost %d\u00d7'
                      % (now['nav'], now['err'], now['lol']))]
        return self._section('Live signal \u00b7 every second', rows)

    def _update_detail_gnss(self, s, i, o, nxt):
        """details of a GPS / Galileo satellite: tracked on its code, placed by its own broadcast orbit"""
        oi = o.info
        health = oi.get('health', 0)
        self.detail.setText(
            self._card(s['name'], i, s, self._badge('\u2713 broadcast orbit', '#ffffff', '#2f6fb8')) +
            self._gnss_live_html(i) +
            self._section('Where it is', self._where_rows(s, nxt)) +
            self._section('Tracking', [
                ('Tracked', 'on its own ranging code, %d s in all' % i.get('frames', 0)),
                ('Best signal', '%.1f dB-Hz' % i['best']),
                ('Doppler curves', str(len(self.data.doppler.get(s['name'], [])))),
                ('Last heard', fmt_time(i['last']) if i['last'] else '-')]) +
            self._section('Its broadcast orbit', [
                ('Reference time', fmt_time(oi['toe'])), ('Received', fmt_time(oi['last_obs'])),
                ('Health', 'OK' if not health else '<b style="color:%s">marked unhealthy by the satellite</b>' % ALERT),
                ('Period', '%.2f min' % o.period_min), ('Uncertainty now', '<b>%s</b>' % fmt_sigma(s['sigma']))]))

    def _fixes_html(self, name):
        """the positions this satellite sent us (Iridium ring alerts, Orbcomm ephemeris), newest pass first"""
        fixes = sorted((f for f in self.data.all_fixes() if f['name'] == name), key=lambda f: f['t'])
        passes = []
        for f in fixes:
            if passes and f['t'] - passes[-1][-1]['t'] < 1200:
                passes[-1].append(f)
            else:
                passes.append([f])
        html = ('<div style="background:%s; padding:5px">' % ALERT_BG + '<span style="font-size:13px; color:%s">'
                '<b>%s positions received: %d</b></span> &nbsp;(%d pass%s)</div>'
                % (ALERT, 'Ephemeris' if systems.of_name(name) is systems.ORBCOMM else 'Ring-alert',
                   len(fixes), len(passes), '' if len(passes) == 1 else 'es'))
        html += ('<table cellspacing="0" cellpadding="2" width="100%%"><tr style="color:%s">' % MUTED +
                 '<td>Time</td><td align="right">Lat</td><td align="right">Lon</td>'
                 '<td align="right">Alt</td><td align="right">Signal</td></tr>')
        shown = 0
        for ps in reversed(passes):
            html += ('<tr><td colspan="5" style="background:%s"><b>%s</b> &middot; %d fix%s</td></tr>'
                     % (ROW_HEAD, fmt_time(ps[0]['t']), len(ps), '' if len(ps) == 1 else 'es'))
            for f in ps:
                if shown >= 20:
                    break
                html += ('<tr><td>%s%s</td><td align="right">%+.2f</td><td align="right">%+.2f</td>'
                         '<td align="right">%d km</td><td align="right">%.1f dB</td></tr>'
                         % (fmt_clock(f['t']), ' <b style="color:%s">LIVE</b>' % ALERT if f.get('live') else '',
                            f['lat'], f['lon'], f['alt_km'], f['snr']))
                shown += 1
        if shown < len(fixes):
            html += '<tr><td colspan="5" style="color:%s">... and %d older</td></tr>' % (MUTED, len(fixes) - shown)
        return html + '</table>'

    def center_selected(self):
        s = self.map.scene and next((s for s in self.map.scene['sats'] if s['selected']), None)
        if s:
            self.map.center_on(s['lon'], s['lat'])

    def jump_next_pass(self):
        if self.selected not in self.data.orbits:
            return
        res = self.next_pass(self.selected)
        if res and not res[3]:
            self.timeline.show_range(res[0] - 1200, res[1] + 1200)
            self.scrub(res[0])
            if self.playing:
                self.toggle_play()


def main():
    log = paths.ERROR_LOG

    def excepthook(et, ev, tb):
        text = ''.join(traceback.format_exception(et, ev, tb))
        open(log, 'a').write(time.ctime() + '\n' + text + '\n')
        sys.__stderr__ and sys.__stderr__.write(text)
    sys.excepthook = excepthook

    if sys.platform == 'win32':     # its own taskbar button and icon, not Python's
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('SatelliteTracker.App')
        except (AttributeError, OSError):
            pass
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName('Satellite Tracker')
    app.setWindowIcon(QtGui.QIcon(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.ico')))
    app.setStyle('Fusion')
    theme.set_theme(theme.load()[0])
    apply_app_colours(app)
    replay = [a[18:] for a in sys.argv if a.startswith('--test-receiver=')]   # testing without the SDR
    w = Tracker(replay[0] if replay else None)
    if '--start-receiver' in sys.argv:  # start receiving straight away (with the band chosen in Source)
        QtCore.QTimer.singleShot(2000, lambda: w.receiver.busy or w.toggle_receiver())
    if '--snapshot' in sys.argv:      # for testing: render a picture of the window and quit
        out = sys.argv[sys.argv.index('--snapshot') + 1]
        at = [a for a in sys.argv if a.startswith('--at=')]
        sel = [a for a in sys.argv if a.startswith('--select=')]
        if at:
            w.scrub(datetime.datetime.fromisoformat(at[0][5:]).timestamp())
            w.timeline.show_range(w.t - 3 * 3600, w.t + 3600)
        for a in sys.argv:                  # --theme=light / --map-theme=light (not saved)
            if a.startswith('--theme='):
                w.set_app_theme(a[8:], save=False)
            elif a.startswith('--map-theme='):
                w.map_theme_box.blockSignals(True)
                w.map_theme_box.setCurrentIndex(w.map_theme_box.findData(a[12:]))
                w.map_theme_box.blockSignals(False)
                w.set_map_theme(a[12:], save=False)
        want = [a[9:] for a in sys.argv if a.startswith('--system=')]
        if want:
            w.sys_box.setCurrentIndex(w.sys_box.findData(want[0]))

        def snap():
            w.refresh()
            w.grab().save(out)
            if sel and '--info' in sys.argv:       # and the satellite's info window, one picture per tab
                w.show_sat_info(sel[0][9:].replace('_', ' '))
                for k in range(w.sat_info.tabs.count()):
                    w.sat_info.tabs.setCurrentIndex(k)
                    w.sat_info.grab().save(out.replace('.png', '-info%d.png' % k))
            if '--windows' in sys.argv:            # and the Globe and Sky windows
                w.show_globe()
                w.show_sky()
                QtCore.QTimer.singleShot(4000, lambda: (w.refresh(), w.globe.grab().save(out.replace('.png', '-globe.png')),
                                                        w.sky.grab().save(out.replace('.png', '-sky.png')), app.quit()))
                return
            app.quit()
        if sel:
            w.select(sel[0][9:].replace('_', ' '))
        w.show()
        QtCore.QTimer.singleShot(9000, snap)
    else:
        w.showMaximized()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
