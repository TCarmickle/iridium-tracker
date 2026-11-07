r"""The Integrity section: is anyone jamming or spoofing satellite navigation here? It watches the GPS L1 band
(WATCH_BANDS: GPS / Galileo / WAAS; GLONASS and BeiDou are received but not watched - a steady spur near B1I kept
alerting) and checks what it hears against physics and against itself.

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
Not radio at all, but it looks like it: the SDR's USB sample stream breaks for 100-140 ms a few times a day. Every
satellite drops together (C/N0 "at loss" 12-20 dB-Hz) and most are back at their previous strength within 2-15 s.
gnss_rx reports such a gap (GGAP) and pocket_trk's own log (GLOG); the fingerprint is also recognised here from the
satellites alone. A signal drop that coincides is shown as "stream gap (SDR/USB) - not RF", at most a watch, and
gets no raw-sample recording (except the first of a session, as evidence).
Each check has two levels (watch, alert); a level must hold a few seconds before it shows, and clears only after
it has been quiet a while and the value has come back below the line by a margin (no flapping on a steady spur).
Changes go to the event list (kept in integrity-events.jsonl).
Each event also gets a snapshot folder (data\integrity\<time>-<check>\): every receiver line from 2 minutes before
to 90 s after it cleared (lines.txt), event.json (the receiver's settings, the checks and satellites at the start and
end, and a summary: how long, how the noise rose and fell, what kind of signal and where in the band, each
satellite's C/N0 before and at its lowest, position and time), waterfall.png (the band over the event), and -
unless switched off - the raw samples from 20 s before to 20 s after (samples.cs8, saved by gnss_rx).

Monitor is the logic (no Qt; fed lines, asked for a report); IntegrityView is the section.
"""
import collections
import json
import math
import os
import shutil
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
# Hysteresis: a level is raised the moment its threshold is reached, but dropped only once the value has stayed
# below the threshold by a margin for CLEAR_S. Without it a steady spur at 11.8-12.0 dB against the 12 dB alert
# line (BeiDou B1I, 2026-11-06) flipped watch / alert four times in six minutes. dB checks: 1 dB; the rest 20 %
HYST_DB = {'noise': 1.0, 'peak': 1.0, 'cn0': 1.0, 'strength': 1.0}
HYST_FRAC = 0.2
# Stream gaps (not RF): the SDR's USB stream loses 100-140 ms of samples a few times a day (rarely 1-3 ms). Every
# channel loses lock within half a second, C/N0 "at loss" 12-20 dB-Hz, and most satellites are re-found at their
# previous strength within 2-15 s (review 2026-11-06, four such "blocked antenna" events in one day). gnss_rx
# reports a gap it measured (GGAP) and pocket_trk's own log (GLOG LOST/FOUND); failing those the fingerprint is
# recognised here: most of the satellites reported LOST within a second below GAP_LOST_CN0, or most of the
# long-locked ones (over GAP_OLD_S) back with a lock age under GAP_RESET_S, all having vanished within 2 s
GAP_RECENT_S = 10.0     # a GGAP this recent classifies a signal drop or lock loss as a stream gap
GAP_HOLD_S = 60.0       # a gap keeps colouring the C/N0 and lock checks this long (they look 15-70 s back)
GAP_LOST_CN0 = 25.0
GAP_RESET_S, GAP_OLD_S = 3.0, 60.0
GAP_REACQ_S = 15.0      # one gap's re-acquisitions arrive staggered over about this long
GAP_IQ_WAIT_S = 8.0     # a C/N0 or lock event waits this long for a gap sign before asking for raw samples
                        # (gnss_rx keeps the 20 s before the request, so nothing is lost by waiting)

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
# Event snapshots: when a check rises to watch / alert, everything the receiver said from PRE_S before it until
# POST_S after the last check cleared (at most MAX_S) is saved in integrity\<time>-<check>\ with a summary; the
# GNSS receiver also saves the raw samples from 20 s before to 20 s after (samples.cs8, ~160 MB; the newest IQ_KEEP)
PRE_S, POST_S, MAX_S = 120.0, 90.0, 900.0
IQ_KEEP = 5
# Arming: nothing counts as an event until the connection has proven itself - ARM_SATS or more satellites at a
# median C/N0 of ARM_CN0 or better for ARM_S (here GPS runs 4-7 satellites at a median ~37-38 dB-Hz). If the signal
# then stays below that bar for DISARM_S with no extra noise, the watch disarms (a poor antenna, not an attack).
ARM_SATS, ARM_CN0, ARM_S, DISARM_S = 4, 33.0, 120.0, 300.0
SNAPS_PER_DAY = 20     # at most this many snapshots a day (beyond it events are still listed, not saved)
SNAPS_KEEP = 200        # the newest snapshot folders kept (each ~0.2-2 MB without its raw samples)
# The bands watched. GLONASS and BeiDou work too, but with this antenna they hear few satellites and a steady spur
# near BeiDou's B1I (1560.33 MHz) kept raising alerts - so GPS only for now (add 'glo1', 'bds1' to watch them)
WATCH_BANDS = ('gnss1',)
GNSS_BANDS = ('gnss1', 'glo1', 'bds1')


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


