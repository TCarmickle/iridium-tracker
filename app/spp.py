"""Our own GNSS position solution (single point positioning), with WAAS corrections.

pocket_trk works out a position from the raw satellite ranges with the broadcast models alone. This module
does the same from its raw measurements ($OBS lines: each satellite's pseudorange, carrier phase, Doppler,
C/N0, once a second) and can add what the WAAS satellites broadcast for North America (RTCA DO-229):
  fast corrections   (message types 2-5, 24)  each GPS satellite's range error now, updated every few seconds
  long-term          (24, 25)                 corrections to each satellite's broadcast orbit and clock
  ionosphere grid    (18 mask, 26 delays)     the delay above a 5 x 5 degree grid, for each satellite's line
                                              of sight through the ionosphere (350 km up)
and carrier smoothing (a Hatch filter, 100 s like WAAS receivers): the carrier phase is millimetre-smooth but
ambiguous, the code range is absolute but noisy - each satellite's code range is carried forward on the carrier's
changes and nudged by each new code measurement, which takes out most of the code noise.

Without WAAS the ionosphere comes from GPS's own broadcast model (Klobuchar; its 8 numbers decoded from subframe 4
page 18 of the GPS navigation message, which pocket_trk logs as $NAV lines).

  Solver.nav(line)              a pocket_trk $NAV line (GPS subframes: the ionosphere model; WAAS messages)
  Solver.eph(eph)               a parsed broadcast ephemeris (gnss.parse_eph)
  Solver.obs(line)              a $OBS line; returns a solution dict when an epoch completes
Times: 'GPST seconds' as in gnss.py (RTKLIB style: GPS time counted like Unix time).
"""
import calendar
import collections
import math

import numpy as np

import gnss

C = 299792458.0
OMGE = 7.2921151467e-5
RE_WGS84 = 6378137.0
F_WGS84 = 1 / 298.257223563
FREQ = {'G': 1575.42e6, 'E': 1575.42e6, 'J': 1575.42e6}
EL_MASK = 10.0                  # degrees
SMOOTH_N = 100                  # Hatch filter length (epochs = seconds)
SLIP_M = 8.0                    # code-carrier jump beyond this: a cycle slip, restart that satellite's smoothing
FAST_TIMEOUT_S = 60.0           # how long WAAS corrections are used after they arrive (aviation uses less)
LT_TIMEOUT_S = 360.0
IONO_TIMEOUT_S = 600.0
IONO_H = 350000.0
HDOP_MAX = 6.0                  # worse geometry than this: no fix (satellites nearly in a line or a plane)
READY_SMOOTH = 30               # seconds of smoothing before a satellite counts as settled
SETTLE_S = 30                   # seconds after a satellite locks before its smoothing starts (code loop settling)
# The antenna never moves, so its height is known from this solver's own earlier fixes: a height row in the solution
# (a 'prior', with this uncertainty) keeps it solving when only 4 satellites with poor geometry, or 3, are left.
# Replays of 2026-11-06's logs: good fixes 1723 -> 3987 of 4043 epochs at the same 1.5-2.5 m (the HDOP gate alone
# threw away 23 % of the seconds with 4 settled GPS satellites, in 7-17 min stretches). Never from the home setting
# (a map-picked home carries a terrain height from the internet, ~20 m off the GPS ellipsoid here).
PRIOR_SIGMA_M = 3.0
PRIOR_RADIUS_M = 1000.0         # the prior only applies while the solution stays this close to where it was learned
LONE_GAL_VAR = 1.0 ** 2         # a lone Galileo satellite rides on the GPS clock: GPS-Galileo bias measured within 1 m
# Error budgets (DO-229 / ICD numbers), in m^2, for Solver(weights='budget'): a WAAS-corrected GPS range weighted by
# its UDRE, others by the broadcast URA / SISA, plus the ionosphere's GIVE and the troposphere. Measured on
# 2026-11-06's sessions with the height prior it was WORSE than the simple weighting (R95 4.8 / 3.6 / 4.9 m against
# 4.7 / 3.4 / 3.5 m): the SISA of 3 m down-weights Galileo ranges that are in fact good - so 'simple' is the default
# and this stays only for experiments (build\_tmp\week2\replay_static.py, VARIANTS=prior,priorS)
UDRE_VAR = [0.0520, 0.0924, 0.1444, 0.2830, 0.4678, 0.8315, 1.2992, 1.8709, 2.5465, 3.3260, 5.1968, 20.7870,
            230.9661, 2078.695]
