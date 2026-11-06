"""The Integrity section: is anyone jamming or spoofing satellite navigation here? It watches whatever GNSS band the
receiver is on (GPS / Galileo / WAAS, GLONASS, BeiDou) and checks what it hears against physics and against itself.

Jamming (someone drowning the signals): GNSS signals arrive under the noise, so with the gain fixed the band's
power IS the noise - a jammer raises it, or shows as a peak above the band's usual shape (gnss_rx's GNOISE lines,
against a baseline learned while the band was quiet). The satellites' signal-to-noise (C/N0) falls together and
they lose lock.
Spoofing (someone sending fake satellite signals): a spoofer has to fake every satellite from one transmitter on
the ground, and that shows -
  - the position fix leaves the antenna's known location, or moves during the session
  - GPS time jumps against this PC's own clock (steps of the PC clock itself are taken out)
  - the satellites' Doppler does not match what their own broadcast orbits say it must be from here (after the
    SDR's frequency error, which is the same for all of them)
  - a satellite is heard that its own orbit puts below the horizon
  - the signals are too strong, or all the same strength (real ones differ with elevation)
Each check has two levels (watch, alert); a level must hold a few seconds before it shows, and clears only after
it has been quiet a while. Changes go to the event list (kept in integrity-events.jsonl).

Monitor is the logic (no Qt; fed lines, asked for a report); IntegrityView is the section.
"""
import collections
import json
import math
import os
import time

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

import catalog
import paths
import theme

C = 299792458.0
CARRIER = {'G': 1575.42e6, 'E': 1575.42e6, 'J': 1575.42e6, 'S': 1575.42e6, 'C': 1561.098e6, 'R': 1602.0e6}
NA, OK, WATCH, ALERT = -1, 0, 1, 2
LEVEL_WORD = {NA: 'not checked', OK: 'clear', WATCH: 'watch', ALERT: 'ALERT'}
HISTORY_S = 1800        # the charts: last 30 minutes
RAISE_S, CLEAR_S = 3.0, 20.0     # a level must hold this long to show; and be gone this long to clear

# thresholds (watch, alert)
NOISE_RISE_DB = (3.0, 6.0)      # the band's power above its quiet baseline
PEAK_DB = (5.0, 12.0)           # a narrowband peak above the baseline's shape
CLIP_PCT = (0.5, 5.0)           # samples at the ADC's limits (overload)
CN0_DROP_DB = (4.0, 8.0)        # the satellites' C/N0 against their own last minute
# Position: measured here (home set from a GNSS fix), the one-minute median fix was within ~15 m of home with
# 5-6 satellites and ~25 m with 4 (36 m at worst); a home set only roughly needs Settings > location from a fix
POS_HOME_M = (250.0, 1500.0)    # fix from the set home location
POS_DRIFT_M = (150.0, 1000.0)   # fix moved during this session (the antenna does not move)
CLOCK_MS = (30.0, 200.0)        # GPS time minus PC time stepped against its last two minutes
# a satellite's Doppler this far from its orbit's (after the common offset): measured here, real GPS satellites
# stayed within 1-1.5 Hz rms (5 Hz at worst) of their own broadcast orbits over 20 minutes
DOPPLER_HZ = 50.0
CN0_HIGH = (55.0, 60.0)         # stronger than real satellites reach a ground antenna
CN0_SPREAD_DB = 1.0             # 5+ satellites within this of each other (and strong): one transmitter?
BELOW_EL = -5.0                 # heard, but its own orbit says it is this far below the horizon


def _xyz(p):
    return np.asarray(p, float).reshape(-1)[:3]


def enu(home, p):
    """(elevation deg, azimuth deg, range km) of Earth-fixed point p (km) from home (lat, lon, alt_km)"""
    lat, lon = math.radians(home[0]), math.radians(home[1])
    h = np.array(catalog.site(home[0], home[1], home[2]))
    d = _xyz(p) - h
    e = -math.sin(lon) * d[0] + math.cos(lon) * d[1]
    n = -math.sin(lat) * math.cos(lon) * d[0] - math.sin(lat) * math.sin(lon) * d[1] + math.cos(lat) * d[2]
    u = math.cos(lat) * math.cos(lon) * d[0] + math.cos(lat) * math.sin(lon) * d[1] + math.sin(lat) * d[2]
    r = float(np.linalg.norm(d))
    return math.degrees(math.asin(u / r)), (math.degrees(math.atan2(e, n)) + 360) % 360, r