def _ms(ms):
    """a gap length for people: '110 ms', '1.5 ms', '0.25 ms'"""
    return '%.0f ms' % ms if ms >= 10 else '%.1f ms' % ms if ms >= 1 else '%.2f ms' % ms


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

    def update(self, now, raw, value, at_once=False):
        """the new raw level; returns the old level when the shown one changed (else None). at_once: a rise needs
        no hold-off (the stream-gap fingerprint is itself the proof, and is over before RAISE_S)"""
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
        if now - self.pending[1] >= hold or (at_once and raw > cur):
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

    def __init__(self, events_path=None, snap_dir=None):
        self.events_path, self.snap_dir = events_path, snap_dir
        self.on_open = None         # (stem) an event snapshot began: where its raw samples should go
        self.on_saved = None        # (folder) a snapshot was saved
        self.meta = {}
        self.label = ''
        self.idle_note = ''         # why nothing is watched (e.g. a band not covered)
        self.blocked = set()        # checks still up when their last snapshot ran out of time: no new one until clear
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
        self.ring = collections.deque()     # the last PRE_S of the receiver's lines: (t, kind, line)
        self.snap = None                    # the event snapshot being gathered
        self.armed = False                  # a proven good signal: only then can anything be raised (_arm)
        self.good_since = self.poor_since = None
        self.arm_note = ''
        self.t_start = time.time()
        # stream gaps (see the module notes): what gnss_rx and pocket_trk reported, and the fingerprint's inputs
        self.lock_s = {}                            # sat -> (t, lock seconds) from its last GCH line
        self.newest_t = None                        # time of the newest receiver line (its clock, for the checks)
        self.gaps = collections.deque(maxlen=500)   # (t, ms, source) GGAP lines this session
        self.log = collections.deque(maxlen=2000)   # (t, event, sat, values) GLOG lines
        self.restarts = []                          # GLOG RESTART times
        self.losts = collections.deque(maxlen=200)  # (t, sat, cn0) GLOG LOST
        self.resets = collections.deque(maxlen=200)  # (t, sat, t_lost): a young lock after a long one
        self.gap_t, self.gap_why = None, ''         # when a stream gap was last seen or recognised, and how
        self.gap_count, self.gap_last = 0, None     # this session; (t, ms or None, source / how)
        self.gap_iq_kept = False                    # raw samples of one gap event kept already (the evidence)
        self._gap_event_t = -1e9
        for c in self.checks:
            c.level, c.value, c.pending = NA, '', None

    def start(self, band, label, auto_gain, meta=None):
        self.reset()
        self.active, self.band, self.auto_gain, self.label = True, band, auto_gain, label
        self.meta = dict(meta or {})
        self.event(time.time(), OK, 'Watching %s%s' % (label, ' (automatic gain: the noise checks are off)'
                                                       if auto_gain else ''))

    def stop(self):
        if self.snap:
            self._close(time.time(), 'the receiver stopped')
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
            rec = (t, kind, line.strip())
            self.ring.append(rec)
            while self.ring and self.ring[0][0] < t - PRE_S:
                self.ring.popleft()
            if self.snap:
                self.snap['lines'].append(rec)
            if kind in ('GCH', 'GNOISE', 'GPOS', 'GSPP', 'GTIME'):      # (the receiver's clock: see evaluate)
                self.newest_t = t if self.newest_t is None else max(self.newest_t, t)
            if kind == 'GCH' and len(f) >= 6:
                sat, cn0, dop = f[2], float(f[4]), float(f[5])
                self.last[sat] = (t, cn0, dop)
                q = self.cn0.setdefault(sat, collections.deque())
                q.append((t, cn0))
                while q and q[0][0] < t - 120:
                    q.popleft()
                if len(f) >= 7:
                    self._lock_age(t, sat, float(f[6]))
            elif kind == 'GGAP' and len(f) >= 3:
                self._gap(t, float(f[2]), f[3] if len(f) > 3 else 'usb')
            elif kind == 'GLOG' and len(f) >= 3:
                ev = f[2].upper()
                sat = f[3] if len(f) > 3 and ev in ('FOUND', 'LOST', 'FALSELOCK') else ''
                self.log.append((t, ev, sat, f[4:] if sat else f[3:]))
                if ev == 'RESTART':
                    self.restarts.append(t)
                elif ev == 'LOST':
                    self.losts.append((t, sat, float(f[4]) if len(f) > 4 else 0.0))
                    self._lost_burst(t)
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

    # ---- stream gaps: reported by gnss_rx / pocket_trk, or recognised from the satellites
    def _gap(self, t, ms, source):
        """gnss_rx saw the sample stream break (GGAP): count it and let the checks know"""
        self.gaps.append((t, ms, source))
        self.gap_count += 1
        self.gap_last = (t, ms, source)
        self._mark_gap(t, source, ms=ms)

    def _lock_age(self, t, sat, lock):
        """a satellite back with a young lock after a long one (and a short absence): one re-acquisition"""
        prev = self.lock_s.get(sat)
        self.lock_s[sat] = (t, lock)
        if prev and lock < GAP_RESET_S and prev[1] > GAP_OLD_S and t - prev[0] < 30:
            self.resets.append((t, sat, prev[0]))
            self._reset_burst(t)

    def _reset_burst(self, t):
        """the re-acquired fingerprint: within GAP_REACQ_S most of the long-locked satellites are back with young
        locks, and they all vanished within the same second or two (setting satellites go one by one; a blocked
        antenna does not bring them back)"""
        recent = [r for r in self.resets if t - r[0] <= GAP_REACQ_S]
        back = {}
        for _, sat, t_lost in recent:
            back[sat] = max(back.get(sat, 0.0), t_lost)
        if len(back) < 2 or max(back.values()) - min(back.values()) > 2.0:
            return
        old = {s for s, (tt, lk) in self.lock_s.items() if t - tt < 30 and lk > GAP_OLD_S} | set(back)
        if len(back) >= len(old) / 2:
            self._mark_gap(t, 're-acquired together', n=len(back), of=len(old))

    def _lost_burst(self, t):
        """pocket_trk reported most of the satellites lost within a second, at the weak C/N0 of a stream gap"""
        burst = {s for tt, s, c in self.losts if t - tt <= 1.5 and c < GAP_LOST_CN0}
        heard = {s for s, v in self.last.items() if t - v[0] < 10} | burst
        if len(burst) >= 2 and len(burst) >= len(heard) / 2:
            self._mark_gap(t, 'lost together', n=len(burst), of=len(heard))

    def _mark_gap(self, t, why, ms=None, n=0, of=0):
        fresh = self.gap_t is None or t - self.gap_t > 30
        self.gap_t, self.gap_why = t, why
        if ms is None and fresh:        # recognised from the satellites alone: counts as a gap too
            self.gap_count += 1
            self.gap_last = (t, None, why)
        if fresh and t - self._gap_event_t > 30:
            self._gap_event_t = t
            if ms is not None:
                what = 'the SDR lost %s of samples (%s)' % (_ms(ms), why)
            else:
                what = '%d of %d satellites %s (SDR/USB)' % (n, of, why)
            self.event(t, OK, 'Stream gap: %s - not an RF event (%d this session)' % (what, self.gap_count))

    def gap_recent(self, now, within=GAP_HOLD_S):
        return self.gap_t is not None and -5.0 <= now - self.gap_t < within

    def gap_note(self):
        """for the verdict card: '3 stream gaps this session, last 110 ms at 12:20'"""
        if not self.gap_count or not self.gap_last:
            return ''
        t, ms, why = self.gap_last
        size = _ms(ms) if ms is not None else 'recognised from the satellites'
        return '%d stream gap%s this session, last %s at %s' % (
            self.gap_count, '' if self.gap_count == 1 else 's', size, time.strftime('%H:%M', time.localtime(t)))

    # ---- events
    def event(self, t, level, text, snap=None):
        e = {'t': t, 'level': level, 'text': text}
        if snap:
            e['snap'] = snap
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
        self.now = now
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
            raw['clip'] = (self._grade(n['clip'], CLIP_PCT, 'clip'), '%.2f %% clipped' % n['clip'])
        else:
            if self.auto_gain:
                raw['noise'] = (NA, 'automatic gain: the level means nothing')
            else:
                raw['noise'] = (self._grade(n['rise'], NOISE_RISE_DB, 'noise'),
                                '%+.1f dB (band %.1f dBFS)' % (n['rise'], n['power']))
            raw['peak'] = (self._grade(n['peak'], PEAK_DB, 'peak'), '%.1f dB at %.4f MHz' % (n['peak'], n['peak_mhz']))
            raw['clip'] = (self._grade(n['clip'], CLIP_PCT, 'clip'), '%.2f %% clipped' % n['clip'])
            self._push('rise', now, n['rise'])
            self._push('peak', now, n['peak'])
        noisy = bool(n and not n['learning'] and (n['rise'] >= 2.0 and not self.auto_gain or n['peak'] >= PEAK_DB[0]))

        # -- signals: C/N0 drop and lock loss
        # Judged on the receiver's own clock - the time of the newest line it sent - not on this tick's wall clock:
        # when the window stalls (a slow redraw) or lines arrive in a batch, the satellites' last lines look
        # seconds old at the tick and every satellite seemed lost (seen 2026-11-06 20:48: 'Satellites lost, 0
        # locked (was 12)' while all 12 were reported every second). A receiver that stops sending altogether is
        # 'receiver stopped', not satellites lost.
        ts = min(now, self.newest_t + 1.0) if self.newest_t is not None else now
        locked = {s: v for s, v in self.last.items() if ts - v[0] < 5}
        self.counts.append((ts, len(locked)))
        while self.counts and self.counts[0][0] < ts - 120:
            self.counts.popleft()
        drops = []
        for s in locked:
            q = self.cn0.get(s, ())
            old = [c for t, c in q if ts - 70 <= t < ts - 15]
            new = [c for t, c in q if t >= ts - 5]
            if len(old) >= 20 and new:
                drops.append(float(np.median(old)) - float(np.median(new)))
        # a stream gap (SDR/USB) drops every satellite too - but it is the receiver, not radio: at most a watch,
        # said so, and the lock check raises at once on the fingerprint of satellites actually dropping together
        # (it is over before RAISE_S; a GGAP alone, with the lost samples zero-filled, may cost no satellite).
        # With extra noise at the same time the radio evidence wins (a short jammer burst drops and re-acquires too)
        gap = self.gap_recent(now) and not noisy
        gap_text = 'stream gap (SDR/USB) - not RF'
        fingerprint = gap and self.gap_why in ('re-acquired together', 'lost together')
        lock_now = False
        if len(drops) >= 3:
            d = float(np.median(drops))
            g = self._grade(d, CN0_DROP_DB, 'cn0')
            if g == ALERT and not noisy:
                g = WATCH           # weaker without more noise: blocked, not jammed
            if gap:
                raw['cn0'] = (min(g, WATCH), '%+.1f dB over %d satellites (%s)' % (-d, len(drops), gap_text))
            else:
                raw['cn0'] = (g, '%+.1f dB over %d satellites%s' % (-d, len(drops), '' if noisy or d < CN0_DROP_DB[0]
                                                                     else ' (noise normal: blocked?)'))
        else:
            raw['cn0'] = (NA, 'needs 3 satellites tracked for a minute')
        before = [c for t, c in self.counts if ts - 70 <= t < ts - 15]
        if before and max(before) >= 4:
            was = float(np.median(before))
            lost = len(locked) <= was / 3
            if gap:
                lock_now = fingerprint and self.gap_recent(now, GAP_REACQ_S)
                raw['lock'] = (WATCH if lost or lock_now else OK,
                               '%d locked (was %.0f) - %s' % (len(locked), was, gap_text))
            else:
                raw['lock'] = ((ALERT if noisy else WATCH) if lost else OK, '%d locked (was %.0f)' % (len(locked), was))
        elif fingerprint and self.gap_recent(now, GAP_REACQ_S):
            lock_now = True
            raw['lock'] = (WATCH, '%d locked - %s' % (len(locked), gap_text))
        else:
            raw['lock'] = (NA, 'needs 4 satellites')
        if locked:
            cs = [v[1] for v in locked.values()]
            self._push('cn0', now, float(np.median(cs)))
        self._push('nsat', now, len(locked))

        # -- strength
        if len(locked) >= 3:
            cs = np.array([v[1] for v in locked.values()])
            g = self._grade(float(cs.max()), CN0_HIGH, 'strength')
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
            raw['pos_home'] = (self._grade(d, POS_HOME_M, 'pos_home'),
                               '%.0f m from home (%d satellites)' % (d, recent[-1][4]))
            self._push('pos', now, d)
            first = self.first_fixes
            if len(first) >= 10 and now - first[-1][0] > 0:
                flat, flon = float(np.median([f[1] for f in first])), float(np.median([f[2] for f in first]))
                dd = distance_m(flat, flon, lat, lon)
                raw['pos_drift'] = (self._grade(dd, POS_DRIFT_M, 'pos_drift'), '%.0f m since the start' % dd)
            else:
                raw['pos_drift'] = (NA, 'needs the first fixes')
        else:
            raw['pos_home'] = raw['pos_drift'] = (NA, 'no position fix yet')

        # -- time
        g = [x for x in self.gtimes if now - x[0] < 30]
        ref = [x[1] for x in self.gtimes if now - 150 <= x[0] < now - 30]
        if g and len(ref) >= 3 and now - self.pc_step_t > 30:
            j = (g[-1][1] - float(np.median(ref))) * 1000
            raw['clock'] = (self._grade(abs(j), CLOCK_MS, 'clock'), '%+.0f ms against the last 2 min' % j)
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

        self._arm(now, locked, n, noisy)
        raised = []
        for c in self.checks:
            lv, text = raw.get(c.key, (NA, ''))
            if not self.armed:          # nothing is raised until the signal has proven itself (values still shown)
                lv = NA
            old = c.update(now, lv, text, at_once=(c.key == 'lock' and lock_now))
            if old is None:
                continue
            if c.level > max(old, OK):
                self.event(now, c.level, '%s: %s - %s' % (LEVEL_WORD[c.level].upper(), c.title, text))
                raised.append(c)
            elif old >= WATCH:
                self.event(now, c.level, '%s back to %s (%s)' % (c.title, LEVEL_WORD[c.level], text))
        self._snapshot(now, raised, home)

    # ---- event snapshots
    def _state(self):
        return [{'check': c.key, 'title': c.title, 'level': LEVEL_WORD[c.level], 'value': c.value} for c in self.checks]

    def _sats(self):
        return [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()} for r in self.sats]

    def _snapshot(self, now, raised, home):
        s = self.snap
        self.blocked = {k for k in self.blocked if self.by_key[k].level >= WATCH}
        if s is None:
            # a signal that stays up (a steady local spur) gets one snapshot, not a new one every MAX_S
            raised = [c for c in raised if c.key not in self.blocked]
            if not raised or not self.snap_dir:
                return
            day = time.strftime('%Y%m%d', time.localtime(now))
            try:
                today = sum(1 for d in os.listdir(self.snap_dir) if d.startswith(day + '-'))
            except OSError:
                today = 0
            if today >= SNAPS_PER_DAY:
                if getattr(self, '_cap_day', None) != day:
                    self._cap_day = day
                    self.event(now, OK, 'No more snapshots today (%d saved, the most per day): events are still '
                                        'listed' % today)
                return
            name = '%s-%s' % (time.strftime('%Y%m%d-%H%M%S', time.localtime(now)), raised[0].key)
            # the raw samples: a signal drop or lock loss may be a stream gap, whose sign (GGAP, GLOG or the
            # re-acquired fingerprint) can arrive a few seconds after the trigger - wait for it before asking
            # (gnss_rx keeps the 20 s before the request); other checks ask at once
            s = self.snap = {'name': name, 'folder': os.path.join(self.snap_dir, name), 't_trigger': now,
                             'lines': list(self.ring), 'raised': [], 'worst': {}, 't_clear': None, 'home': home,
                             'at_trigger': self._state(), 'sats_at_trigger': self._sats(), 'bias_at_trigger': self.bias,
                             'gap': False, 'iq_due': now + (GAP_IQ_WAIT_S if raised[0].key in ('cn0', 'lock') else 0.0)}
        if self.gap_t is not None and s['t_trigger'] - GAP_REACQ_S <= self.gap_t <= s['t_trigger'] + GAP_REACQ_S + 5:
            s['gap'] = True
        if s['iq_due'] is not None and now >= s['iq_due']:
            s['iq_due'] = None
            if s['gap'] and self.gap_iq_kept:
                self.event(now, OK, 'Raw samples not requested: a stream gap (SDR/USB), not RF - one gap\'s samples '
                                    'are already kept as evidence')
            elif self.on_open:
                if s['gap']:
                    self.gap_iq_kept = True
                self.on_open(os.path.join(s['folder'], 'samples'))
        for c in raised:
            s['raised'].append({'t': now, 'check': c.key, 'title': c.title, 'level': LEVEL_WORD[c.level],
                                'value': c.value})
        hot = [c for c in self.checks if c.level >= WATCH]
        for c in hot:
            w = s['worst'].get(c.key)
            if w is None or c.level > w['rank']:
                s['worst'][c.key] = {'rank': c.level, 'level': LEVEL_WORD[c.level], 'title': c.title, 'value': c.value,
                                     't': now}
        if hot:
            s['t_clear'] = None
        elif s['t_clear'] is None:
            s['t_clear'] = now
        if s['t_clear'] is not None and now - s['t_clear'] >= POST_S:
            self._close(now, 'all checks clear for %.0f s' % POST_S)
        elif now - s['t_trigger'] >= MAX_S:
            self.blocked |= {c.key for c in hot}
            self._close(now, 'longest snapshot (%.0f min) reached; still active (no new snapshot for %s until it '
                             'clears)' % (MAX_S / 60, ', '.join(c.title.lower() for c in hot)))

    def _close(self, now, why):
        s, self.snap = self.snap, None
        t_clear = s['t_clear'] or now
        lines = sorted(s['lines'])
        summary = summarize(lines, s['t_trigger'], t_clear, s['home'], gap=s.get('gap', False))
        lv = max([w['rank'] for w in s['worst'].values()] or [WATCH])
        info = {
            'event': s['name'],
            'trigger_local': time.strftime('%Y-%m-%d %H:%M:%S %Z (%A)', time.localtime(s['t_trigger'])),
            'trigger_unix': s['t_trigger'],
            'cleared_unix': s['t_clear'], 'closed_unix': now, 'closed_because': why,
            'band': self.band, 'band_label': self.label, 'receiver': self.meta,
            'home': {'lat': s['home'][0], 'lon': s['home'][1], 'alt_km': s['home'][2]} if s['home'] else None,
            'summary': summary,
            'checks_raised': s['raised'],
            'worst': {k: {x: y for x, y in v.items() if x != 'rank'} for k, v in s['worst'].items()},
            'checks_at_trigger': s['at_trigger'], 'checks_at_end': self._state(),
            'satellites_at_trigger': s['sats_at_trigger'], 'satellites_at_end': self._sats(),
            'sdr_doppler_offset_hz_at_trigger': s['bias_at_trigger'],
            'lines': {'file': 'lines.txt', 'count': len(lines), 'from_unix': lines[0][0] if lines else None,
                      'to_unix': lines[-1][0] if lines else None,
                      'kinds': 'GCH satellite tracked, GPOS position fix, GNOISE band noise, GSPEC spectrum vs baseline, '
                               'GTIME GPS minus PC clock, GGAP sample-stream gap (ms, source), GLOG pocket_trk\'s own '
                               'log (FOUND/LOST/FALSELOCK/EPOCH/RESTART/START) (see gnss_rx.py)'},
        }
        try:
            os.makedirs(s['folder'], exist_ok=True)
            with open(os.path.join(s['folder'], 'event.json'), 'w') as f:
                json.dump(info, f, indent=1)
            with open(os.path.join(s['folder'], 'lines.txt'), 'w') as f:
                f.write(''.join(l + '\n' for _, _, l in lines))
        except OSError as e:
            self.event(now, lv, 'Could not save the event snapshot (%s)' % e)
            return
        self.event(now, lv, 'Snapshot saved: %s - %s (double-click to view)' % (s['name'], summary['verdict']),
                   snap=s['folder'])
        if self.on_saved:
            self.on_saved(s['folder'])

    def _grade(self, v, lim, key=None):
        """OK / WATCH / ALERT for a value against its (watch, alert) thresholds. With a check key: hysteresis - a
        level is raised the moment its line is reached, but the level already shown is kept until the value is
        below that line by a margin (HYST_DB, else HYST_FRAC of the line); Check.update then still waits CLEAR_S.
        The shown value itself is the raw one"""
        g = ALERT if v >= lim[1] else WATCH if v >= lim[0] else OK
        if key is None:
            return g
        cur = self.by_key[key].level
        if g >= cur or cur < WATCH:
            return g
        line = lim[cur - 1]
        return cur if v >= line - HYST_DB.get(key, HYST_FRAC * line) else g

    def _push(self, key, t, v):
        q = self.series[key]
        q.append((t, v))
        while q and q[0][0] < t - HISTORY_S:
            q.popleft()

    # ---- arming: a proven good connection before anything counts as an event
    def _arm(self, now, locked, n, noisy):
        """armed once ARM_SATS+ satellites at a median C/N0 of ARM_CN0+ held for ARM_S, with the noise baseline
        learned and the band at its quiet level; disarmed if the signal then stays below that bar for DISARM_S with
        no extra noise (a poor antenna or cable, not an attack - nothing to log)"""
        cs = [v[1] for v in locked.values()]
        med = float(np.median(cs)) if cs else 0.0
        good = len(cs) >= ARM_SATS and med >= ARM_CN0
        have = '%d satellites at a median %.0f dB-Hz' % (len(cs), med) if cs else 'no satellites'
        need = 'needs %d+ at %.0f+ for %d min' % (ARM_SATS, ARM_CN0, ARM_S / 60)
        noise_ok = bool(n and not n['learning'] and (self.auto_gain or n['rise'] < NOISE_RISE_DB[0]))
        if not self.armed:
            if good and noise_ok:
                self.good_since = self.good_since or now
                held = now - self.good_since
                if held >= ARM_S:
                    self.armed, self.poor_since = True, None
                    self.event(now, OK, 'Armed: good signal for %d min (%s) - events count from now' % (ARM_S / 60, have))
                    self.arm_note = ''
                else:
                    self.arm_note = 'arming: good signal for %.0f of %.0f s (%s)' % (held, ARM_S, have)
            else:
                self.good_since = None
                if good and n and n['learning']:
                    self.arm_note = 'waiting: learning the quiet band'
                elif good and not n:
                    self.arm_note = 'waiting: no noise readings yet'
                elif good:
                    self.arm_note = ('waiting: the band is %+.1f dB above its learned quiet level - if the antenna was '
                                     'just connected or changed, press Relearn quiet noise' % n['rise'])
                else:
                    self.arm_note = 'waiting for a good signal: %s (%s)' % (have, need)
            return
        if good or noisy:
            self.poor_since = None
            return
        self.poor_since = self.poor_since or now
        if now - self.poor_since >= DISARM_S:
            self.armed, self.good_since = False, None
            if self.snap:
                self._close(now, 'disarmed: the signal stayed poor')
            for c in self.checks:
                c.level, c.pending = NA, None
            self.event(now, OK, 'Disarmed: the signal stayed below the bar for %d min without extra noise (%s) - '
                                'an antenna or cable issue, not logged as an event' % (DISARM_S / 60, have))
            self.arm_note = 'waiting for a good signal: %s (%s)' % (have, need)

    def verdict(self, group):
        """(level, one line) for jamming / spoofing"""
        if not self.active:
            return NA, self.idle_note or 'not watching - start GNSS L1 (GPS) on the receiver'
        tail = ('; ' + self.gap_note()) if group == 'jam' and self.gap_note() else ''
        if not self.armed:
            return NA, (self.arm_note or 'waiting for a good signal') + tail
        cs = [c for c in self.checks if c.group == group]
        lv = max(c.level for c in cs)
        hot = [c.title.lower() for c in cs if c.level == lv and lv >= WATCH]
        if lv >= WATCH:
            if group == 'jam' and self.gap_recent(getattr(self, 'now', time.time())):
                tail = ' - a stream gap (SDR/USB), not RF' + tail
            return lv, ', '.join(hot) + tail
        judged = sum(1 for c in cs if c.level != NA)
        if not judged:
            return NA, 'waiting for satellites' + tail
        return OK, 'no sign (%d of %d checks running)%s' % (judged, len(cs), tail)


