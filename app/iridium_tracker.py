"""Iridium Tracker - a map of the Iridium satellites your own antenna has heard.

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
import spectrum  # noqa: E402
import skyplot  # noqa: E402
import globe  # noqa: E402
import sound  # noqa: E402
import orbitfit  # noqa: E402  (orbits from our own observations)
import receiver  # noqa: E402  (the SDR receiver and decoder, run from here)
import location_dialog  # noqa: E402

UTC = datetime.timezone.utc
HOME_LAT, HOME_LON, HOME_H = catalog.HOME or (0.0, 0.0, 0.0)   # set_home() changes it while running
RE, F = 6378.137, 1 / 298.257223563
E2 = F * (2 - F)
MIN_EL = 8.2          # Iridium serves phones down to about 8 degrees elevation
MAX_SIGMA_KM = 1500   # hide a satellite once our orbit for it is more uncertain than this
FAINT_SIGMA_KM = 150  # draw it faded beyond this uncertainty
FIT_DAYS = 7          # orbits are fitted to each satellite's most recent week of observations
HEARTBEAT_S = 45      # a receiver's decoder log is written every 10 s; this long without a change = stopped
PLANE_MAX_SIGMA_KM = 300  # a satellite's orbit plane must be known this well to place its plane
PLANE_JOIN_DEG = 5.0      # a satellite without a slot number joins a plane whose node is this close
PLANE_SPACING_DEG = 31.6  # Iridium's planes, numbered 1-6 in order, are this far apart (design value)
LINK_EVERY_S = 30         # how often an unlinked plane/slot is tried against the satellites while receiving
ORBIT_RADIUS = 6378.137 + 781   # Iridium's design altitude, only used to draw the plane rings
PLANE_COLORS = {1: (77, 148, 255), 2: (176, 120, 240), 3: (40, 200, 200), 4: (240, 100, 180),
                5: (215, 150, 80), 6: (150, 200, 60), None: (150, 150, 150)}

# dark theme
BG, PANEL, SURFACE, BORDER = '#14171c', '#1b1f26', '#232832', '#2f3541'
TEXT, MUTED, ACCENT, ALERT, GOOD = '#e6e9ef', '#8b93a1', '#4c9aff', '#ff6b6b', '#4cd07d'
STYLE = """
QWidget { background: %(BG)s; color: %(TEXT)s; font-family: "Segoe UI"; font-size: 9pt; }
QPushButton { background: %(SURFACE)s; border: 1px solid %(BORDER)s; border-radius: 6px; padding: 5px 12px; }
QPushButton:hover { background: #2c323d; border-color: #3d4452; }
QPushButton:pressed { background: #353c49; }
QPushButton:checked { background: %(ACCENT)s; border-color: %(ACCENT)s; color: white; font-weight: 600; }
QComboBox { background: %(SURFACE)s; border: 1px solid %(BORDER)s; border-radius: 6px; padding: 4px 8px; }
QComboBox:hover { border-color: #3d4452; }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView { background: %(SURFACE)s; border: 1px solid %(BORDER)s;
                              selection-background-color: %(ACCENT)s; outline: none; }
QCheckBox { spacing: 6px; }
QTableWidget, QListWidget { background: %(PANEL)s; alternate-background-color: #1f242c;
                            border: 1px solid %(BORDER)s; border-radius: 6px; }
QTableWidget { selection-background-color: transparent; selection-color: %(TEXT)s; }
QHeaderView::section { background: %(SURFACE)s; color: %(MUTED)s; border: none;
                       border-bottom: 1px solid %(BORDER)s; padding: 5px 6px; font-weight: 600; }
QScrollArea { background: %(PANEL)s; border: 1px solid %(BORDER)s; border-radius: 6px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle { background: #3a4150; border-radius: 3px; min-height: 24px; min-width: 24px; }
QScrollBar::handle:hover { background: #4a5263; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }
QSplitter::handle { background: %(BG)s; }
QStatusBar { background: #101317; color: %(MUTED)s; }
QStatusBar QLabel { background: transparent; color: %(MUTED)s; }
QToolTip { background: %(SURFACE)s; color: %(TEXT)s; border: 1px solid %(BORDER)s; padding: 4px; }
""" % dict(BG=BG, PANEL=PANEL, SURFACE=SURFACE, BORDER=BORDER, TEXT=TEXT, MUTED=MUTED, ACCENT=ACCENT)
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
        self._tles = None
        self.live_read = {}     # live file -> how far we've read it
        self.live_cap = None    # the newest capture (the one the receiver writes while running)
        self.live_fixes, self.live_heard, self.live_events = [], {}, []
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
            if net_id in i['net_ids']:
                return n
        hit = catalog.match_position(self.tles(), datetime.datetime.fromtimestamp(t, UTC), lat, lon)
        return hit[0] if hit and hit[1] > 85 else None

    def tles(self):
        """the public satellite list - for names only"""
        if self._tles is None:
            try:
                self._tles = catalog.load_tles()
            except OSError:
                self._tles = []
        return self._tles

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
            names = set(self.info)
        if catalog.HOME is None:      # first run: nothing to fit until the antenna location is set
            return
        for n in names:
            self._fit_one(n)
        # a single fix can't tell whether the satellite was heading north or south; once the other
        # orbits are in, keep the direction that lines up with one of the planes we have measured
        for n in names:
            if 'direction' in self.no_orbit.get(n, ''):
                self._fit_one(n, lambda t, n=n: [p['node'] for p in self.planes(t) if n not in p['members']])

    def _fit_one(self, n, plane_nodes_at=None):
        fixes = [(f['t'], f['xyz_km']) for f in self.all_fixes() if f['name'] == n and f.get('xyz_km')]
        groups = list(self.doppler.get(n, []))
        by_cap = {}
        for f in self.all_fixes():   # the ring alerts' own frequencies are Doppler measurements too
            if f['name'] == n and f.get('hz'):
                by_cap.setdefault(f['cap'], []).append((f['t'], f['hz']))
        groups += list(by_cap.values())
        # only the latest week counts: older observations don't improve today's orbit, and this keeps
        # the fit quick however much history builds up
        if fixes:
            since = max(t for t, _ in fixes) - FIT_DAYS * 86400
            fixes = [fx for fx in fixes if fx[0] >= since]
            groups = [g for g in groups if g and max(t for t, _ in g) >= since]
        orbit, why = orbitfit.fit(fixes, groups, catalog.HOME, plane_nodes_at)
        self.orbits.pop(n, None)
        self.no_orbit.pop(n, None)
        if orbit:
            self.orbits[n] = orbit
        else:
            self.no_orbit[n] = why

    def load_catalog(self):
        path = catalog.DATA
        try:
            self.cat_mtime = os.path.getmtime(path)
            caps = json.load(open(path))
        except (OSError, ValueError):
            caps = {}
        self.captures, self.fixes = [], []
        self.doppler = {}       # name -> list of Doppler curves [(t, Hz), ...]
        self.info = {}          # name -> details
        self.slot_name = {}     # (plane, slot) -> name
        self.slots_heard = set()
        ts = lambda iso: datetime.datetime.fromisoformat(iso).timestamp()
        for cap_name, c in sorted(caps.items()):
            if not sum(c['counts'].values()):
                continue
            start = ts(c['start_utc'])
            self.captures.append({'name': cap_name, 'start': start, 'end': start + c['hours'] * 3600,
                                  'fixes': len(c['positions'])})
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
            self.info[name] = {'fixes': 0, 'net_ids': set(), 'slots': set(), 'last': 0, 'best': 0, 'how': set()}
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
        """satellites identified by their Doppler curve that we have no orbit for (listed, not drawn)"""
        return [n for n, i in self.info.items() if n not in self.orbits and 'Doppler' in i['how']]

    def all_fixes(self):
        return self.fixes + self.live_fixes

    def planes(self, t):
        """where each orbital plane is at time t, from our own orbits. A satellite joins the plane
        its ITL plane/slot number names; one without a slot number joins whichever plane its own
        orbit lines up with. Returns a list of {plane, node, inc, members, spread, sigma}."""
        sats = []
        for n, o in self.orbits.items():
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
        current = [f for f in glob.glob(os.path.join(paths.CAPTURES, 'output-*'))
                   if f.endswith(('.live', '.log', '.bits'))]
        if current:
            newest = max(current, key=os.path.getmtime)
            self.live_cap = os.path.basename(newest).rsplit('.', 1)[0]
            self.heartbeat = os.path.getmtime(newest)
            stem = os.path.join(paths.CAPTURES, self.live_cap)
            self.last_burst = max((os.path.getmtime(stem + e) for e in ('.live', '.bits')
                                   if os.path.exists(stem + e) and os.path.getsize(stem + e) > 200), default=0)
        # every capture the receiver wrote that hasn't been decoded into the catalog yet
        lives = sorted(glob.glob(os.path.join(paths.CAPTURES, 'output-*.live')), key=os.path.getmtime)
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
                events.append('%s  POSITION  %s  %+.2f, %+.2f  (%.0f dB)' % (fmt_clock(t), name or 'net ID %03d' % fx['net_id'], lat, lon, snr))
            pm = re.search(r' P(\d) S(\d+) ', line)
            if kind == 'ITL' and pm:
                ps = (int(pm.group(1)), int(pm.group(2)))
                name = self.slot_name.get(ps)
                if name:
                    self.live_heard[name] = t
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


# ---------------------------------------------------------------- map tiles
# invert, then turn the hues back around (like CSS "invert(1) hue-rotate(180deg)"): a dark map that keeps
# water blue and parks green; the last factor dims it a little
_DARK_MAP = np.array([[-0.574, 1.430, 0.144],
                      [0.426, 0.430, 0.144],
                      [0.426, 1.430, -0.856]]) * 0.82


def darken_tile(data):
    """OpenStreetMap tile bytes -> dark-themed QImage (safe to call off the GUI thread)"""
    img = QtGui.QImage.fromData(data)
    if img.isNull():
        return None
    img = img.convertToFormat(QtGui.QImage.Format_RGB32)
    w, h = img.width(), img.height()
    ptr = img.constBits()
    ptr.setsize(img.byteCount())
    bgra = np.frombuffer(ptr, np.uint8).reshape(h, img.bytesPerLine() // 4, 4)[:, :w]
    rgb = 255.0 - bgra[..., 2::-1].astype(np.float32)
    out = np.empty((h, w, 4), np.uint8)
    out[..., 2::-1] = np.clip(rgb @ _DARK_MAP.T + 8, 0, 255).astype(np.uint8)
    out[..., 3] = 255
    return QtGui.QImage(out.tobytes(), w, h, 4 * w, QtGui.QImage.Format_RGB32).copy()


def load_tile_rgb(z, x, y):
    """one darkened map tile as an RGB array (from the cache, downloading it if needed) - for the globe"""
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
    img = darken_tile(data)
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
        z, x, y = key
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
            img = darken_tile(data)
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


def draw_satellite(p, x, y, color, scale=1.0):
    p.save()
    p.translate(x, y)
    p.scale(scale, scale)
    p.setPen(QtGui.QPen(Qt.white, 1))
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
        self.setMinimumSize(600, 400)
        self.setMouseTracking(True)
        self.zoom = 2.3
        self.cx, self.cy = merc(-100, 25)
        self.tiles = {}
        self.loader = TileLoader()
        self.loader.loaded.connect(self._tile_loaded)
        self.scene = None
        self.hits = []
        self.drag = None
        self.picking = False
        self.legend_open = True     # the map key; click it to fold / unfold
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

    def _tile_loaded(self, key, img):
        self.tiles[key] = QtGui.QPixmap.fromImage(img)
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
        elif self.drag and not self.drag[3]:
            hit = self._hit(e.pos())
            if hit and hit[0]:
                self.satelliteClicked.emit(hit[0])
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
                tip = ('<b>Measured position</b> (decoded by your antenna)<br>%s<br>%s  net ID %03d  beam %d<br>'
                       'lat %+.2f  lon %+.2f  alt %d km<br>signal %.1f dB%s'
                       % (fmt_time(fx['t'], True), fx['name'] or 'unmatched', fx['net_id'], fx['beam'],
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
                col = QtGui.QColor(230, 50, 60) if s['selected'] else (QtGui.QColor(40, 190, 90) if s['el'] > 0 else QtGui.QColor(120, 126, 138))
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
                    if s['live']:
                        p.setPen(QtGui.QPen(QtGui.QColor(0, 200, 0), 2))
                        p.setBrush(Qt.NoBrush)
                        p.drawEllipse(QPointF(x + o, y), 15, 15)
                    draw_satellite(p, x + o, y, col, 1.25 if s['selected'] else 0.9)
                    if s['selected'] or s['el'] > 0:
                        p.setPen(QtGui.QColor(TEXT))
                        p.drawText(QPointF(x + o + 12, y - 8), s['name'].replace('IRIDIUM ', 'IR '))
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
                key = (z, i % n, j)
                rect = QtCore.QRectF(ox + i * ts, oy + j * ts, ts + 0.5, ts + 0.5)
                pm = self.tiles.get(key)
                if pm:
                    p.drawPixmap(rect, pm, QtCore.QRectF(pm.rect()))
                else:
                    self.loader.request(key)
                    # use the parent tile, scaled up, until this one arrives
                    parent = self.tiles.get((z - 1, (i % n) // 2, j // 2))
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
                      ', '.join(m.replace('IRIDIUM ', 'IR ') for m in pl['members']), pl['spread'], pl['sigma']))
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
        items = [('fix', 'Measured: position decoded by your antenna'),
                 ('past', 'Predicted track, past (our own orbit fit)'),
                 ('future', 'Predicted track, future'),
                 ('sat', 'Predicted position (faded + dotted ring = uncertain)'),
                 ('view', 'green = above your horizon now'),
                 ('plane', 'Orbital plane (our orbits), arrows = direction')]
        font = p.font()
        font.setPointSize(8)
        p.setFont(font)
        fm = p.fontMetrics()
        h = 18 * len(items) + 10
        open_ = self.legend_open and self.height() > h + 90      # folds itself on a short map
        if not open_:
            r = QtCore.QRectF(10, self.height() - 52, fm.width('Key \u25b8') + 20, 24)
            self.legend_rect = r
            p.setPen(QtGui.QPen(QtGui.QColor(60, 66, 78)))
            p.setBrush(QtGui.QColor(24, 28, 35, 225))
            p.drawRoundedRect(r, 8, 8)
            p.setPen(QtGui.QColor(TEXT))
            p.drawText(r, Qt.AlignCenter, 'Key \u25b8')
        else:
            width = max(fm.width(t) for _, t in items) + 58
            r = QtCore.QRectF(10, self.height() - h - 24, width, h)
            self.legend_rect = r
            p.setPen(QtGui.QPen(QtGui.QColor(60, 66, 78)))
            p.setBrush(QtGui.QColor(24, 28, 35, 225))
            p.drawRoundedRect(r, 8, 8)
            p.setPen(QtGui.QColor(MUTED))
            p.drawText(QtCore.QRectF(r.right() - 22, r.top() + 2, 18, 14), Qt.AlignCenter, '\u2715')
        for k, (kind, text) in enumerate(items if open_ else []):
            y = r.top() + 14 + 18 * k
            x = r.left() + 18
            if kind == 'fix':
                p.setPen(QtGui.QPen(Qt.white, 1.5))
                p.setBrush(C_FIX)
                p.drawEllipse(QPointF(x, y), 4.5, 4.5)
            elif kind in ('past', 'future'):
                p.setPen(QtGui.QPen(C_PAST if kind == 'past' else C_FUTURE, 3))
                p.drawLine(QPointF(x - 9, y), QPointF(x + 9, y))
            elif kind == 'sat':
                draw_satellite(p, x, y, QtGui.QColor(120, 126, 138), 0.8)
            elif kind == 'plane':
                p.setPen(QtGui.QPen(QtGui.QColor(*PLANE_COLORS[1]), 2.5))
                p.drawLine(QPointF(x - 9, y), QPointF(x + 9, y))
            else:
                draw_satellite(p, x, y, QtGui.QColor(40, 190, 90), 0.8)
            p.setPen(QtGui.QColor(TEXT))
            p.drawText(QPointF(x + 18, y + 4), text)
        p.setPen(QtGui.QColor(MUTED))
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
            p.setPen(QtGui.QPen(QtGui.QColor('#4a5263' if midnight else BORDER), 1))
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
                                  (QtGui.QColor(ALERT), 'position fix', 'tick'),
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
        self.setWindowTitle('Iridium Tracker')
        avail = QtWidgets.QApplication.primaryScreen().availableGeometry()
        self.resize(int(min(1700, avail.width() * 0.95)), int(min(1000, avail.height() * 0.92)))
        self.data = Data()
        self.t = time.time()
        self.playing, self.following = False, True
        self.speed = 1
        self.selected = None
        self.last_tick = time.time()
        self.pass_cache = (None, 0, None)

        self.map = MapView()
        self.map.satelliteClicked.connect(self.select)
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
        self.tracks_box = QtWidgets.QComboBox()
        self.tracks_box.addItems(['Tracks: selected + in view', 'Tracks: selected only', 'Tracks: all heard', 'Tracks: none'])
        self.fp_box = QtWidgets.QComboBox()
        self.fp_box.addItems(['Footprint: selected', 'Footprints: all in view', 'Footprints: none'])
        self.fix_box = QtWidgets.QComboBox()
        self.fix_box.addItems(['My fixes: last hour (replay)', 'My fixes: all', 'My fixes: none'])
        self.planes_box = QtWidgets.QComboBox()
        self.planes_box.addItems(['Planes: all', "Planes: selected satellite's", 'Planes: none'])
        self.night_chk = QtWidgets.QCheckBox('Day/night')
        self.night_chk.setChecked(True)
        for wdg in (self.tracks_box, self.fp_box, self.fix_box, self.planes_box, self.night_chk):
            opts.addWidget(wdg)
            sig = wdg.stateChanged if isinstance(wdg, QtWidgets.QCheckBox) else wdg.currentIndexChanged
            sig.connect(self.refresh)
        opts.addStretch(1)

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
        self.src_btn = QtWidgets.QPushButton('\u2699  Source')
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
        self.globe_btn = QtWidgets.QPushButton('\U0001F310  Globe')
        self.globe_btn.setToolTip('A 3D globe: the satellites, their orbital planes and your fixes around the Earth')
        self.globe_btn.clicked.connect(self.show_globe)
        self.globe = None
        self.rx_dot = QtWidgets.QLabel()
        self.rx_status = ElidedLabel('Receiver off')
        self.rx_status.setStyleSheet('color: %s;' % MUTED)
        title = QtWidgets.QLabel('IRIDIUM TRACKER')
        title.setStyleSheet('font-size: 13px; font-weight: 700; letter-spacing: 2px; color: %s;' % TEXT)
        header = QtWidgets.QFrame()
        header.setStyleSheet('QFrame#hdr { background: %s; border: 1px solid %s; border-radius: 8px; }'
                             ' QFrame#hdr QLabel { background: transparent; }' % (PANEL, BORDER))
        header.setObjectName('hdr')
        hl = QtWidgets.QHBoxLayout(header)
        hl.setContentsMargins(12, 6, 8, 6)
        hl.addWidget(title)
        hl.addSpacing(16)
        hl.addWidget(self.rx_btn)
        hl.addWidget(self.src_btn)
        hl.addWidget(self.spec_btn)
        hl.addWidget(self.sky_btn)
        hl.addWidget(self.globe_btn)
        hl.addSpacing(6)
        hl.addWidget(self.rx_dot)
        hl.addWidget(self.activity)
        hl.addWidget(self.rx_status, 1)
        self.loc_btn = QtWidgets.QPushButton()
        self.loc_btn.setToolTip('Change or detect your antenna location')
        self.loc_btn.clicked.connect(self.start_pick)
        hl.addWidget(self.loc_btn)
        self.data_btn = QtWidgets.QPushButton()
        self.data_btn.setToolTip('How much space your data uses, storage limits and clean-up')
        self.data_btn.clicked.connect(self.show_data)
        hl.addWidget(self.data_btn)
        self._show_location()
        self._receiver_running(False)

        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(4, 4, 4, 4)
        lv.addWidget(header)
        self.pick_bar = QtWidgets.QFrame()
        self.pick_bar.setObjectName('pick')
        self.pick_bar.setStyleSheet('QFrame#pick { background: #16324f; border: 1px solid %s; border-radius: 8px; }'
                                    ' QFrame#pick QLabel { background: transparent; }' % ACCENT)
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
        self.map_split = QtWidgets.QSplitter(Qt.Vertical)
        self.map_split.addWidget(self.map)
        self.map_split.addWidget(self.spectrum)
        self.map_split.setStretchFactor(0, 3)
        self.map_split.setStretchFactor(1, 2)
        self.map_split.setSizes([560, 330])
        self.spec_btn.toggled.connect(self.spectrum.setVisible)
        lv.addWidget(self.map_split, 1)
        lv.addLayout(opts)
        lv.addWidget(self.timeline)
        lv.addLayout(row)

        # side panel
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
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
        self.detail.setStyleSheet('background: %s; padding: 8px;' % PANEL)
        self.detail_scroll = QtWidgets.QScrollArea()     # the details can be longer than the space
        self.detail_scroll.setWidget(self.detail)
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setMinimumHeight(260)
        btns = QtWidgets.QHBoxLayout()
        b1 = QtWidgets.QPushButton('Center on map')
        b1.clicked.connect(self.center_selected)
        b2 = QtWidgets.QPushButton('Jump to next pass')
        b2.clicked.connect(self.jump_next_pass)
        b3 = QtWidgets.QPushButton('Clear')
        b3.clicked.connect(lambda: self.select(None))
        for b in (b1, b2, b3):
            btns.addWidget(b)
        self.events = QtWidgets.QListWidget()
        self.events.setMaximumHeight(170)

        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
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
        split.setSizes([max(600, self.width() - 520), 520])
        self.setCentralWidget(split)
        self.status = QtWidgets.QLabel()
        self.statusBar().addWidget(self.status, 1)

        self.reload_lists()
        self.go_live()
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
        ok = kind not in ('RAW', 'ERR')
        self.activity.flash(ok)
        if ok:      # (a damaged burst can't say which satellite sent it)
            name = self._satellite_of(line)
            if name:
                self.sat_lights.flash(name)

    def _satellite_of(self, line):
        """which satellite sent a frame: by plane/slot (time & location) or network ID (ring alerts, broadcasts)"""
        m = re.search(r' P(\d) S(\d+) ', line)
        if m:
            return self.data.slot_name.get((int(m.group(1)), int(m.group(2))))
        m = re.search(r'sat:(\d+)', line)
        if m:
            net = int(m.group(1))
            return next((n for n, i in self.data.info.items() if net in i['net_ids']), None)
        return None

    # ---- globe
    def show_globe(self):
        if self.globe is None:
            self.globe = globe.GlobeWindow(self, (HOME_LAT, HOME_LON), load_tile_rgb)
            self.globe.view.satelliteClicked.connect(self.select)
        self.globe.view.home = (HOME_LAT, HOME_LON)
        self.globe.view.set_scene(self.map.scene)
        self.globe.show()
        self.globe.raise_()

    # ---- sky plot
    def show_sky(self):
        if self.sky is None:
            self.sky = skyplot.SkyDialog(self, self.data, from_home, self._run_start)
        self.sky.show()
        self.sky.raise_()
        self.sky.refresh()

    def _run_start(self):
        """start of the run being received now, or of the latest one"""
        if self.receiver.busy:
            return self.receiver.t_start
        starts = [c['start'] for c in self.data.captures] + [f['t'] for f in self.data.live_fixes]
        return max(c['start'] for c in self.data.captures) if self.data.captures else (min(starts) if starts else None)

    # ---- receiver
    def show_source(self):
        d = source_dialog.SourceDialog(self, receiving=self.receiver.busy)
        if d.exec_() and not self.receiver.busy:
            self.rx_status.setText(self._source_text())

    def _source_text(self):
        s = sdr_settings.load()
        return 'Receiver off  \u00b7  %s  \u00b7  %.4f MHz, %g MS/s' % (
            s['label'], sdr_settings.center_hz(s) / 1e6, s['sample_rate'] / 1e6)

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
        self.rx_btn.setStyleSheet('QPushButton { background: %s; border: none; color: white; font-weight: 700;'
                                  ' padding: 7px 14px; } QPushButton:hover { background: %s; }'
                                  ' QPushButton:disabled { background: #3a4150; color: #9aa1ad; }'
                                  % (color, QtGui.QColor(color).lighter(115).name()))
        self.rx_dot.setText('<span style="color:%s; font-size:16px">●</span>' % (ALERT if on else '#4a5263'))
        self.rx_btn.setEnabled(True)

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
        protect = {'output-%s' % self.receiver.stamp} if self.receiver.busy else set()
        data_dialog.DataDialog(self, protect).exec_()
        self._show_data_size()

    # ---- location
    def _show_location(self):
        if catalog.HOME is None:
            self.loc_btn.setText('\U0001F4CD  Set your location')
        else:
            self.loc_btn.setText('\U0001F4CD  %.4f, %.4f  \u00b7  %.0f m' % (HOME_LAT, HOME_LON, HOME_H * 1000))

    def start_pick(self):
        """the next click on the map sets the antenna location"""
        self.pick_cancel.setVisible(catalog.HOME is not None)
        self.pick_bar.show()
        self.map.set_picking(True)

    def cancel_pick(self):
        self.pick_bar.hide()
        self.map.set_picking(False)

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
        self.receiver.shutdown()
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
        names = sorted(d.tracked + d.doppler_only, key=lambda n: int(re.sub(r'\D', '', n) or 0))
        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(names))
        for r, n in enumerate(names):
            i = d.info[n]
            vals = [n, ', '.join('P%d S%02d' % ps for ps in sorted(i['slots'])) or '-',
                    ', '.join('%03d' % x for x in sorted(i['net_ids'])) or '-', str(i['fixes']),
                    short_time(i['last']) if i['last'] else '-', '']
            for c, v in enumerate(vals):
                item = QtWidgets.QTableWidgetItem(v)
                if c == 3:
                    item.setData(Qt.DisplayRole, i['fixes'])
                self.table.setItem(r, c, item)
        self.table.setSortingEnabled(True)
        self.table.blockSignals(False)
        waiting = sorted(n for n, why in d.no_orbit.items() if 'direction' in why)
        self.summary.setText(
            '<b>%d satellites with our own orbit</b> &middot; %d identified by Doppler only &middot; '
            '%d position fixes &middot; %d of 66 plane/slots heard'
            '<br><span style="color:%s">Every position and track comes only from your decoded fixes and Doppler. '
            'Satellites are shown once they have sent a position fix%s; %d others were heard but have not sent one yet.</span>'
            % (len(d.tracked), len(d.doppler_only), len(d.all_fixes()), len(d.slots_heard), MUTED,
               ' (needs another fix: %s)' % ', '.join(waiting) if waiting else '',
               sum(1 for why in d.no_orbit.values() if 'no position' in why)))
        self.tle_note = 'Orbits: computed from your own observations only'

    def _table_clicked(self, row, _col):
        self.select(self.table.item(row, 0).text(), from_table=True)

    def select(self, name, from_table=False):
        self.selected = name
        if not from_table:
            self.table.blockSignals(True)
            for r in range(self.table.rowCount()):
                if self.table.item(r, 0).text() == name:
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
        queue, self.data.frame_queue = self.data.frame_queue, []
        if queue and not self.receiver.busy:
            # another program is receiving (e.g. the Iridium Console): its frames arrive here in batches,
            # so spread their flashes over the time until the next look
            queue = queue[-200:]
            for i, (kind, line) in enumerate(queue):
                QtCore.QTimer.singleShot(int(i * 1900 / len(queue)), lambda k=kind, l=line: self._frame(k, l))
        if (len(self.data.all_fixes()), len(self.data.info), self.data.cat_mtime, self.data.links_version) != before:
            self.reload_lists()
        d, now = self.data, time.time()
        burst = ('last burst %s ago' % fmt_age(now - d.last_burst)) if d.last_burst else 'no bursts yet'
        writing = now - d.heartbeat < HEARTBEAT_S
        if self.receiver.state == 'running':        # this window's receiver: it knows its own state
            live = 'LIVE: receiving %s  (%s)' % (d.live_cap, burst)
        elif self.receiver.busy:
            live = 'Receiver finishing - adding the capture to the catalog'
        elif writing:
            live = 'LIVE: another receiver is writing %s  (%s)' % (d.live_cap, burst)
        else:
            live = 'Receiver not running'
        if not self.receiver.busy:
            outside = writing and d.live_cap != 'output-%s' % self.receiver.stamp   # not our own last run
            if outside:
                self.rx_status.setText('Another receiver is running (%s) - stop it (Iridium Console or another '
                                       'tracker window) before starting the receiver here' % self.data.live_cap)
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
            sats.append({'name': n, 'lat': float(lat[0]), 'lon': float(lon[0]), 'alt': float(alt[0]), 'el': float(el[0]),
                         'az': float(az[0]), 'rng': float(rng[0]), 'selected': n == self.selected, 'ident': ident,
                         'sigma': sigma, 'live': self.following and time.time() - d.live_heard.get(n, 0) < 60})
        by_name = {s['name']: s for s in sats}

        tmode = self.tracks_box.currentIndex()
        want = {s['name'] for s in sats if (tmode == 2) or (tmode == 0 and s['el'] > 0)}
        if self.selected and tmode != 3:
            want.add(self.selected)
        tracks = []
        span = np.arange(-3000, 3001, 30.0)
        for n in want:
            if n not in by_name:
                continue
            orbit = d.orbits[n]
            X, Y, Z = orbit.ecef(t + span).T
            lat, lon, _ = to_lla(X, Y, Z)
            lon = np.degrees(np.unwrap(np.radians(lon)))
            lon -= round((lon[100] - by_name[n]['lon']) / 360) * 360
            ok = orbit.sigma_km(t + span) <= MAX_SIGMA_KM
            pts = [pt if good else None for pt, good in zip(zip(lat.tolist(), lon.tolist()), ok)]
            tracks.append({'past': pts[:101], 'future': pts[100:], 'selected': n == self.selected})

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
        pmode = self.planes_box.currentIndex()
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
                          'sun': sun_position(t), 'night': self.night_chk.isChecked()}
        self.map.update()
        if self.globe is not None and self.globe.isVisible():
            self.globe.view.set_scene(self.map.scene)
        self._update_table(by_name)
        self._update_detail(by_name.get(self.selected))

    def _update_table(self, by_name):
        for r in range(self.table.rowCount()):
            n = self.table.item(r, 0).text()
            s = by_name.get(n)
            item = self.table.item(r, 5)
            if item is None:
                continue
            if s is None:
                item.setText('position unknown' if n in self.data.orbits else 'Doppler only, no orbit')
                item.setBackground(QtGui.QBrush())
                item.setForeground(QtGui.QColor(MUTED))
            else:
                item.setText(('%.0f° up' % s['el'] if s['el'] > 0 else 'below') + '  ±%.0f km' % s['sigma'])
                item.setBackground(QtGui.QColor(28, 84, 52) if s['el'] > 0 else QtGui.QBrush())
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
                elif c != 5:
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

    def _update_detail(self, s):
        if not s:
            if self.selected in self.data.orbits:
                o = self.data.orbits[self.selected]
                self.detail.setText('<h3 style="margin:0">%s</h3>Our orbit for this satellite is too uncertain at %s '
                                    '(&plusmn;%.0f km), so it is not drawn. Last observed %s.<br>'
                                    'Move the timeline closer to when you heard it, or catch another fix.<hr>%s'
                                    % (self.selected, fmt_time(self.t), o.sigma_km(self.t)[0], fmt_time(o.info['last_obs']),
                                       self._fixes_html(self.selected)))
            elif self.selected in self.data.info:      # identified by Doppler, but no orbit yet
                i = self.data.info[self.selected]
                why = self.data.no_orbit.get(self.selected, '')
                self.detail.setText(
                    '<h3 style="margin:0">%s</h3>Identified by its Doppler curve: the way its frequency shifted '
                    'as it passed matched this satellite.<br>Slot: %s &middot; last heard %s &middot; best signal %.0f dB'
                    '<br><span style="color:%s">Not on the map: we have no orbit for it yet%s. A position fix from it '
                    '(or a longer Doppler curve) will put it there.</span>'
                    % (self.selected, ', '.join('P%d S%02d' % ps for ps in sorted(i['slots'])) or '-',
                       fmt_time(i['last']) if i['last'] else '-', i['best'], MUTED,
                       ' (%s)' % why if why else ''))
            else:
                self.detail.setText('Click a satellite on the map or in the list to see where it is, '
                                    'how you identified it, how good our orbit for it is, and its next pass over you.')
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
                nxt = '%s to %s, max %.0f&deg; up%s' % (fmt_time(np_[0]), fmt_clock(np_[1]), np_[2], plus)
        orbit_txt = ('%d position fix%s over %d pass%s + %d Doppler curve%s (%d frames)<br>'
                     '%s'
                     'fits your fixes to &plusmn;%.1f km &middot; period %.2f min<br>'
                     '<b>Uncertainty at this time:</b> &plusmn;%.0f km'
                     % (oi['fixes'], '' if oi['fixes'] == 1 else 'es', oi['passes'], '' if oi['passes'] == 1 else 'es',
                        oi['doppler_groups'], '' if oi['doppler_groups'] == 1 else 's', oi['doppler_points'],
                        'heading (north/south) from the measured orbital plane it lines up with<br>' if oi.get('direction_from_plane') else '',
                        oi['fix_rms_km'], o.period_min, s['sigma']))
        self.detail.setText(
            '<h3 style="margin:0">%s</h3>'
            '<b>Identified by:</b> %s<br>'
            '<b>Plane/slot:</b> %s &nbsp; <b>Network ID:</b> %s<br>'
            '<b>Your position fixes:</b> %d &nbsp; <b>Best signal:</b> %.1f dB<br>'
            '<b>Last heard:</b> %s<hr>'
            '%s<hr>'
            '<b>Our orbit:</b> %s<hr>'
            '<b>At %s</b> (predicted):<br>lat %+.2f, lon %+.2f, altitude %.0f km<br>'
            'From you: %s<br>'
            '<b>Next pass:</b> %s'
            % (s['name'], ' and '.join(sorted(i['how'])) or '-',
               ', '.join('plane %d #%d' % ps for ps in sorted(i['slots'])) or 'unknown',
               ', '.join('%03d' % x for x in sorted(i['net_ids'])) or 'unknown',
               i['fixes'], i['best'], fmt_time(i['last']) if i['last'] else '-', self._fixes_html(s['name']), orbit_txt,
               fmt_clock(self.t), s['lat'], s['lon'], s['alt'],
               ('elevation %.0f&deg;, azimuth %.0f&deg;, %.0f km away' % (s['el'], s['az'], s['rng'])) if s['el'] > 0
               else 'below your horizon (%.0f km away)' % s['rng'], nxt))

    def _fixes_html(self, name):
        """the ring-alert positions this satellite sent us, newest pass first"""
        fixes = sorted((f for f in self.data.all_fixes() if f['name'] == name), key=lambda f: f['t'])
        passes = []
        for f in fixes:
            if passes and f['t'] - passes[-1][-1]['t'] < 1200:
                passes[-1].append(f)
            else:
                passes.append([f])
        html = ('<div style="background:#3a1d22; padding:5px"><span style="font-size:13px; color:%s">'
                '<b>Ring-alert positions received: %d</b></span> &nbsp;(%d pass%s)</div>'
                % (ALERT, len(fixes), len(passes), '' if len(passes) == 1 else 'es'))
        html += ('<table cellspacing="0" cellpadding="2" width="100%%"><tr style="color:%s">' % MUTED +
                 '<td>Time</td><td align="right">Lat</td><td align="right">Lon</td>'
                 '<td align="right">Alt</td><td align="right">Signal</td></tr>')
        shown = 0
        for ps in reversed(passes):
            html += ('<tr><td colspan="5" style="background:#262b35"><b>%s</b> &middot; %d fix%s</td></tr>'
                     % (fmt_time(ps[0]['t']), len(ps), '' if len(ps) == 1 else 'es'))
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

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName('Iridium Tracker')
    app.setStyle('Fusion')
    pal = QtGui.QPalette()
    for role, color in ((QtGui.QPalette.Window, BG), (QtGui.QPalette.WindowText, TEXT), (QtGui.QPalette.Base, PANEL),
                        (QtGui.QPalette.AlternateBase, '#1f242c'), (QtGui.QPalette.Text, TEXT),
                        (QtGui.QPalette.Button, SURFACE), (QtGui.QPalette.ButtonText, TEXT),
                        (QtGui.QPalette.Highlight, ACCENT), (QtGui.QPalette.HighlightedText, '#ffffff'),
                        (QtGui.QPalette.ToolTipBase, SURFACE), (QtGui.QPalette.ToolTipText, TEXT)):
        pal.setColor(role, QtGui.QColor(color))
    app.setPalette(pal)
    app.setStyleSheet(STYLE)
    replay = [a[18:] for a in sys.argv if a.startswith('--test-receiver=')]   # testing without the SDR
    w = Tracker(replay[0] if replay else None)
    if '--snapshot' in sys.argv:      # for testing: render a picture of the window and quit
        out = sys.argv[sys.argv.index('--snapshot') + 1]
        at = [a for a in sys.argv if a.startswith('--at=')]
        sel = [a for a in sys.argv if a.startswith('--select=')]
        if at:
            w.scrub(datetime.datetime.fromisoformat(at[0][5:]).timestamp())
            w.timeline.show_range(w.t - 3 * 3600, w.t + 3600)
        if sel:
            w.select(sel[0][9:].replace('_', ' '))
        w.show()
        QtCore.QTimer.singleShot(9000, lambda: (w.refresh(), w.grab().save(out), app.quit()))
    else:
        w.showMaximized()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