GIVE_VAR = [0.0084, 0.0333, 0.0749, 0.1331, 0.2079, 0.2994, 0.4075, 0.5322, 0.6735, 0.8315, 1.1974, 1.8709,
            3.3260, 20.787, 187.0826]
URA_M = [2.4, 3.4, 4.85, 6.85, 9.65, 13.65, 24.0, 48.0, 96.0, 192.0, 384.0, 768.0, 1536.0, 3072.0, 6144.0]


def broadcast_var(e):
    """variance (m^2) of a range corrected by the broadcast orbit and clock alone: GPS URA, Galileo SISA"""
    n = int(e.get('sva', 0))
    if e.get('sys') == 'E':
        if n <= 49:
            s = n * 0.01
        elif n <= 74:
            s = 0.5 + (n - 50) * 0.02
        elif n <= 99:
            s = 1.0 + (n - 75) * 0.04
        elif n <= 125:
            s = 2.0 + (n - 100) * 0.16
        else:
            s = 6.0             # (255: no accuracy prediction available)
        return max(s, 0.5) ** 2
    return URA_M[min(max(n, 0), len(URA_M) - 1)] ** 2


# ---------------------------------------------------------------- geometry
def ecef_to_lla(x):
    a, f = RE_WGS84, F_WGS84
    e2 = f * (2 - f)
    lon = math.atan2(x[1], x[0])
    p = math.hypot(x[0], x[1])
    lat = math.atan2(x[2], p * (1 - e2))
    for _ in range(6):
        N = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - N
        lat = math.atan2(x[2], p * (1 - e2 * N / (N + h)))
    N = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return lat, lon, p / math.cos(lat) - N


def lla_to_ecef(lat, lon, h):
    a, f = RE_WGS84, F_WGS84
    e2 = f * (2 - f)
    N = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return np.array([(N + h) * math.cos(lat) * math.cos(lon), (N + h) * math.cos(lat) * math.sin(lon),
                     (N * (1 - e2) + h) * math.sin(lat)])


def azel(rx, sat, lla):
    lat, lon = lla[0], lla[1]
    d = sat - rx
    r = np.linalg.norm(d)
    e = -math.sin(lon) * d[0] + math.cos(lon) * d[1]
    n = -math.sin(lat) * math.cos(lon) * d[0] - math.sin(lat) * math.sin(lon) * d[1] + math.cos(lat) * d[2]
    u = math.cos(lat) * math.cos(lon) * d[0] + math.cos(lat) * math.sin(lon) * d[1] + math.sin(lat) * d[2]
    return math.atan2(e, n) % (2 * math.pi), math.asin(u / r)


def tropo(lla, el):
    """Saastamoinen with the standard atmosphere (as RTKLIB's default), metres"""
    h = max(lla[2], 0.0)
    if h > 10000 or el <= 0:
        return 0.0
    hum = 0.7
    p = 1013.25 * (1 - 2.2557e-5 * h) ** 5.2568
    T = 15.0 - 6.5e-3 * h + 273.16
    e = 6.108 * hum * math.exp((17.15 * T - 4684.0) / (T - 38.45))
    z = math.pi / 2 - el
    trph = 0.0022768 * p / (1 - 0.00266 * math.cos(2 * lla[0]) - 0.00028 * h / 1e3) / math.cos(z)
    trpw = 0.002277 * (1255.0 / T + 0.05) * e / math.cos(z)
    return trph + trpw


def klobuchar(t_gpst, lla, az, el, ion):
    """GPS broadcast ionosphere model (IS-GPS-200 20.3.3.5.2.5): L1 delay in metres"""
    a, b = ion[:4], ion[4:]
    psi = 0.0137 / (el / math.pi + 0.11) - 0.022
    phi = lla[0] / math.pi + psi * math.cos(az)
    phi = max(-0.416, min(0.416, phi))
    lam = lla[1] / math.pi + psi * math.sin(az) / math.cos(phi * math.pi)
    phi += 0.064 * math.cos((lam - 1.617) * math.pi)
    tt = (43200.0 * lam + t_gpst) % 86400.0
    f = 1.0 + 16.0 * (0.53 - el / math.pi) ** 3
    amp = max(a[0] + phi * (a[1] + phi * (a[2] + phi * a[3])), 0.0)
    per = max(b[0] + phi * (b[1] + phi * (b[2] + phi * b[3])), 72000.0)
    x = 2.0 * math.pi * (tt - 50400.0) / per
    return C * f * (5e-9 + (amp * (1.0 + x * x * (-0.5 + x * x / 24.0)) if abs(x) < 1.57 else 0.0))