# ---------------------------------------------------------------- what an event looked like
def parse_lines(lines):
    """[(t, kind, line)] -> per kind lists of parsed values"""
    out = {'noise': [], 'spec': [], 'ch': collections.defaultdict(list), 'pos': [], 'time': [], 'gaps': [], 'log': []}
    for t, kind, line in lines:
        f = line.split()
        try:
            if kind == 'GNOISE' and len(f) >= 10 and f[8] == '0':
                out['noise'].append((t, float(f[3]), float(f[4]), float(f[5]), float(f[6])))  # rise clip peak MHz
            elif kind == 'GSPEC' and len(f) >= 5:
                out['spec'].append((t, float(f[2]), float(f[3]), [int(v) / 10 for v in f[4].split(',')]))
            elif kind == 'GCH' and len(f) >= 6:         # (t, cn0, doppler, lock seconds or None)
                out['ch'][f[2]].append((t, float(f[4]), float(f[5]), float(f[6]) if len(f) >= 7 else None))
            elif kind == 'GPOS' and len(f) >= 6 and int(f[5]) >= 4:
                out['pos'].append((t, float(f[2]), float(f[3]), float(f[4]), int(f[5])))
            elif kind == 'GTIME' and len(f) >= 5:
                out['time'].append((t, float(f[2])))
            elif kind == 'GGAP' and len(f) >= 3:        # (t, ms, source)
                out['gaps'].append((t, float(f[2]), f[3] if len(f) > 3 else 'usb'))
            elif kind == 'GLOG' and len(f) >= 3:        # (t, EVENT, sat or '', values)
                ev = f[2].upper()
                sat = f[3] if len(f) > 3 and ev in ('FOUND', 'LOST', 'FALSELOCK') else ''
                out['log'].append((t, ev, sat, f[4:] if sat else f[3:]))
        except (ValueError, IndexError):
            pass
    return out