def expected_doppler(orbit, t, home, carrier):
    """the Doppler (Hz) a satellite's own orbit gives at home: approaching = positive (as pocket_trk reports it)"""
    h = np.array(catalog.site(home[0], home[1], home[2]))
    p0, p1 = _xyz(orbit.ecef(t - 0.5)), _xyz(orbit.ecef(t + 0.5))
    p = (p0 + p1) / 2
    los = (p - h) / np.linalg.norm(p - h)
    rr = float(np.dot(p1 - p0, los)) * 1000.0         # range rate, m/s
    return -rr * carrier / C


def distance_m(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin((b - a) / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dl / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(min(1.0, x)))


class Check:
    """one check's level now, with hold-off: raised after RAISE_S, cleared after CLEAR_S"""

    def __init__(self, key, group, title, about):
        self.key, self.group, self.title, self.about = key, group, title, about
        self.level, self.value = NA, ''
        self.raw, self.since = NA, 0.0
        self.pending = None

    def update(self, now, raw, value):
        """the new raw level; returns the old level when the shown one changed (else None)"""
        self.value = value
        cur = self.level
        if raw == cur:
            self.pending = None
            return None
        if raw <= OK and cur <= OK:         # 'not checked' <-> 'clear': at once, no event
            self.level, self.pending = raw, None
            return None
        if self.pending is None or self.pending[0] != raw:
            self.pending = (raw, now)
        hold = RAISE_S if raw > cur else CLEAR_S
        if now - self.pending[1] >= hold:
            self.level, self.pending = raw, None
            return cur
        return None


CHECKS = [
    ('noise', 'jam', 'Noise in the band', 'The band\'s power against its quiet baseline. GNSS signals are under the '
     'noise, so a rise is extra noise: a broadband jammer (or a strong signal nearby). Needs a fixed gain.'),
    ('peak', 'jam', 'Narrowband interference', 'The strongest peak above the band\'s usual shape (its own spurs are '
     'part of the baseline): a carrier or swept jammer, or a new local interferer.'),
    ('clip', 'jam', 'Overload', 'Samples at the SDR\'s limits: something very strong - lower the gain, or a jammer close by.'),
    ('cn0', 'jam', 'Signal strength drop', 'The satellites\' C/N0 against their own last minute, all at once. With '
     'more noise: jamming. Without: something is blocking the antenna (or a spoofer is taking over).'),
    ('lock', 'jam', 'Satellites lost', 'Most satellites lost at once (not setting one by one).'),
    ('pos_home', 'spoof', 'Position vs home', 'The receiver\'s own position fix against the home location in '
     'Settings. A spoofer moves it (set home from a GNSS fix for a tight check).'),
    ('pos_drift', 'spoof', 'Position drift', 'How far the fix moved during this session - the antenna does not move; '
     'a spoofer can drag the position slowly.'),
    ('clock', 'spoof', 'Time jump', 'GPS time against this PC\'s clock (its own steps taken out): a spoofer can shift '
     'the time it reports.'),
    ('doppler', 'spoof', 'Doppler vs orbits', 'Each satellite\'s Doppler against what its own broadcast orbit says it '
     'must be from here (after the SDR\'s frequency error, common to all). One transmitter on the ground can\'t '
     'get this right for every satellite.'),
    ('horizon', 'spoof', 'Satellites below the horizon', 'Heard although its own orbit puts it below the horizon here.'),
    ('strength', 'spoof', 'Too strong / too even', 'Real satellites reach the ground weak (here ~30-50 dB-Hz) and differ '
     'with elevation; fake ones from one transmitter tend to be strong and all alike.'),
]


class Monitor:
    """the checks, fed the GNSS receiver's lines"""

    def __init__(self, events_path=None):
        self.events_path = events_path
        self.checks = [Check(*c) for c in CHECKS]
        self.by_key = {c.key: c for c in self.checks}
        self.events = collections.deque(maxlen=500)
        self._load_events()
        self.active = False
        self.band = ''
        self.auto_gain = False
        self.reset()

    # ---- session
    def reset(self):
        self.noise = None               # the last GNOISE values
        self.cn0 = {}                   # sat -> deque of (t, cn0)
        self.last = {}                  # sat -> (t, cn0, doppler)
        self.counts = collections.deque()   # (t, satellites locked)
        self.fixes = collections.deque(maxlen=600)
        self.first_fixes = []
        self.gtimes = collections.deque(maxlen=200)
        self.wall_mono0 = time.time() - time.monotonic()
        self.pc_step_t = -1e9
        self.series = {k: collections.deque() for k in ('rise', 'peak', 'cn0', 'nsat', 'pos', 'clock', 'dop')}
        self.sats = []
        self.bias = None
        self.t_start = time.time()
        for c in self.checks:
            c.level, c.value, c.pending = NA, '', None

    def start(self, band, label, auto_gain):
        self.reset()
        self.active, self.band, self.auto_gain = True, band, auto_gain
        self.event(time.time(), OK, 'Watching %s%s' % (label, ' (automatic gain: the noise checks are off)'
                                                       if auto_gain else ''))

    def stop(self):
        if self.active:
            self.event(time.time(), OK, 'Stopped watching (receiver stopped)')
        self.active = False
        for c in self.checks:
            c.level, c.pending = NA, None

    # ---- lines
    def line(self, kind, line):
        if not self.active:
            return
        f = line.split()
        try:
            t = float(f[1])
            if kind == 'GCH' and len(f) >= 6:
                sat, cn0, dop = f[2], float(f[4]), float(f[5])
                self.last[sat] = (t, cn0, dop)
                q = self.cn0.setdefault(sat, collections.deque())
                q.append((t, cn0))
                while q and q[0][0] < t - 120:
                    q.popleft()
            elif kind == 'GPOS' and len(f) >= 6:
                fx = (t, float(f[2]), float(f[3]), float(f[4]), int(f[5]))
                if fx[4] >= 4:
                    self.fixes.append(fx)
                    if len(self.first_fixes) < 60:
                        self.first_fixes.append(fx)
            elif kind == 'GNOISE' and len(f) >= 10:
                v = [float(x) for x in f[2:10]]
                self.noise = {'t': t, 'power': v[0], 'rise': v[1], 'clip': v[2], 'peak': v[3], 'peak_mhz': v[4],
                              'floor': v[5], 'learning': v[6] > 0}
            elif kind == 'GTIME' and len(f) >= 5:
                # corrected for steps of the PC clock since the session began (someone set it, or Windows did)
                step = (time.time() - time.monotonic()) - self.wall_mono0
                self.gtimes.append((t, float(f[2]) + step, float(f[4])))
        except (IndexError, ValueError):
            pass

    # ---- events
    def event(self, t, level, text):
        e = {'t': t, 'level': level, 'text': text}
        self.events.append(e)
        if self.events_path:
            try:
                with open(self.events_path, 'a') as f:
                    f.write(json.dumps(e) + '\n')
            except OSError:
                pass

    def _load_events(self):
        if not self.events_path or not os.path.exists(self.events_path):
            return
        try:
            for line in open(self.events_path).read().splitlines()[-500:]:
                try:
                    self.events.append(json.loads(line))
                except ValueError:
                    pass
        except OSError:
            pass

    def clear_events(self):
        self.events.clear()
        if self.events_path:
            try:
                os.remove(self.events_path)
            except OSError:
                pass

    # ---- the checks
    def level(self, group):
        lv = [c.level for c in self.checks if c.group == group]
        return max(lv) if lv else NA

    def evaluate(self, now, home, orbit_of):
        """run every check; home = (lat, lon, alt_km); orbit_of(sat) -> its broadcast orbit or None"""
        if not self.active:
            return
        raw = {}
        # the PC clock stepped (time.time against the monotonic clock): no time-jump verdicts for a while
        wm = time.time() - time.monotonic()
        if abs(wm - getattr(self, '_wm_last', wm)) > 0.02:
            self.pc_step_t = now
        self._wm_last = wm

        # -- noise
        n = self.noise if self.noise and now - self.noise['t'] < 5 else None
        if n is None:
            for k in ('noise', 'peak', 'clip'):
                raw[k] = (NA, 'no noise readings')
        elif n['learning']:
            for k in ('noise', 'peak'):
                raw[k] = (NA, 'learning the quiet band (%d s)' % 30)
            raw['clip'] = (self._grade(n['clip'], CLIP_PCT), '%.2f %% clipped' % n['clip'])
        else:
            if self.auto_gain:
                raw['noise'] = (NA, 'automatic gain: the level means nothing')
            else:
                raw['noise'] = (self._grade(n['rise'], NOISE_RISE_DB), '%+.1f dB (band %.1f dBFS)' % (n['rise'], n['power']))
            raw['peak'] = (self._grade(n['peak'], PEAK_DB), '%.1f dB at %.4f MHz' % (n['peak'], n['peak_mhz']))
            raw['clip'] = (self._grade(n['clip'], CLIP_PCT), '%.2f %% clipped' % n['clip'])
            self._push('rise', now, n['rise'])
            self._push('peak', now, n['peak'])
        noisy = bool(n and not n['learning'] and (n['rise'] >= 2.0 and not self.auto_gain or n['peak'] >= PEAK_DB[0]))

        # -- signals: C/N0 drop and lock loss
        locked = {s: v for s, v in self.last.items() if now - v[0] < 5}
        self.counts.append((now, len(locked)))
        while self.counts and self.counts[0][0] < now - 120:
            self.counts.popleft()
        drops = []
        for s in locked:
            q = self.cn0.get(s, ())
            old = [c for t, c in q if now - 70 <= t < now - 15]
            new = [c for t, c in q if t >= now - 5]
            if len(old) >= 20 and new:
                drops.append(float(np.median(old)) - float(np.median(new)))
        if len(drops) >= 3:
            d = float(np.median(drops))
            g = self._grade(d, CN0_DROP_DB)
            if g == ALERT and not noisy:
                g = WATCH           # weaker without more noise: blocked, not jammed
            raw['cn0'] = (g, '%+.1f dB over %d satellites%s' % (-d, len(drops), '' if noisy or d < CN0_DROP_DB[0]
                                                                 else ' (noise normal: blocked?)'))
        else:
            raw['cn0'] = (NA, 'needs 3 satellites tracked for a minute')
        before = [c for t, c in self.counts if now - 70 <= t < now - 15]
        if before and max(before) >= 4:
            was = float(np.median(before))
            lost = len(locked) <= was / 3
            raw['lock'] = ((ALERT if noisy else WATCH) if lost else OK, '%d locked (was %.0f)' % (len(locked), was))
        else:
            raw['lock'] = (NA, 'needs 4 satellites')
        if locked:
            cs = [v[1] for v in locked.values()]
            self._push('cn0', now, float(np.median(cs)))
        self._push('nsat', now, len(locked))

        # -- strength
        if len(locked) >= 3:
            cs = np.array([v[1] for v in locked.values()])
            g = self._grade(float(cs.max()), CN0_HIGH)
            even = len(cs) >= 5 and float(cs.std()) < CN0_SPREAD_DB and float(cs.mean()) > 40
            if even:
                g = max(g, WATCH) if g < WATCH else ALERT
            raw['strength'] = (g, 'max %.0f, spread %.1f dB-Hz' % (cs.max(), cs.std()))
        else:
            raw['strength'] = (NA, 'needs 3 satellites')

        # -- position
        recent = [fx for fx in self.fixes if now - fx[0] < 60]
        if len(recent) >= 3 and home:
            lat, lon = float(np.median([f[1] for f in recent])), float(np.median([f[2] for f in recent]))
            d = distance_m(home[0], home[1], lat, lon)
            raw['pos_home'] = (self._grade(d, POS_HOME_M), '%.0f m from home (%d satellites)' % (d, recent[-1][4]))
            self._push('pos', now, d)
            first = self.first_fixes
            if len(first) >= 10 and now - first[-1][0] > 0:
                flat, flon = float(np.median([f[1] for f in first])), float(np.median([f[2] for f in first]))
                dd = distance_m(flat, flon, lat, lon)
                raw['pos_drift'] = (self._grade(dd, POS_DRIFT_M), '%.0f m since the start' % dd)
            else:
                raw['pos_drift'] = (NA, 'needs the first fixes')
        else:
            raw['pos_home'] = raw['pos_drift'] = (NA, 'no position fix yet')

        # -- time
        g = [x for x in self.gtimes if now - x[0] < 30]
        ref = [x[1] for x in self.gtimes if now - 150 <= x[0] < now - 30]
        if g and len(ref) >= 3 and now - self.pc_step_t > 30:
            j = (g[-1][1] - float(np.median(ref))) * 1000
            raw['clock'] = (self._grade(abs(j), CLOCK_MS), '%+.0f ms against the last 2 min' % j)
            self._push('clock', now, j)
        elif now - self.pc_step_t <= 30:
            raw['clock'] = (NA, 'the PC clock was just set')
        else:
            raw['clock'] = (NA, 'needs a few minutes of GPS time')

        # -- geometry: Doppler and horizon from the satellites' own orbits
        rows, resid, below = [], {}, []
        for sat, (t, cn0, dop) in sorted(locked.items()):
            row = {'sat': sat, 'cn0': cn0, 'dop': dop, 'exp': None, 'el': None, 'res': None, 'flag': ''}
            o = orbit_of(sat) if home else None
            if o is not None:
                try:
                    el, az, rng = enu(home, o.ecef(t))
                    row['el'], row['az'] = el, az
                    if el < BELOW_EL:
                        below.append(sat)
                        row['flag'] = 'below horizon'
                    if sat[0] != 'S':       # (WAAS: its orbit message is too coarse for this)
                        row['exp'] = expected_doppler(o, t, home, CARRIER.get(sat[0], 1575.42e6))
                        resid[sat] = dop - row['exp']
                except Exception:
                    pass
            rows.append(row)
        if len(resid) >= 3:
            b = float(np.median(list(resid.values())))
            self.bias = b
            bad = []
            for row in rows:
                if row['sat'] in resid:
                    row['res'] = resid[row['sat']] - b
                    if abs(row['res']) > DOPPLER_HZ:
                        bad.append(row['sat'])
                        row['flag'] = (row['flag'] + ', ' if row['flag'] else '') + 'Doppler off'
            worst = max(abs(r['res']) for r in rows if r['res'] is not None)
            self._push('dop', now, worst)
            lv = OK if not bad else ALERT if len(bad) >= 2 and len(bad) >= len(resid) / 2 else WATCH
            raw['doppler'] = (lv, ('%d of %d off (%s)' % (len(bad), len(resid), ' '.join(bad)) if bad else
                                   'all %d match (worst %.0f Hz)' % (len(resid), worst)) +
                              ', SDR offset %+.0f Hz' % b)
        else:
            self.bias = None
            raw['doppler'] = (NA, 'needs 3 satellites with decoded orbits (%d)' % len(resid))
        with_orbit = sum(1 for r in rows if r['el'] is not None)
        if with_orbit:
            raw['horizon'] = (OK if not below else WATCH if len(below) == 1 else ALERT,
                              ('%s below the horizon' % ' '.join(below)) if below else 'all %d above it' % with_orbit)
        else:
            raw['horizon'] = (NA, 'needs decoded orbits')
        self.sats = rows

        for c in self.checks:
            lv, text = raw.get(c.key, (NA, ''))
            old = c.update(now, lv, text)
            if old is None:
                continue
            if c.level > max(old, OK):
                self.event(now, c.level, '%s: %s - %s' % (LEVEL_WORD[c.level].upper(), c.title, text))
            elif old >= WATCH:
                self.event(now, c.level, '%s back to %s (%s)' % (c.title, LEVEL_WORD[c.level], text))

    @staticmethod
    def _grade(v, lim):
        return ALERT if v >= lim[1] else WATCH if v >= lim[0] else OK

    def _push(self, key, t, v):
        q = self.series[key]
        q.append((t, v))
        while q and q[0][0] < t - HISTORY_S:
            q.popleft()

    def verdict(self, group):
        """(level, one line) for jamming / spoofing"""
        if not self.active:
            return NA, 'not watching - start a GNSS band on the receiver'
        cs = [c for c in self.checks if c.group == group]
        lv = max(c.level for c in cs)
        hot = [c.title.lower() for c in cs if c.level == lv and lv >= WATCH]
        if lv >= WATCH:
            return lv, ', '.join(hot)
        judged = sum(1 for c in cs if c.level != NA)
        if not judged:
            return NA, 'waiting for satellites'
        return OK, 'no sign (%d of %d checks running)' % (judged, len(cs))


# ---------------------------------------------------------------- the section
def level_colour(lv):
    return {ALERT: theme.PAL['ALERT'], WATCH: theme.PAL['WARN'], OK: theme.PAL['GOOD']}.get(lv, theme.PAL['MUTED'])


class StripChart(QtWidgets.QWidget):
    """a value over the last 30 minutes, with the watch / alert lines (instrument-dark, like the other plots)"""

    def __init__(self, title, unit, lines, limits=None, floor_range=1.0, absval=False):
        super().__init__()
        self.title, self.unit, self.lines, self.limits = title, unit, lines, limits
        self.floor_range, self.absval = floor_range, absval
        self.series = {}
        self.setMinimumHeight(90)

    def set_series(self, series):
        self.series = series
        self.update()

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QtGui.QColor('#0d1014'))
        L, R, T, B = 46, 8, 18, 16
        pw, ph = w - L - R, h - T - B
        now = time.time()
        vals = [v for key, _, _ in self.lines for _, v in self.series.get(key, ())]
        lims = list(self.limits or ())
        lo = min(vals + [0.0] + ([-lims[0]] if self.absval and lims else []))
        hi = max(vals + [self.floor_range] + lims[:1])
        if hi - lo < self.floor_range:
            hi = lo + self.floor_range
        pad = (hi - lo) * 0.08
        lo, hi = lo - pad, hi + pad

        def Y(v):
            return T + ph * (1 - (v - lo) / (hi - lo))

        def X(t):
            return L + pw * (1 - (now - t) / HISTORY_S)

        p.setPen(QtGui.QColor('#8b93a1'))
        f = p.font()
        f.setPointSizeF(8)
        p.setFont(f)
        p.drawText(L, 2, pw, 14, Qt.AlignLeft, self.title)
        for frac, txt in ((0, '-30 min'), (0.5, '-15'), (1, 'now')):
            p.drawText(int(L + pw * frac) - 30, h - 14, 60, 14, Qt.AlignCenter, txt)
        for v in (lo + pad, hi - pad):
            p.drawText(0, int(Y(v)) - 7, L - 4, 14, Qt.AlignRight | Qt.AlignVCenter, '%.3g %s' % (v, self.unit))
        p.setPen(QtGui.QPen(QtGui.QColor('#2f3541'), 1))
        p.drawRect(L, T, pw, ph)
        for k, (v, col) in enumerate(zip(lims, ('#ffb454', '#ff6b6b'))):
            for s in ((1, -1) if self.absval else (1,)):
                if lo < s * v < hi:
                    pen = QtGui.QPen(QtGui.QColor(col), 1, Qt.DashLine)
                    p.setPen(pen)
                    p.drawLine(L, int(Y(s * v)), L + pw, int(Y(s * v)))
        x = L + pw - 4
        for key, col, name in self.lines:
            pts = [(X(t), Y(v)) for t, v in self.series.get(key, ()) if now - t <= HISTORY_S]
            p.setPen(QtGui.QPen(QtGui.QColor(col), 1.5))
            path = QtGui.QPainterPath()
            for i, (px, py) in enumerate(pts):
                if i and px - pts[i - 1][0] > pw * 20 / HISTORY_S:      # a gap (receiver stopped)
                    path.moveTo(px, py)
                elif i:
                    path.lineTo(px, py)
                else:
                    path.moveTo(px, py)
            p.drawPath(path)
            fm = p.fontMetrics()
            x -= fm.horizontalAdvance(name)
            p.drawText(int(x), 2, fm.horizontalAdvance(name) + 2, 14, Qt.AlignLeft, name)
            x -= 10
        p.end()