# ---------------------------------------------------------------- bits
def _bits(hexstr, n):
    return [int(c) for c in bin(int(hexstr, 16))[2:].zfill(len(hexstr) * 4)][:n]


def _u(b, i, n):
    v = 0
    for k in range(n):
        v = (v << 1) | b[i + k]
    return v


def _s(b, i, n):
    v = _u(b, i, n)
    return v - (1 << n) if v & (1 << (n - 1)) else v


def gps_iono(bits240):
    """subframe 4 page 18 of a GPS L1 C/A frame (240 data bits, parity removed) -> the 8 Klobuchar numbers"""
    if _u(bits240, 0, 8) != 0x8B or _u(bits240, 24 + 19, 3) != 4 or _u(bits240, 48 + 2, 6) != 56:
        return None
    w3, w4, w5 = 48, 72, 96
    return [_s(bits240, w3 + 8, 8) * 2 ** -30, _s(bits240, w3 + 16, 8) * 2 ** -27,
            _s(bits240, w4, 8) * 2 ** -24, _s(bits240, w4 + 8, 8) * 2 ** -24,
            _s(bits240, w4 + 16, 8) * 2.0 ** 11, _s(bits240, w5, 8) * 2.0 ** 14,
            _s(bits240, w5 + 8, 8) * 2.0 ** 16, _s(bits240, w5 + 16, 8) * 2.0 ** 16]


# ---------------------------------------------------------------- WAAS (SBAS) corrections
def _igp_bands():
    """IGP grid points of bands 0-8 in mask order (DO-229 appendix A): each band 40 deg of longitude in 5-degree
    columns, south to north; columns on 10-degree longitudes go to +-75 (+85 N at 180W / 90W / 0 / 90E, -85 S at
    140W / 50W / 40E / 130E), the others +-55"""
    mid = list(range(-55, 60, 5))
    bands = []
    for b in range(9):
        pts = []
        for c in range(8):
            lon = -180 + 40 * b + 5 * c
            if lon % 10:
                lats = mid
            else:
                lats = [-75, -65] + mid + [65, 75]
                if lon in (-180, -90, 0, 90):
                    lats = lats + [85]
                elif lon in (-140, -50, 40, 130):
                    lats = [-85] + lats
            pts += [(lat, lon) for lat in lats]
        bands.append(pts)
    return bands


IGP_BANDS = _igp_bands()