def _onset(p, t_trigger):
    """(when it really began, how that was found). The checks look 5 s back and wait RAISE_S, so the trigger comes
    seconds after the collapse - and with the onset taken as trigger - RAISE_S a satellite that fell 2 s earlier
    showed as unaffected (G18 0.8 dB, G32 1.6 dB in the 03:56 event, though all nine fell to 8-29 dB-Hz). Looked
    for in the 10 s before the trigger: the first second where the satellites' median C/N0 was more than 3 dB
    below the previous minute's, pocket_trk's first LOST, or the moment a satellite vanished that came back with
    a young lock"""
    lo, hi = t_trigger - 10.0, t_trigger
    cands = []
    by_sec = collections.defaultdict(list)
    for v in p['ch'].values():
        for t, c, *_ in v:
            by_sec[int(t)].append((t, c))
    secs = sorted(by_sec)
    for sec in secs:
        if sec < lo - 1 or sec > hi:
            continue
        ref = [c for s2 in secs if sec - 60 <= s2 < sec for _, c in by_sec[s2]]
        cur = [c for _, c in by_sec[sec]]
        if len(ref) >= 10 and cur and float(np.median(ref)) - float(np.median(cur)) > 3.0:
            cands.append((min(t for t, _ in by_sec[sec]), 0, 'median C/N0 fell'))
            break
    lost = [t for t, ev, _, _ in p['log'] if ev == 'LOST' and lo <= t <= hi]
    if lost:
        cands.append((min(lost), 1, 'pocket_trk LOST'))
    for v in p['ch'].values():      # a lock age under GAP_RESET_S after one over GAP_OLD_S: it vanished at t_prev
        prev = None
        for t, c, _, lock in v:
            if lock is not None and prev is not None and lock < GAP_RESET_S and prev[1] > GAP_OLD_S \
                    and lo <= prev[0] <= hi and t <= hi + GAP_REACQ_S:
                cands.append((prev[0], 2, 'lock age reset'))
                break
            if lock is not None:
                prev = (t, lock)
    if not cands:
        return t_trigger - RAISE_S, 'trigger - %.0f s (no clearer sign in the data)' % RAISE_S
    t, _, why = min(cands)
    return min(max(t, lo), hi), why