class VerdictCard(QtWidgets.QFrame):
    def __init__(self, title):
        super().__init__()
        self.setObjectName('card')
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(14, 8, 14, 8)
        self.title = QtWidgets.QLabel(title)
        self.word = QtWidgets.QLabel()
        self.why = QtWidgets.QLabel()
        self.why.setWordWrap(True)
        self.why.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        f = self.word.font()
        f.setPointSizeF(f.pointSizeF() * 1.9)
        f.setBold(True)
        self.word.setFont(f)
        for w in (self.title, self.word, self.why):
            v.addWidget(w)

    def set(self, lv, why, active):
        col = level_colour(lv)
        self.word.setText({OK: 'Clear', WATCH: 'Watch', ALERT: 'ALERT'}.get(lv, 'Waiting' if active else 'Not watching'))
        self.why.setText(why)
        if getattr(self, '_col', None) != (col, theme.name):
            self._col = (col, theme.name)
            self.word.setStyleSheet('color: %s' % col)
            theme.style(self, 'QFrame#card { background: %%(PANEL)s; border: 2px solid %s; border-radius: 6px; }'
                        'QLabel { color: %%(MUTED)s; background: transparent; border: none; }' % col)


class IntegrityView(QtWidgets.QWidget):
    """the section: two verdicts, the checks, the charts, the satellites and the events"""
    changed = QtCore.pyqtSignal(int)        # the worst level now (for the section button)

    def __init__(self, tracker, orbit_of, home_of):
        super().__init__()
        self.tracker, self.orbit_of, self.home_of = tracker, orbit_of, home_of
        self.mon = Monitor(os.path.join(paths.DATA, 'integrity-events.jsonl'))
        self.n_events = -1
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        top = QtWidgets.QHBoxLayout()
        self.jam = VerdictCard('JAMMING')
        self.spoof = VerdictCard('SPOOFING')
        top.addWidget(self.jam, 1)
        top.addWidget(self.spoof, 1)
        outer.addLayout(top)

        self.checks = QtWidgets.QTreeWidget()
        self.checks.setHeaderLabels(['Check', 'Status', 'Now'])
        self.checks.setRootIsDecorated(False)
        self.checks.setUniformRowHeights(True)
        self.items = {}
        for grp, label in (('jam', 'Jamming'), ('spoof', 'Spoofing')):
            head = QtWidgets.QTreeWidgetItem(['%s checks' % label])
            f = head.font(0)
            f.setBold(True)
            head.setFont(0, f)
            head.setFlags(Qt.ItemIsEnabled)
            self.checks.addTopLevelItem(head)
            for c in self.mon.checks:
                if c.group == grp:
                    it = QtWidgets.QTreeWidgetItem(['   ' + c.title, '', ''])
                    it.setToolTip(0, c.about)
                    it.setToolTip(2, c.about)
                    self.checks.addTopLevelItem(it)
                    self.items[c.key] = it
        self.checks.setColumnWidth(0, 210)
        self.checks.setColumnWidth(1, 80)

        self.sat_table = QtWidgets.QTreeWidget()
        self.sat_table.setHeaderLabels(['Satellite', 'C/N0', 'Doppler Hz', 'From its orbit', 'Off by', 'Elevation', ''])
        self.sat_table.setRootIsDecorated(False)
        self.sat_table.setUniformRowHeights(True)
        self.sat_table.setToolTip('The satellites tracked now. "From its orbit": the Doppler its own broadcast orbit '
                                  'gives here, plus the SDR\'s frequency offset (common to all); "Off by": what is '
                                  'left. Real satellites stay within tens of Hz.')

        self.events = QtWidgets.QListWidget()
        self.events.setToolTip('What changed, newest first (kept in integrity-events.jsonl in the data folder)')

        self.charts = [
            StripChart('Noise above the quiet baseline', 'dB', [('rise', '#4c9aff', 'band power'),
                                                                ('peak', '#c792ea', 'narrowband peak')],
                       NOISE_RISE_DB, 2.0),
            StripChart('Satellites', '', [('cn0', '#4cd07d', 'median C/N0 dB-Hz'), ('nsat', '#e6e9ef', 'locked')],
                       None, 5.0),
            StripChart('Position from home', 'm', [('pos', '#4c9aff', 'distance')], POS_HOME_M[:1], 50.0),
            StripChart('GPS time vs the PC clock (step)', 'ms', [('clock', '#ffb454', 'step')], CLOCK_MS[:1], 10.0,
                       absval=True),
            StripChart('Worst Doppler mismatch', 'Hz', [('dop', '#c792ea', 'worst satellite')], (DOPPLER_HZ,), 50.0),
        ]
        chart_box = QtWidgets.QWidget()
        cv = QtWidgets.QVBoxLayout(chart_box)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(3)
        for c in self.charts:
            cv.addWidget(c, 1)

        left = QtWidgets.QSplitter(Qt.Vertical)
        left.addWidget(self.checks)
        sat_box = QtWidgets.QWidget()
        sv = QtWidgets.QVBoxLayout(sat_box)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.addWidget(self._head('SATELLITES NOW'))
        sv.addWidget(self.sat_table)
        left.addWidget(sat_box)
        ev_box = QtWidgets.QWidget()
        ev = QtWidgets.QVBoxLayout(ev_box)
        ev.setContentsMargins(0, 0, 0, 0)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self._head('EVENTS'))
        row.addStretch(1)
        self.relearn = QtWidgets.QPushButton('Relearn quiet noise')
        self.relearn.setToolTip('Forget the GNSS bands\' quiet-noise baselines: the next 30 s of receiving become the '
                                'new one. Do this when the band is known to be quiet (after changing the antenna or '
                                'the gains, it happens by itself).')
        self.relearn.clicked.connect(self._relearn)
        clear = QtWidgets.QPushButton('Clear events')
        clear.clicked.connect(self._clear)
        row.addWidget(self.relearn)
        row.addWidget(clear)
        ev.addLayout(row)
        ev.addWidget(self.events)
        left.addWidget(ev_box)
        split = QtWidgets.QSplitter()
        split.addWidget(left)
        split.addWidget(chart_box)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        outer.addWidget(split, 1)
        theme.remember_splitter(split, 'integrity', [700, 500])
        theme.remember_splitter(left, 'integrity_left', [300, 220, 160])

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.tick()

    def _head(self, text):
        lab = QtWidgets.QLabel(text)
        theme.style(lab, 'QLabel { color: %(MUTED)s; font-weight: 600; }')
        return lab

    # ---- the receiver
    def on_line(self, kind, line):
        if kind in ('GCH', 'GPOS', 'GNOISE', 'GTIME'):
            self.mon.line(kind, line)

    def on_running(self, on, system=None, auto_gain=False):
        if on and system is not None and system['key'] in ('gnss1', 'glo1', 'bds1'):
            self.mon.start(system['key'], system['label'], auto_gain)
        elif not on:
            self.mon.stop()
        self.tick()

    # ---- buttons
    def _relearn(self):
        gone = 0
        for k in ('gnss1', 'glo1', 'bds1'):
            try:
                os.remove(os.path.join(paths.DATA, 'noise-%s.npz' % k))
                gone += 1
            except OSError:
                pass
        busy = self.mon.active
        self.mon.event(time.time(), OK, 'Quiet-noise baselines forgotten (%d)%s' % (
            gone, ': the receiver relearns at its next start' if busy else ''))
        self.tick()

    def _clear(self):
        self.mon.clear_events()
        self.n_events = -1
        self.tick()

    # ---- once a second
    def tick(self):
        now = time.time()
        try:
            self.mon.evaluate(now, self.home_of(), self.orbit_of)
        except Exception as e:          # never take the tracker down
            print('integrity: %r' % e)
        jl, jw = self.mon.verdict('jam')
        sl, sw = self.mon.verdict('spoof')
        self.changed.emit(max(jl, sl))
        if not self.isVisible():
            return
        self.jam.set(jl, jw, self.mon.active)
        self.spoof.set(sl, sw, self.mon.active)
        for c in self.mon.checks:
            it = self.items[c.key]
            it.setText(1, LEVEL_WORD[c.level])
            it.setForeground(1, QtGui.QBrush(QtGui.QColor(level_colour(c.level))))
            it.setText(2, c.value)
        self.sat_table.clear()
        for r in self.mon.sats:
            exp = r['exp'] + self.mon.bias if r['exp'] is not None and self.mon.bias is not None else None
            it = QtWidgets.QTreeWidgetItem([
                catalog.gnss_name(r['sat']), '%.0f' % r['cn0'], '%+.0f' % r['dop'],
                '%+.0f' % exp if exp is not None else '-', '%+.0f' % r['res'] if r['res'] is not None else '-',
                '%.0f°' % r['el'] if r['el'] is not None else '-', r['flag']])
            if r['flag']:
                for col in range(7):
                    it.setForeground(col, QtGui.QBrush(QtGui.QColor(theme.PAL['WARN'])))
            self.sat_table.addTopLevelItem(it)
        for c in self.charts:
            c.set_series(self.mon.series)
        if len(self.mon.events) != self.n_events or (self.mon.events and self.events.count() == 0):
            self.n_events = len(self.mon.events)
            self.events.clear()
            for e in reversed(self.mon.events):
                it = QtWidgets.QListWidgetItem('%s  %s' % (time.strftime('%m-%d %H:%M:%S', time.localtime(e['t'])),
                                                           e['text']))
                if e['level'] >= WATCH:
                    it.setForeground(QtGui.QBrush(QtGui.QColor(level_colour(e['level']))))
                self.events.addItem(it)

    def showEvent(self, e):
        super().showEvent(e)
        self.tick()