class Sbas:
    """the corrections a WAAS satellite has sent so far"""

    def __init__(self):
        self.iodp, self.mask = None, []        # mask: slot (0-based) -> PRN number in the mask's own numbering
        self.fast = {}                          # PRN -> (prc m, udrei, t, iodp)
        self.lt = {}                            # PRN -> dict(iode, dx, dy, dz, daf0, ddx, ddy, ddz, daf1, t0, t)
        self.igp_mask = {}                      # band -> (iodi, [(lat, lon)] of the masked points)
        self.iono = {}                          # (lat, lon) -> (vertical delay m, givei, t)
        self.types = collections.Counter()

    def message(self, bits250, t):
        m = gnss.sbas_message(bits250)
        if not m:
            return
        mt, d = m
        self.types[mt] += 1
        try:
            if mt == 1:
                self.mask = [i + 1 for i in range(210) if d[i]]
                self.iodp = _u(d, 210, 2)
            elif mt in (2, 3, 4, 5):
                iodp = _u(d, 2, 2)
                for k in range(13):
                    self._fast(13 * (mt - 2) + k, _s(d, 4 + 12 * k, 12) * 0.125, _u(d, 160 + 4 * k, 4), t, iodp)
            elif mt == 24:
                iodp, blk = _u(d, 96, 2), _u(d, 98, 2)
                for k in range(6):
                    self._fast(13 * blk + k, _s(d, 12 * k, 12) * 0.125, _u(d, 72 + 4 * k, 4), t, iodp)
                self._longterm(d, 106, t)
            elif mt == 25:
                self._longterm(d, 0, t)
                self._longterm(d, 106, t)
            elif mt == 18:
                band, iodi = _u(d, 4, 4), _u(d, 8, 2)
                if band < 9:
                    self.igp_mask[band] = (iodi, [p for i, p in enumerate(IGP_BANDS[band]) if d[10 + i]])
            elif mt == 26:
                band, blk = _u(d, 0, 4), _u(d, 4, 4)
                iodi = _u(d, 8 + 15 * 13, 2)
                got = self.igp_mask.get(band)
                if got and got[0] == iodi:
                    pts = got[1]
                    for k in range(15):
                        j = 15 * blk + k
                        if j < len(pts):
                            v, givei = _u(d, 8 + 13 * k, 9), _u(d, 8 + 13 * k + 9, 4)
                            if v == 511 or givei == 15:
                                self.iono.pop(pts[j], None)
                            else:
                                self.iono[pts[j]] = (v * 0.125, givei, t)
        except IndexError:
            pass

    def _fast(self, slot, prc, udrei, t, iodp):
        if iodp != self.iodp or slot >= len(self.mask):
            return
        prn = self.mask[slot]
        if udrei >= 14:         # 14 = not monitored, 15 = do not use
            self.fast.pop(prn, None)
        else:
            self.fast[prn] = (prc, udrei, t, iodp)

    def _longterm(self, d, i, t):
        vel = d[i]
        halves = []
        if vel == 0:
            for k in range(2):
                j = i + 1 + 51 * k
                halves.append(dict(slot=_u(d, j, 6), iode=_u(d, j + 6, 8), dx=_s(d, j + 14, 9) * 0.125,
                                   dy=_s(d, j + 23, 9) * 0.125, dz=_s(d, j + 32, 9) * 0.125,
                                   daf0=_s(d, j + 41, 10) * 2 ** -31, ddx=0.0, ddy=0.0, ddz=0.0, daf1=0.0, t0=None))
            iodp = _u(d, i + 103, 2)
        else:
            j = i + 1
            halves.append(dict(slot=_u(d, j, 6), iode=_u(d, j + 6, 8), dx=_s(d, j + 14, 11) * 0.125,
                               dy=_s(d, j + 25, 11) * 0.125, dz=_s(d, j + 36, 11) * 0.125,
                               daf0=_s(d, j + 47, 11) * 2 ** -31, ddx=_s(d, j + 58, 8) * 2 ** -11,
                               ddy=_s(d, j + 66, 8) * 2 ** -11, ddz=_s(d, j + 74, 8) * 2 ** -11,
                               daf1=_s(d, j + 82, 8) * 2 ** -39, t0=_u(d, j + 90, 13) * 16.0))
            iodp = _u(d, j + 103, 2)
        if iodp != self.iodp:
            return
        for h in halves:
            if 1 <= h['slot'] <= len(self.mask):
                h['t'] = t
                self.lt[self.mask[h['slot'] - 1]] = h

    # -- use
    def fast_corr(self, prn, t):
        f = self.fast.get(prn)
        return f[0] if f and t - f[2] <= FAST_TIMEOUT_S else None

    def fast_var(self, prn, t):
        """the variance (m^2) WAAS gives for a satellite's corrected range (its UDREI), or None"""
        f = self.fast.get(prn)
        return UDRE_VAR[f[1]] if f and t - f[2] <= FAST_TIMEOUT_S and f[1] < len(UDRE_VAR) else None

    def longterm(self, prn, iode, t_gpst):
        """(dpos m (3,), dclock s) for a GPS satellite whose ephemeris has this IODE, or None"""
        h = self.lt.get(prn)
        if not h or h['iode'] != iode or t_gpst - h['t'] > LT_TIMEOUT_S:
            return None
        dt = 0.0
        if h['t0'] is not None:     # t0 is a GPS time of day
            dt = ((t_gpst % 86400.0) - h['t0'] + 43200.0) % 86400.0 - 43200.0
        return (np.array([h['dx'] + h['ddx'] * dt, h['dy'] + h['ddy'] * dt, h['dz'] + h['ddz'] * dt]),
                h['daf0'] + h['daf1'] * dt)

    def iono_delay(self, lla, az, el, t):
        """slant L1 ionosphere delay (m) from the grid, or None if the pierce point can't be interpolated"""
        got = self.iono_delay_var(lla, az, el, t)
        return got[0] if got else None

    def iono_delay_var(self, lla, az, el, t):
        """(slant L1 delay m, its variance m^2 from the grid's GIVEI) at the pierce point, or None.
        All 4 corners of the 5-degree cell: bilinear. One corner missing: the triangle of the other 3, if the pierce
        point lies in it (DO-229 A.4.4.10.3) - before, one missing corner sent 1.9 % of the ranges to the much
        rougher broadcast (Klobuchar) model"""
        re = 6378136.3
        psi = math.pi / 2 - el - math.asin(min(1.0, re / (re + IONO_H) * math.cos(el)))
        lat = math.asin(max(-1.0, min(1.0, math.sin(lla[0]) * math.cos(psi)
                                      + math.cos(lla[0]) * math.sin(psi) * math.cos(az))))
        lon = lla[1] + math.asin(max(-1.0, min(1.0, math.sin(psi) * math.sin(az) / math.cos(lat))))
        la, lo = math.degrees(lat), (math.degrees(lon) + 180) % 360 - 180
        if abs(la) > 55:
            return None
        la1, lo1 = math.floor(la / 5) * 5, math.floor(lo / 5) * 5
        x, y = (lo - lo1) / 5.0, (la - la1) / 5.0
        pts = []                                # (x, y, delay, GIVE variance) of the corners we have
        for cx, cy in ((0, 0), (1, 0), (0, 1), (1, 1)):
            p = (la1 + 5 * cy, (lo1 + 5 * cx + 180) % 360 - 180)
            v = self.iono.get(p)
            if v and t - v[2] <= IONO_TIMEOUT_S:
                pts.append((cx, cy, v[0], GIVE_VAR[v[1]] if v[1] < len(GIVE_VAR) else GIVE_VAR[-1]))
        if len(pts) == 4:
            wts = [(1 - x) * (1 - y), x * (1 - y), (1 - x) * y, x * y]
        elif len(pts) == 3:
            (x1, y1), (x2, y2), (x3, y3) = [(p[0], p[1]) for p in pts]
            d = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
            w1 = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / d
            w2 = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / d
            wts = [w1, w2, 1.0 - w1 - w2]
            if min(wts) < -1e-9:                # outside the triangle: no interpolation
                return None
        else:
            return None
        vert = sum(w * p[2] for w, p in zip(wts, pts))
        var = sum(w * p[3] for w, p in zip(wts, pts))
        fpp = 1.0 / math.sqrt(1.0 - (re * math.cos(el) / (re + IONO_H)) ** 2)
        return fpp * vert, fpp * fpp * var