def summarize(lines, t_trigger, t_clear, home, gap=False):
    """the event in numbers and a best-guess description: when it began and ended, how the noise rose and fell,
    what kind of signal (carrier, swept, broadband) and where, what it did to each satellite, the position, time;
    and whether it was a stream gap (SDR/USB) rather than radio (gap: the monitor already saw one live)"""
    p = parse_lines(lines)
    onset, onset_from = _onset(p, t_trigger)
    s = {'local_time': time.strftime('%H:%M:%S', time.localtime(t_trigger)),
         'weekday': time.strftime('%A', time.localtime(t_trigger)),
         'onset_unix': round(onset, 3), 'onset_local': time.strftime('%H:%M:%S', time.localtime(onset)),
         'onset_from': onset_from,
         'duration_s': round(max(0.0, t_clear - onset), 1)}
    notes = []
    # -- noise: level, shape in time, kind of signal
    nz = p['noise']
    before = [x for x in nz if x[0] < onset - 5]
    during = [x for x in nz if onset - 30 <= x[0] <= t_clear + 5]
    jam = False
    if during:
        rises = np.array([x[1] for x in during])
        peaks = np.array([x[3] for x in during])
        i = int(np.argmax(rises))
        j = int(np.argmax(peaks))
        s['noise'] = {'baseline_rise_before_db': round(float(np.median([x[1] for x in before])), 2) if before else None,
                      'max_rise_db': round(float(rises[i]), 2),
                      'max_rise_at': time.strftime('%H:%M:%S', time.localtime(during[i][0])),
                      'max_peak_db': round(float(peaks[j]), 2), 'max_peak_mhz': during[j][4],
                      'max_clip_pct': round(max(x[2] for x in during), 4)}
        strong = [x for x in during if x[3] >= PEAK_DB[0]]
        if strong:
            fs = np.array([x[4] for x in strong])
            s['noise']['peak_mhz_range'] = [round(float(fs.min()), 4), round(float(fs.max()), 4)]
            s['noise']['peak_seconds'] = len(strong)
        # how it came and went: time from 10 % of the top to the top, and back
        key = rises if rises[i] >= NOISE_RISE_DB[0] else peaks
        top = int(np.argmax(key))
        lim = 0.1 * key[top]
        a = top
        while a > 0 and key[a - 1] > lim:
            a -= 1
        b = top
        while b < len(key) - 1 and key[b + 1] > lim:
            b += 1
        s['noise']['rise_time_s'] = round(during[top][0] - during[a][0], 1)
        s['noise']['fall_time_s'] = round(during[b][0] - during[top][0], 1)
        jam = rises[i] >= NOISE_RISE_DB[0] or peaks[j] >= PEAK_DB[0]
    # -- spectrum: which part of the band was hit
    sp = [x for x in p['spec'] if onset - 30 <= x[0] <= t_clear + 5]
    if sp:
        n = min(len(x[3]) for x in sp)
        hold = np.max([x[3][:n] for x in sp], axis=0)
        floor = float(np.median(hold))
        hot = np.nonzero(hold > 3)[0]       # (GSPEC is already against the quiet baseline)
        f_lo, f_hi = sp[0][1], sp[0][2]
        df = (f_hi - f_lo) / max(n - 1, 1)
        if len(hot):
            s['spectrum'] = {'max_hold_floor_db': round(floor, 2), 'hit_from_mhz': round(f_lo + hot[0] * df, 4),
                             'hit_to_mhz': round(f_lo + hot[-1] * df, 4), 'bins_hit': int(len(hot)), 'bins': n,
                             'strongest_mhz': round(f_lo + int(np.argmax(hold)) * df, 4),
                             'strongest_db': round(float(hold.max()), 1)}
    if jam:
        nzs = s['noise']
        width = (s['spectrum']['hit_to_mhz'] - s['spectrum']['hit_from_mhz']) if 'spectrum' in s else None
        fr = nzs.get('peak_mhz_range')
        if fr and fr[1] - fr[0] > 0.2:
            kind = 'a swept (chirp) signal across %.3f-%.3f MHz' % tuple(fr)
        elif nzs['max_peak_db'] >= PEAK_DB[0] and (width is None or width < 0.3):
            kind = 'a narrowband signal at %.4f MHz%s' % (nzs['max_peak_mhz'], ' (about %.0f kHz wide)' % (width * 1000)
                                                          if width else '')
        elif nzs['max_rise_db'] >= NOISE_RISE_DB[0]:
            kind = 'broadband noise (+%.1f dB%s)' % (nzs['max_rise_db'], ', across %.2f MHz of the band' % width
                                                     if width else '')
        else:
            kind = 'interference peaking at %.4f MHz' % nzs['max_peak_mhz']
        notes.append('Signal: ' + kind)
        r, f = nzs['rise_time_s'], nzs['fall_time_s']
        if r >= 5 and f >= 5 and max(r, f) / max(min(r, f), 1) < 3:
            notes.append('It grew over %.0f s and faded over %.0f s - like a moving source passing by (a jammer in '
                         'a vehicle)' % (r, f))
        elif r <= 2:
            notes.append('It switched on abruptly (rise %.0f s) - a source nearby being turned on, or a test' % r)
        else:
            notes.append('Rise %.0f s, fall %.0f s' % (r, f))
    # -- satellites: C/N0 before and at the worst, who was lost
    sats = {}
    lost = []
    long_locked, reacq = 0, []
    for sat, v in p['ch'].items():
        pre = [c for t, c, *_ in v if onset - 60 <= t < onset - 2]
        win = [(t, c) for t, c, *_ in v if onset - 2 <= t <= t_clear + 5]
        e = {'cn0_before': round(float(np.median(pre)), 1) if pre else None}
        if win:
            e['cn0_min'] = round(min(c for _, c in win), 1)
            if e['cn0_before'] is not None:
                e['drop_db'] = round(e['cn0_before'] - e['cn0_min'], 1)
        if pre:     # tracked before: lost if it went quiet for 10 s or more during the event
            ts = [onset] + sorted(t for t, _ in win) + [t_clear]
            quiet = max(b - a for a, b in zip(ts, ts[1:]))
            if quiet >= 10:
                lost.append(sat)
                e['lost'] = True
                e['longest_gap_s'] = round(quiet, 1)
            # the stream-gap fingerprint: a long lock before, a young one soon after
            locks_pre = [lk for t, _, _, lk in v if t < onset + 0.5 and lk is not None and onset - 60 <= t]
            if locks_pre and locks_pre[-1] > GAP_OLD_S:
                long_locked += 1
                young = [t for t, _, _, lk in v if onset + 0.5 <= t <= onset + GAP_REACQ_S + 5
                         and lk is not None and lk < GAP_RESET_S]
                if young:
                    e['reacquired_after_s'] = round(young[0] - onset, 1)
                    reacq.append(young[0] - onset)
        sats[sat] = e
    s['satellites'] = sats
    drops = [e['drop_db'] for e in sats.values() if 'drop_db' in e]
    if drops:
        s['median_cn0_drop_db'] = round(float(np.median(drops)), 1)
    if lost:
        s['satellites_lost'] = sorted(lost)
    # -- a stream gap (SDR/USB) rather than radio? gnss_rx's own report, pocket_trk's LOST burst, or the
    #    re-acquired fingerprint (most long-locked satellites back young within GAP_REACQ_S)
    gaps = [g for g in p['gaps'] if onset - GAP_RECENT_S <= g[0] <= t_trigger + 5]
    s['gaps'] = [{'local': time.strftime('%H:%M:%S', time.localtime(t)), 'unix': round(t, 3), 'ms': ms,
                  'source': src} for t, ms, src in p['gaps']]
    s['restarts'] = [time.strftime('%H:%M:%S', time.localtime(t)) for t, ev, _, _ in p['log'] if ev == 'RESTART']
    burst = {sat for t, ev, sat, vals in p['log'] if ev == 'LOST' and onset - 2 <= t <= onset + 5
             and (not vals or float(vals[0]) < GAP_LOST_CN0)}
    tracked = sum(1 for e in sats.values() if e['cn0_before'] is not None)
    lost_burst = len(burst) >= 2 and len(burst) >= tracked / 2
    reacquired = len(reacq) >= 2 and len(reacq) >= long_locked / 2
    # (the fingerprint alone does not outweigh a noise rise or peak at the same time - a short jammer burst drops
    # and re-acquires too; a measured GGAP does)
    if gaps or ((lost_burst or reacquired or gap) and not jam):
        s['kind'] = 'stream_gap'
        back = ('%d of %d satellites re-acquired within %.0f s' % (len(reacq), long_locked, max(reacq))
                if reacq else '%d satellites reported lost within a second' % len(burst) if burst else
                'the satellites dropped together')
        if gaps:
            ms = max(g[1] for g in gaps)
            notes.append('The SDR lost %s of samples (%s), %s - not an RF event' % (_ms(ms), gaps[0][2], back))
        else:
            notes.append('The sample stream broke (SDR/USB): %s at their previous strength - not an RF event' % back)
    elif jam:
        s['kind'] = 'interference'
    elif drops and float(np.median(drops)) >= CN0_DROP_DB[0]:
        s['kind'] = 'signal_drop'
        notes.append('Signals weakened %.0f dB without more noise - the antenna blocked or disconnected (or a '
                     'spoofer taking over)' % float(np.median(drops)))
    else:
        s['kind'] = 'other'
    # -- position and time
    if home and p['pos']:
        d = [(t, distance_m(home[0], home[1], la, lo)) for t, la, lo, _, _ in p['pos']]
        pre = [x for t, x in d if t < onset]
        win = [x for t, x in d if onset <= t <= t_clear + 5]
        s['position'] = {'from_home_before_m': round(float(np.median(pre)), 1) if pre else None,
                         'from_home_max_m': round(max(win), 1) if win else None,
                         'fixes_during': len(win)}
    if p['time']:
        off = [o for t, o in p['time'] if onset - 120 <= t <= t_clear + 5]
        if off:
            s['gps_minus_pc_s'] = {'min': round(min(off), 4), 'max': round(max(off), 4)}
    s['notes'] = notes
    s['verdict'] = notes[0] if notes else 'see the checks raised'
    return s


# ---------------------------------------------------------------- the section
def level_colour(lv):
    return {ALERT: theme.PAL['ALERT'], WATCH: theme.PAL['WARN'], OK: theme.PAL['GOOD']}.get(lv, theme.PAL['MUTED'])


class StripChart(QtWidgets.QWidget):
    """a value over the last 30 minutes, with the watch / alert lines (instrument-dark, like the other plots)"""

    def __init__(self, title, unit, lines, limits=None, floor_range=1.0, absval=False, window=None, marker=None):
        super().__init__()
        self.title, self.unit, self.lines, self.limits = title, unit, lines, limits
        self.floor_range, self.absval = floor_range, absval
        self.window, self.marker = window, marker       # a fixed (t0, t1) instead of the last 30 min; a time line
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
        t_a, now = self.window or (time.time() - HISTORY_S, time.time())
        span = max(now - t_a, 1.0)
        vals = [v for key, _, _ in self.lines for t, v in self.series.get(key, ()) if t_a <= t <= now]
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
            return L + pw * (1 - (now - t) / span)

        p.setPen(QtGui.QColor('#8b93a1'))
        f = p.font()
        f.setPointSizeF(8)
        p.setFont(f)
        p.drawText(L, 2, pw, 14, Qt.AlignLeft, self.title)
        ticks = ([(fr, time.strftime('%H:%M:%S', time.localtime(t_a + fr * span))) for fr in (0, 0.5, 1)]
                 if self.window else ((0, '-30 min'), (0.5, '-15'), (1, 'now')))
        for frac, txt in ticks:
            p.drawText(int(L + pw * frac) - 30, h - 14, 60, 14, Qt.AlignCenter, txt)
        if self.marker and t_a <= self.marker <= now:
            p.setPen(QtGui.QPen(QtGui.QColor('#e6e9ef'), 1, Qt.DotLine))
            p.drawLine(int(X(self.marker)), T, int(X(self.marker)), T + ph)
            p.setPen(QtGui.QColor('#8b93a1'))
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
            pts = [(X(t), Y(v)) for t, v in self.series.get(key, ()) if t_a <= t <= now]
            p.setPen(QtGui.QPen(QtGui.QColor(col), 1.5))
            path = QtGui.QPainterPath()
            for i, (px, py) in enumerate(pts):
                if i and px - pts[i - 1][0] > pw * 20 / span:      # a gap (receiver stopped)
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