# ---------------------------------------------------------------- the solver
class Solver:
    """modes: iono 'klobuchar' | 'sbas' | 'none'; sbas_orbit (fast + long-term corrections); smooth (carrier)"""

    def __init__(self, iono='sbas', sbas_orbit=True, smooth=True, systems='GE', settle=None, weights='simple'):
        self.iono_mode, self.sbas_orbit, self.smooth, self.systems = iono, sbas_orbit, smooth, systems
        self.settle = SETTLE_S if settle is None else settle
        self.weights = weights          # 'simple' (noise by smoothing and elevation) or 'budget' (see the constants)
        self.ephs = {}                  # sat -> {iode: eph} (WAAS long-term corrections name the IODE they fix)
        self.ion = None                 # Klobuchar numbers
        self.sbas = {}                  # WAAS PRN -> Sbas (each GEO's own corrections; the freshest is used)
        self.pending, self.t_epoch = [], None
        self.hatch = {}                 # sat -> [smoothed P, last P, last L, n]
        self.x = np.array([-1.3e6, -4.9e6, 4.0e6])     # (a start in North America; it converges from anywhere)
        self.prior = None               # (ellipsoid height m, sigma m, ECEF of where it was learned) - set_prior()

    def set_prior(self, h, lat_deg, lon_deg, sigma=PRIOR_SIGMA_M):
        """the static antenna's ellipsoid height, from this solver's own earlier fixes (None: no prior)"""
        if h is None:
            self.prior = None
            return
        ref = lla_to_ecef(math.radians(lat_deg), math.radians(lon_deg), h)
        self.prior = (float(h), float(sigma), ref)
        if np.linalg.norm(self.x - ref) > 1e5:      # start the solution there (it converges from anywhere anyway)
            self.x = ref.copy()

    def _prior_on(self):
        return self.prior is not None and np.linalg.norm(self.x - self.prior[2]) < PRIOR_RADIUS_M

    # -- inputs
    def eph(self, e):
        if e and e['sys'] in self.systems:
            d = self.ephs.setdefault(e['sat'], {})
            d[e['iode']] = e
            if len(d) > 4:
                for k in sorted(d, key=lambda k: d[k]['toe'])[:-4]:
                    del d[k]

    def nav(self, line):
        f = line.strip().split(',')
        if len(f) < 8:
            return
        sat = f[2]
        try:
            n = int(f[6])
            bits = _bits(f[7], n)
        except ValueError:
            return
        if sat.startswith('G') and n >= 240:
            ion = gps_iono(bits)
            if ion:
                self.ion = ion
        elif sat.startswith('S') and n >= 250:
            prn = 100 + int(sat[1:])
            self.sbas.setdefault(prn, Sbas()).message(bits, self._t_of(float(f[1])))

    def _t_of(self, rt):
        """receiver time -> GPST seconds, from the last epoch's pairing"""
        if self.t_epoch is None:
            return 0.0
        return self.t_epoch[0] + (rt - self.t_epoch[1])

    def obs(self, line):
        """a $OBS line; when a new epoch starts, the previous one is solved and its solution returned"""
        f = line.strip().split(',')
        if len(f) < 15 or f[0] != '$OBS':
            return None
        try:
            t = calendar.timegm((int(f[2]), int(f[3]), int(f[4]), int(f[5]), int(f[6]), 0)) + float(f[7])
            o = {'sat': f[8], 'cn0': float(f[10]), 'P': float(f[11]), 'L': float(f[12]), 'D': float(f[13]),
                 'lli': int(f[14])}
        except ValueError:
            return None
        out = None
        if self.pending and abs(t - self.pending[0][0]) > 1e-3:
            # the finished epoch leaves the queue before it is solved: if solve() raises (a diverged solution can
            # reach math.sin(inf)), the next observation starts a fresh epoch instead of re-solving the same one for ever
            epoch, obs_list = self.pending[0][0], [p[1] for p in self.pending]
            self.pending = []
            out = self.solve(epoch, obs_list)
        self.t_epoch = (t, float(f[1]))
        if o['sat'][0] in self.systems and o['P'] > 1e7:
            self.pending.append((t, o))
        return out

    def flush(self):
        out = self.solve(self.pending[0][0], [p[1] for p in self.pending]) if self.pending else None
        self.pending = []
        return out

    # -- carrier smoothing
    def _smooth(self, obs):
        if not self.smooth:
            return {o['sat']: (o['P'], 1) for o in obs}
        lam = {o['sat']: C / FREQ[o['sat'][0]] for o in obs}
        # the code and carrier differ by a common amount each epoch (pocket_trk's receiver clock steering, up to a
        # millisecond at a time): that common part is taken from the code so the carrier carries only geometry
        diffs = []
        for o in obs:
            h = self.hatch.get(o['sat'])
            if h:
                diffs.append((o['P'] - h[1]) - lam[o['sat']] * (o['L'] - h[2]))
        common = float(np.median(diffs)) if diffs else 0.0
        out = {}
        for o in obs:
            s, h = o['sat'], self.hatch.get(o['sat'])
            if h and abs((o['P'] - h[1]) - lam[s] * (o['L'] - h[2]) - common) < SLIP_M and o['L'] != 0:
                age = h[4] + 1
                # a newly locked satellite's code range is still settling (pocket_trk's code loop pulling in):
                # smoothing starts only once it has been tracked SETTLE_S, or that start would stay in the average
                n = min(h[3] + 1, SMOOTH_N) if age > self.settle else 1
                ps = o['P'] / n + (n - 1) / n * (h[0] + lam[s] * (o['L'] - h[2]) + common)
            else:
                n, ps, age = 1, o['P'], 0
            self.hatch[s] = [ps, o['P'], o['L'], n, age]
            out[s] = (ps, n)
        for s in [s for s in self.hatch if s not in out]:
            del self.hatch[s]
        return out

    def _pick_eph(self, sat, t_gpst):
        d = self.ephs.get(sat)
        if not d:
            return None
        e = min(d.values(), key=lambda e: abs(t_gpst - e['toe_gpst']))
        return e if abs(t_gpst - e['toe_gpst']) < 4 * 3600 and e['svh'] == 0 else None

    def _sbas(self):
        """the WAAS satellite with the most complete corrections"""
        if not self.sbas:
            return None
        return max(self.sbas.values(), key=lambda s: (len(s.fast) + len(s.lt), len(s.iono)))

    # -- one epoch
    def solve(self, t, obs):
        sm = self._smooth(obs)
        sb = self._sbas() if (self.iono_mode == 'sbas' or self.sbas_orbit) else None
        meas = []
        for o in obs:
            e = self._pick_eph(o['sat'], t)
            if e is None:
                continue
            P, nsm = sm[o['sat']]
            # transmit time and the satellite there
            tx = t - P / C
            for _ in range(2):
                dts = float(np.atleast_1d(gnss.sat_clock(e, gnss.gpst_to_unix(tx)))[0])
                tx = t - P / C - dts
            pos = gnss._kepler(e, np.atleast_1d(float(gnss.gpst_to_unix(tx))))[0][0].astype(float)
            dts = float(np.atleast_1d(gnss.sat_clock(e, gnss.gpst_to_unix(tx)))[0])
            corr = []
            svar = broadcast_var(e)         # how well the broadcast orbit and clock alone know this range
            if o['sat'][0] == 'G' and sb is not None and self.sbas_orbit:
                lt = sb.longterm(int(o['sat'][1:]), e['iode'], t)
                fc = sb.fast_corr(int(o['sat'][1:]), t)
                if lt is not None:
                    pos = pos + lt[0]
                    dts += lt[1]
                    corr.append('lt')
                if fc is not None:
                    P = P + fc
                    corr.append('fast')
                    fv = sb.fast_var(int(o['sat'][1:]), t)
                    if fv is not None and lt is not None:
                        svar = fv           # corrected by WAAS: its own (much smaller) figure
            # the group delay the broadcast clock needs on this signal: GPS TGD (L1 C/A); Galileo E1B BGD(E1,E5b)
            # (rtcm3 stores that one for I/NAV ephemerides)
            tgd = e.get('tgd', 0.0)
            meas.append({'sat': o['sat'], 'sys': o['sat'][0], 'P': P, 'pos': pos, 'dts': dts - tgd,
                         'cn0': o['cn0'], 'nsm': nsm, 'corr': corr, 'svar': svar})
        prior = self._prior_on()
        out = self._lsq(t, meas, sb, prior)
        if out is not None and prior and out['nsat'] >= 5:
            # the same epoch without the height row: what the height prior is checked and learned against
            free = self._lsq(t, meas, sb, False, probe=True)
            if free is not None:
                out['h_free'], out['vdop_free'], out['ok_free'] = free['h'], free['vdop'], free['ok']
        return out

    def _var(self, m, el, ion_src, ion, ion_var):
        """a range's variance (m^2): receiver noise (smaller once carrier-smoothed, larger low down) and, with the
        error budget, what the orbit/clock (WAAS UDRE, else GPS URA / Galileo SISA), ionosphere and troposphere
        leave over"""
        se = max(math.sin(el), 0.1)
        noise = (0.3 + 3.0 / math.sqrt(max(m['nsm'], 1))) * (1 + 1 / se)
        if self.weights != 'budget':
            return noise ** 2
        var = noise ** 2 + m['svar']
        if ion_src == 'sbas':
            var += ion_var
        elif ion_src == 'klob':
            var += (0.5 * ion) ** 2         # (the broadcast model takes out about half of the delay)
        else:
            var += 25.0
        mt = 1.001 / math.sqrt(0.002001 + se * se)      # troposphere: 0.12 m at the zenith (DO-229 J.2.4)
        return var + (0.12 * mt) ** 2

    def _lsq(self, t, meas, sb, use_prior=False, probe=False):
        """one epoch's least squares. use_prior: the static antenna's height row (PRIOR_SIGMA_M) - then 3 satellites
        give a (quick) fix and a lone Galileo satellite rides on the GPS clock; probe: a side solution (the
        prior-free check), which leaves the solver's state alone"""
        min_sats = 3 if use_prior else 4
        if len(meas) < min_sats:
            return None
        x = self.x.copy()
        counts = collections.Counter(m['sys'] for m in meas)
        # a second system costs one more unknown (its own clock): only with 2+ of its satellites - or, with the
        # height prior, one Galileo satellite on the GPS clock (their offset measured within 1 m: a bigger sigma)
        sysl = ['G'] + [s for s in sorted(counts) if s != 'G' and counts[s] >= 2]
        lone = {s for s in counts if s == 'E' and counts[s] == 1 and use_prior}
        nclk = len(sysl)
        clk = np.zeros(nclk)
        drop = set()
        res = None
        H = v = w = used = None
        nsat_rows = 0
        for it in range(12):
            lla = ecef_to_lla(x)
            H, v, w, used = [], [], [], []
            for m in meas:
                s = m['sys']
                if m['sat'] in drop or (s not in sysl and s not in lone):
                    continue
                tau = np.linalg.norm(m['pos'] - x) / C
                a = OMGE * tau          # the Earth turned while the signal travelled
                ps = np.array([m['pos'][0] * math.cos(a) + m['pos'][1] * math.sin(a),
                               -m['pos'][0] * math.sin(a) + m['pos'][1] * math.cos(a), m['pos'][2]])
                r = np.linalg.norm(ps - x)
                az, el = azel(x, ps, lla) if it > 0 or np.linalg.norm(x) > 6e6 else (0.0, math.pi / 2)
                if it > 1 and math.degrees(el) < EL_MASK:
                    continue
                ion, ion_src, ion_var = 0.0, '-', 0.0
                if self.iono_mode == 'sbas' and sb is not None:
                    d = sb.iono_delay_var(lla, az, el, t)
                    if d is not None:
                        ion, ion_var, ion_src = d[0], d[1], 'sbas'
                if ion_src == '-' and self.iono_mode in ('sbas', 'klobuchar') and self.ion:
                    ion, ion_src = klobuchar(t, lla, az, el, self.ion), 'klob'
                trp = tropo(lla, el) if it > 0 else 0.0
                k = sysl.index(s) if s in sysl else 0
                model = r + clk[k] - C * m['dts'] + ion + trp
                row = list(-(ps - x) / r) + [1.0 if j == k else 0.0 for j in range(nclk)]
                H.append(row)
                v.append(m['P'] - model)
                var = self._var(m, el, ion_src, ion, ion_var) + (LONE_GAL_VAR if s in lone else 0.0)
                w.append(1 / var)
                used.append((m, az, el, ion_src, ion, trp))
            nsat_rows = len(H)
            if use_prior and nsat_rows >= 2:        # the height: one more 'measurement' of the up direction
                lat, lon = lla[0], lla[1]
                H.append([math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)] + [0.0] * nclk)
                v.append(self.prior[0] - lla[2])
                w.append(1 / self.prior[1] ** 2)
            if len(H) < 3 + nclk or nsat_rows < min_sats:
                return None
            H, v, W = np.array(H), np.array(v), np.diag(w)
            try:
                N = np.linalg.inv(H.T @ W @ H)
            except np.linalg.LinAlgError:
                return None
            dx = N @ H.T @ W @ v
            if not np.all(np.isfinite(dx)) or np.linalg.norm(dx[:3]) > 1e7:
                if not probe:
                    self.x = np.array([-1.3e6, -4.9e6, 4.0e6])     # diverged: start the next epoch from scratch
                return None
            x = x + dx[:3]
            clk = clk + dx[3:]
            if np.linalg.norm(dx[:3]) < 1e-3 and it > 2:
                res = v - H @ dx
                # one bad satellite (multipath, wrong correction): drop the worst if it stands out and there is
                # redundancy to tell which one it is (the height row counts; it is never dropped itself)
                k = int(np.argmax(np.abs(res[:nsat_rows]) * np.sqrt(w[:nsat_rows])))
                if len(H) > 4 + nclk and abs(res[k]) * math.sqrt(w[k]) > 4.0 and len(drop) < 2:
                    drop.add(used[k][0]['sat'])
                    continue
                break
        if not probe:
            self.x = x
        lat, lon, h = ecef_to_lla(x)
        # geometry (DOP) in east-north-up, the height row included: it is what holds a 4-satellite fix together
        R = np.array([[-math.sin(lon), math.cos(lon), 0],
                      [-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat)],
                      [math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)]])
        G = np.array([list(R @ np.array(row[:3])) + list(row[3:]) for row in H])
        try:
            Q = np.linalg.inv(G.T @ G)
            hdop, vdop = math.sqrt(Q[0, 0] + Q[1, 1]), math.sqrt(Q[2, 2])
        except (np.linalg.LinAlgError, ValueError):
            hdop = vdop = float('nan')
        corrs = collections.Counter(c for u in used for c in u[0]['corr'])
        smoothed = sum(1 for u in used if u[0]['nsm'] >= READY_SMOOTH)
        # a fix to show: good geometry, 4+ satellites, and (smoothing on) 4+ of them smoothed long enough to have
        # settled; 3 satellites with the height prior are only ever a quick fix (about twice as wide)
        ok = hdop <= HDOP_MAX and len(used) >= 4 and (not self.smooth or smoothed >= 4)
        return {'ok': ok, 't': t, 'lat': math.degrees(lat), 'lon': math.degrees(lon), 'h': h, 'nsat': len(used),
                'hdop': hdop, 'vdop': vdop, 'sats': [u[0]['sat'] for u in used], 'dropped': sorted(drop),
                'iono': collections.Counter(u[3] for u in used), 'fast': corrs.get('fast', 0), 'lt': corrs.get('lt', 0),
                'smoothed': smoothed, 'prior': bool(use_prior and nsat_rows >= 2),
                'res_rms': float(np.sqrt(np.mean(res[:nsat_rows] ** 2))) if res is not None else None}