WF_STOPS = [(0.0, (8, 12, 40)), (0.15, (20, 40, 120)), (0.35, (0, 170, 200)), (0.6, (240, 220, 40)),
            (1.0, (255, 40, 40))]       # -1 dB (dark blue) ... +15 dB (red) above the baseline


def waterfall_image(spec, t_mark=None, col_w=4):
    """GSPEC rows [(t, f_lo, f_hi, [dB...])] -> a waterfall QImage (frequency across, time down, one row a second;
    white ticks at the event's start)"""
    if not spec:
        return None
    n = min(len(x[3]) for x in spec)
    v = np.array([x[3][:n] for x in spec], float)
    x = np.clip((v + 1.0) / 16.0, 0, 1)
    xs = [a for a, _ in WF_STOPS]
    rgb = np.stack([np.interp(x, xs, [c[i] for _, c in WF_STOPS]) for i in range(3)], axis=-1).astype(np.uint8)
    rgb = np.repeat(rgb, col_w, axis=1)
    if t_mark:
        r = int(np.argmin([abs(s[0] - t_mark) for s in spec]))
        rgb[r, :12] = rgb[r, -12:] = 255
    rgb = np.ascontiguousarray(rgb)
    h, w = rgb.shape[:2]
    return QtGui.QImage(rgb.tobytes(), w, h, 3 * w, QtGui.QImage.Format_RGB888).copy()


def _fmt(v):
    if isinstance(v, float):
        return '%g' % round(v, 4)
    if isinstance(v, list):
        return ', '.join(_fmt(x) for x in v)
    return str(v)


class SnapshotDialog(QtWidgets.QDialog):
    """one saved event: the summary, the waterfall of the band, and the charts from before to after"""

    def __init__(self, parent, folder):
        super().__init__(parent)
        self.folder = folder
        self.setWindowTitle('Event snapshot - %s' % os.path.basename(folder))
        self.resize(1200, 820)
        info = json.load(open(os.path.join(folder, 'event.json')))
        lines = []
        for line in open(os.path.join(folder, 'lines.txt'), errors='replace').read().splitlines():
            f = line.split()
            try:
                lines.append((float(f[1]), f[0].rstrip(':'), line))
            except (IndexError, ValueError):
                pass
        p = parse_lines(lines)
        s = info.get('summary', {})
        t0 = lines[0][0] if lines else info['trigger_unix'] - PRE_S
        t1 = lines[-1][0] if lines else info['closed_unix']
        mark = s.get('onset_unix') or info['trigger_unix'] - RAISE_S      # (older snapshots have no onset)

        text = QtWidgets.QTextBrowser()
        html = ['<h3>%s</h3>' % info.get('trigger_local', ''),
                '<p><b>%s</b> · %s · lasted about %s s · %s</p>' % (
                    info.get('band_label', ''), ', '.join('%s (%s)' % (w['title'], w['level'])
                                                          for w in info.get('worst', {}).values()),
                    _fmt(s.get('duration_s', '?')), info.get('closed_because', ''))]
        for n in s.get('notes', []):
            html.append('<p>• %s</p>' % n)
        if s.get('onset_local'):
            html.append('<p>Began %s (%s)%s</p>' % (s['onset_local'], s.get('onset_from', ''),
                                                    ' · kind: %s' % s['kind'].replace('_', ' ') if s.get('kind') else ''))
        for key, title in (('noise', 'Noise'), ('spectrum', 'Spectrum (max over the event)'), ('position', 'Position'),
                           ('gps_minus_pc_s', 'GPS time minus PC clock (s)')):
            if s.get(key):
                html.append('<p><b>%s</b>: %s</p>' % (title, '; '.join('%s %s' % (k.replace('_', ' '), _fmt(v))
                                                                       for k, v in s[key].items())))
        if s.get('gaps'):
            html.append('<p><b>Stream gaps</b> (the SDR\'s samples went missing; GGAP lines in this window): %s</p>'
                        % ', '.join('%s %s ms (%s)' % (g['local'], _fmt(g['ms']), g['source']) for g in s['gaps']))
        if s.get('restarts'):
            html.append('<p><b>Receiver restarts</b> (pocket_trk): %s</p>' % ', '.join(s['restarts']))
        if s.get('satellites'):
            rows = ''.join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                catalog.gnss_name(k), _fmt(v.get('cn0_before', '-')), _fmt(v.get('cn0_min', '-')),
                _fmt(v.get('drop_db', '-')), '%s s' % _fmt(v['reacquired_after_s']) if 'reacquired_after_s' in v else '',
                'lost' if v.get('lost') else '') for k, v in sorted(s['satellites'].items()))
            html.append('<p><b>Satellites</b> (C/N0 dB-Hz)</p><table cellspacing=6><tr><th align=left>Satellite</th>'
                        '<th>before</th><th>lowest</th><th>drop</th><th>back after</th><th></th></tr>%s</table>' % rows)
        iq = os.path.join(folder, 'samples.cs8')
        html.append('<p><b>Raw samples</b>: %s</p>' % (
            'samples.cs8 (%.0f MB, 20 s before to 20 s after; see samples.json) - open in SDR++ or inspectrum'
            % (os.path.getsize(iq) / 1e6) if os.path.exists(iq) else 'not kept (off, low disk, or pruned)'))
        html.append('<p style="color:gray">Everything is in %s: event.json (all the details), lines.txt (every line '
                    'the receiver sent from %d s before to %d s after), waterfall.png.</p>' % (folder, PRE_S, POST_S))
        text.setHtml(''.join(html))

        wf = QtWidgets.QLabel()
        wf.setAlignment(Qt.AlignCenter)
        img = waterfall_image(p['spec'], mark)
        if img is not None:
            wf.setPixmap(QtGui.QPixmap.fromImage(img.scaled(512, min(max(img.height(), 200), 600))))
            if p['spec']:
                wf.setToolTip('The band against its quiet baseline, %.3f MHz (left) to %.3f MHz (right), one row a '
                              'second from top to bottom; the white ticks mark the event\'s start' %
                              (p['spec'][0][1], p['spec'][0][2]))
        else:
            wf.setText('no spectrum lines in this snapshot')
        wf_box = QtWidgets.QWidget()
        wv = QtWidgets.QVBoxLayout(wf_box)
        wv.setContentsMargins(0, 0, 0, 0)
        cap = QtWidgets.QLabel()
        cap.setWordWrap(True)
        if p['spec']:
            cap.setText('<b>The band over the event</b> - %.3f MHz (left) to %.3f MHz (right); time runs down, one row '
                        'a second, %s to %s; the white ticks mark the start. Colour: dB above the quiet band, dark blue '
                        '0, cyan +5, yellow +9, red +15.' % (
                            p['spec'][0][1], p['spec'][0][2], time.strftime('%H:%M:%S', time.localtime(p['spec'][0][0])),
                            time.strftime('%H:%M:%S', time.localtime(p['spec'][-1][0]))))
        wv.addWidget(cap)
        series = {'rise': [(x[0], x[1]) for x in p['noise']], 'peak': [(x[0], x[3]) for x in p['noise']],
                  'cn0': [], 'nsat': []}
        by_t = collections.defaultdict(list)
        for sat, v in p['ch'].items():
            for t, c, *_ in v:
                by_t[int(t)].append(c)
        for t in sorted(by_t):
            series['cn0'].append((t, float(np.median(by_t[t]))))
            series['nsat'].append((t, len(by_t[t])))
        home = info.get('home')
        if home:
            series['pos'] = [(t, distance_m(home['lat'], home['lon'], la, lo)) for t, la, lo, _, _ in p['pos']]
        charts = [StripChart('Noise above the quiet baseline', 'dB', [('rise', '#4c9aff', 'band power'),
                                                                      ('peak', '#c792ea', 'narrowband peak')],
                             NOISE_RISE_DB, 2.0, window=(t0, t1), marker=mark),
                  StripChart('Satellites', '', [('cn0', '#4cd07d', 'median C/N0 dB-Hz'), ('nsat', '#e6e9ef', 'locked')],
                             None, 5.0, window=(t0, t1), marker=mark),
                  StripChart('Position from home', 'm', [('pos', '#4c9aff', 'distance')], POS_HOME_M[:1], 50.0,
                             window=(t0, t1), marker=mark)]
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        for c in charts:
            c.set_series(series)
            rv.addWidget(c, 1)
        top = QtWidgets.QSplitter()
        top.addWidget(text)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(wf)
        scroll.setWidgetResizable(True)
        wv.addWidget(scroll, 1)
        top.addWidget(wf_box)
        top.setSizes([620, 560])
        split = QtWidgets.QSplitter(Qt.Vertical)
        split.addWidget(top)
        split.addWidget(right)
        split.setSizes([480, 340])
        v = QtWidgets.QVBoxLayout(self)
        v.addWidget(split, 1)
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        open_btn = QtWidgets.QPushButton('Open folder')
        open_btn.clicked.connect(lambda: os.startfile(folder))
        close = QtWidgets.QPushButton('Close')
        close.clicked.connect(self.accept)
        row.addWidget(open_btn)
        row.addWidget(close)
        v.addLayout(row)


class IntegrityView(QtWidgets.QWidget):
    """the section: two verdicts, the checks, the charts, the satellites and the events"""
    changed = QtCore.pyqtSignal(int)        # the worst level now (for the section button)

    def __init__(self, tracker, orbit_of, home_of):
        super().__init__()
        self.tracker, self.orbit_of, self.home_of = tracker, orbit_of, home_of
        self.snap_dir = os.path.join(paths.DATA, 'integrity')
        self.mon = Monitor(os.path.join(paths.DATA, 'integrity-events.jsonl'), self.snap_dir)
        self.mon.on_open = self._snapshot_began
        self.mon.on_saved = self._snapshot_saved
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
        self.events.setToolTip('What changed, newest first (kept in integrity-events.jsonl in the data folder).\n'
                               'Double-click a "Snapshot saved" line to see that event: summary, waterfall, charts.')
        self.events.itemDoubleClicked.connect(self._open_event)

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
        clear.setToolTip('Empty the list (the saved snapshot folders stay)')
        clear.clicked.connect(self._clear)
        self.iq_box = QtWidgets.QCheckBox('Keep raw samples')
        self.iq_box.setToolTip('With each event snapshot, save the raw SDR samples from 20 s before to 20 s after '
                               '(samples.cs8, ~160 MB; only the newest %d are kept) - to look at the interfering '
                               'signal itself later in SDR++ or inspectrum' % IQ_KEEP)
        self.iq_box.setChecked(bool(theme.load_ui().get('integrity_iq', True)))
        self.iq_box.toggled.connect(lambda on: theme.save_ui(integrity_iq=on))
        folder = QtWidgets.QPushButton('Snapshots')
        folder.setToolTip('Open the folder with the saved event snapshots')
        folder.clicked.connect(self._open_folder)
        row.addWidget(self.iq_box)
        row.addWidget(folder)
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
        if kind in ('GCH', 'GPOS', 'GNOISE', 'GTIME', 'GSPEC', 'GGAP', 'GLOG'):
            self.mon.line(kind, line)

    def on_running(self, on, system=None, auto_gain=False, meta=None):
        self.mon.idle_note = ''
        if on and system is not None and system['key'] in WATCH_BANDS:
            self.mon.start(system['key'], system['label'], auto_gain, meta)
        elif on and system is not None and system['key'] in GNSS_BANDS:
            self.mon.idle_note = ('not watching %s - the watch covers GNSS L1 (GPS) only for now' % system['label'])
        elif not on:
            self.mon.stop()
        self.tick()

    # ---- snapshots
    def _snapshot_began(self, stem):
        """an event began: ask the GNSS receiver for the raw samples around it (if wanted and there is room)"""
        if not self.iq_box.isChecked():
            return
        try:
            import receiver
            import storage
            if storage.free_gb() < storage.settings()['min_free_gb'] + 1:
                self.mon.event(time.time(), OK, 'Raw samples not kept for this event: low disk space')
                return
            os.makedirs(os.path.dirname(stem), exist_ok=True)
            with open(receiver.snap_trigger(), 'w') as f:
                f.write(stem)
        except Exception as e:
            print('integrity: raw-sample request failed: %r' % e)

    def _snapshot_saved(self, folder):
        """a snapshot is complete: its waterfall picture, and only the newest IQ_KEEP raw recordings kept"""
        try:
            lines = []
            for line in open(os.path.join(folder, 'lines.txt'), errors='replace'):
                if line.startswith('GSPEC'):
                    f = line.split()
                    lines.append((float(f[1]), 'GSPEC', line.strip()))
            info = json.load(open(os.path.join(folder, 'event.json')))
            img = waterfall_image(parse_lines(lines)['spec'],
                                  info.get('summary', {}).get('onset_unix') or info['trigger_unix'] - RAISE_S)
            if img is not None:
                img.save(os.path.join(folder, 'waterfall.png'))
        except Exception as e:
            print('integrity: waterfall: %r' % e)
        iq = []
        for d in os.listdir(self.snap_dir):
            p = os.path.join(self.snap_dir, d, 'samples.cs8')
            if os.path.exists(p):
                iq.append((os.path.getmtime(p), p))
        for _, p in sorted(iq)[:-IQ_KEEP]:
            try:
                os.remove(p)
            except OSError:
                pass
        snaps = sorted(d for d in os.listdir(self.snap_dir) if os.path.isfile(os.path.join(self.snap_dir, d, 'event.json')))
        for d in snaps[:-SNAPS_KEEP]:       # (names start with the date and time: oldest first)
            shutil.rmtree(os.path.join(self.snap_dir, d), ignore_errors=True)

    def _open_event(self, item):
        folder = item.data(Qt.UserRole)
        if folder and os.path.exists(os.path.join(folder, 'event.json')):
            SnapshotDialog(self, folder).exec_()
        elif folder:
            QtWidgets.QMessageBox.information(self, 'Event snapshot', 'That snapshot is no longer there:\n%s' % folder)

    def _open_folder(self):
        os.makedirs(self.snap_dir, exist_ok=True)
        os.startfile(self.snap_dir)

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
            gone, ': the receiver relearns them now (30 s)' if busy else ''))
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
            it.setText(1, 'not armed' if self.mon.active and not self.mon.armed else LEVEL_WORD[c.level])
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
                if e.get('snap'):
                    it.setData(Qt.UserRole, e['snap'])
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                self.events.addItem(it)

    def showEvent(self, e):
        super().showEvent(e)
        self.tick()
