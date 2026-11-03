"""GNSS satellite orbits from the broadcast ephemerides our own receiver decodes.

PocketSDR's pocket_trk logs every ephemeris it decodes as a line
  $EPH,time,sat,sig,IODE,IODC,SVA,SVH,Toe,Toc,Ttr,A,e,i0,OMEGA0,omega,M0,delta-n,OMEGAdot,Idot,
       Crc,Crs,Cuc,Cus,Cic,Cis,Toes,Fit,Af0,Af1,Af2,TGD,code,flag
(src/sdr_pvt.c out_log_eph, which prints RTKLIB's eph_t). Toe/Toc/Ttr are whole seconds of RTKLIB
gtime_t, i.e. GPS time counted like Unix time (GPST = UTC + leap seconds); BeiDou and Galileo times are
already converted to GPST there. Toes is seconds of the week in the satellite's own time scale (BDT
for BeiDou) and goes into the node angle. Angles are radians, A is metres.

The positions use the satellites' own interface documents: IS-GPS-200 (GPS, QZSS), Galileo OS SIS
ICD (same Keplerian form, Galileo GM), BeiDou ICD with CGCS2000 constants and the extra rotation of
the BeiDou GEO satellites (PRN 1-5, 59-63). Nothing here is downloaded: the satellite itself sent
these numbers to our antenna.
"""
import math

import numpy as np

import orbitfit

C_LIGHT = 299792458.0
GPS_EPOCH = 315964800          # 1980-01-06 00:00:00 UTC as Unix time (GPS week 0)
BDT_WEEK0 = 1356               # BeiDou week 0 starts at GPS week 1356 (2006-01-01), BDT = GPST - 14 s
GST_WEEK0 = 1024               # Galileo week 0 starts at GPS week 1024 (1999-08-22), GST = GPST
BDS_GEO_PRNS = set(range(1, 6)) | set(range(59, 64))

# (GM m^3/s^2, Earth rotation rad/s) per system's interface document
CONSTANTS = {'G': (3.9860050e14, 7.2921151467e-5),     # IS-GPS-200 (also QZSS)
             'J': (3.9860050e14, 7.2921151467e-5),
             'E': (3.986004418e14, 7.2921151467e-5),   # Galileo OS SIS ICD
             'C': (3.986004418e14, 7.292115e-5)}       # BeiDou ICD, CGCS2000

# GPST - UTC (s) from each Unix time on; no leap second has been announced after 2017
LEAP_SECONDS = [(1341100800, 16), (1435708800, 17), (1483228800, 18)]

FIELDS = ['iode', 'iodc', 'sva', 'svh', 'toe_gpst', 'toc_gpst', 'ttr_gpst', 'A', 'e', 'i0', 'OMG0', 'omg', 'M0',
          'deln', 'OMGd', 'idot', 'crc', 'crs', 'cuc', 'cus', 'cic', 'cis', 'toes', 'fit', 'af0', 'af1', 'af2',
          'tgd', 'code', 'flag']
INT_FIELDS = {'iode', 'iodc', 'sva', 'svh', 'toe_gpst', 'toc_gpst', 'ttr_gpst', 'code', 'flag'}

# position uncertainty model (km), see EphemerisOrbit.sigma_km
SIG_FIT_KM = 0.005        # inside the fit interval broadcast orbits are good to a few metres
SIG_HOURS = 2.0           # ...which runs this long either side of Toe
SIG_RATE_KM = 0.1         # beyond it the error grows by this per hour past it...
SIG_GROWTH_KM = 0.005     # ...plus this times (hours past it)^2, for manoeuvres and model drift


def gpst_minus_utc(t_unix):
    t = np.asarray(t_unix, float)
    out = np.full(t.shape, 15.0)
    for start, leap in LEAP_SECONDS:
        out = np.where(t >= start, float(leap), out)
    return out


def gpst_to_unix(t_gpst):
    """RTKLIB-style GPST seconds -> Unix (UTC) seconds"""
    t = np.asarray(t_gpst, float)
    return t - gpst_minus_utc(t - 18.0)


def unix_to_gpst(t_unix):
    return np.asarray(t_unix, float) + gpst_minus_utc(t_unix)


def is_geo(eph):
    return eph['sys'] == 'C' and eph['prn'] in BDS_GEO_PRNS


def parse_eph(line):
    """One PocketSDR '$EPH,...' log line -> ephemeris dict, or None if it isn't one we can use
    (another log line, GLONASS, a malformed line)."""
    parts = line.strip().split(',')
    if len(parts) < 4 + len(FIELDS) or parts[0] != '$EPH':
        return None
    sat, sig = parts[2], parts[3]
    if not sat or sat[0] not in CONSTANTS:
        return None
    try:
        prn = int(sat[1:])
        eph = {'rcv_time': float(parts[1])}
        for name, val in zip(FIELDS, parts[4:4 + len(FIELDS)]):
            eph[name] = int(val) if name in INT_FIELDS else float(val)
    except ValueError:
        return None
    if eph['A'] <= 0:
        return None
    return finish_eph(eph, sat[0], prn, sig)


def finish_eph(eph, sys, prn, sig=''):
    """Adds the derived keys to a dict holding the FIELDS values (as $EPH prints them)."""
    eph.update({'sys': sys, 'prn': prn, 'sat': '%s%02d' % (sys, prn), 'sig': sig,
                'sqrtA': math.sqrt(eph['A']), 'health': eph['svh'],
                'toe': float(gpst_to_unix(eph['toe_gpst'])), 'toc': float(gpst_to_unix(eph['toc_gpst'])),
                'ttr': float(gpst_to_unix(eph['ttr_gpst']))})
    t_sys = eph['toe_gpst'] - (14 if sys == 'C' else 0)          # in the satellite's own time scale
    week = int((t_sys - GPS_EPOCH) // 604800)
    eph['week'] = week - (BDT_WEEK0 if sys == 'C' else GST_WEEK0 if sys == 'E' else 0)
    eph['geo'] = is_geo(eph)
    return eph


def _kepler(eph, t_unix):
    """Satellite position (m, ECEF) and eccentric anomaly at Unix time(s) t."""
    mu, omge = CONSTANTS[eph['sys']]
    tk = unix_to_gpst(t_unix) - eph['toe_gpst']
    A = eph['A']
    n = math.sqrt(mu / A ** 3) + eph['deln']
    M = eph['M0'] + n * tk
    e = eph['e']
    E = M.copy()
    for _ in range(30):
        dE = (E - e * np.sin(E) - M) / (1 - e * np.cos(E))
        E -= dE
        if np.all(np.abs(dE) < 1e-14):
            break
    nu = np.arctan2(math.sqrt(1 - e * e) * np.sin(E), np.cos(E) - e)
    phi = nu + eph['omg']
    s2, c2 = np.sin(2 * phi), np.cos(2 * phi)
    u = phi + eph['cus'] * s2 + eph['cuc'] * c2
    r = A * (1 - e * np.cos(E)) + eph['crs'] * s2 + eph['crc'] * c2
    i = eph['i0'] + eph['idot'] * tk + eph['cis'] * s2 + eph['cic'] * c2
    x, y = r * np.cos(u), r * np.sin(u)
    ci, si = np.cos(i), np.sin(i)
    if eph['geo']:
        # BeiDou GEO: the elements are in a frame tilted by 5 deg and not rotating with the Earth
        O = eph['OMG0'] + eph['OMGd'] * tk - omge * eph['toes']
        cO, sO = np.cos(O), np.sin(O)
        xg, yg, zg = x * cO - y * ci * sO, x * sO + y * ci * cO, y * si
        so, co = np.sin(omge * tk), np.cos(omge * tk)
        c5, s5 = math.cos(math.radians(-5)), math.sin(math.radians(-5))
        pos = np.stack([xg * co + yg * so * c5 + zg * so * s5,
                        -xg * so + yg * co * c5 + zg * co * s5,
                        -yg * s5 + zg * c5], -1)
    else:
        O = eph['OMG0'] + (eph['OMGd'] - omge) * tk - omge * eph['toes']
        cO, sO = np.cos(O), np.sin(O)
        pos = np.stack([x * cO - y * ci * sO, x * sO + y * ci * cO, y * si], -1)
    return pos, E


def sat_ecef(eph, t_unix):
    """Earth-fixed position (km) and velocity (km/s) at Unix time(s) t: (3,) each for a scalar t,
    (n, 3) for an array."""
    t = np.asarray(t_unix, float)
    tt = np.atleast_1d(t)
    pos = _kepler(eph, tt)[0]
    vel = (_kepler(eph, tt + 0.5)[0] - _kepler(eph, tt - 0.5)[0])
    if t.ndim == 0:
        pos, vel = pos[0], vel[0]
    return pos / 1000.0, vel / 1000.0


def sat_clock(eph, t_unix):
    """Satellite clock offset (s) at Unix time(s) t, relativistic term included (TGD not applied)."""
    mu = CONSTANTS[eph['sys']][0]
    dt = unix_to_gpst(t_unix) - eph['toc_gpst']
    E = _kepler(eph, np.atleast_1d(np.asarray(t_unix, float)))[1]
    rel = -2 * math.sqrt(mu * eph['A']) * eph['e'] * np.sin(E) / C_LIGHT ** 2
    out = eph['af0'] + eph['af1'] * dt + eph['af2'] * dt * dt + rel.reshape(np.shape(dt))
    return out


def _plane(r, v):
    """(node, inclination, argument of latitude) of the inertial state r, v (any units)"""
    h = np.cross(r, v)
    h /= np.linalg.norm(h, axis=-1, keepdims=True)
    inc = np.arccos(np.clip(h[..., 2], -1, 1))
    node = np.arctan2(h[..., 0], -h[..., 1])
    nvec = np.stack([np.cos(node), np.sin(node), np.zeros_like(node)], -1)
    u = np.arctan2(np.sum(np.cross(nvec, r) * h, -1), np.sum(nvec * r, -1))
    return node, inc, u


def _inertial(eph, t):
    """position (km) and velocity (km/s) in orbitfit's inertial frame (Earth turned back by GMST)"""
    t = np.atleast_1d(np.asarray(t, float))
    p, v = sat_ecef(eph, t)
    w = 2 * math.pi * 1.00273781191135448 / 86400      # the rate orbitfit.gmst turns at
    ri = orbitfit.ecef_to_eci(p, t)
    vi = orbitfit.ecef_to_eci(v, t) + w * np.stack([-ri[:, 1], ri[:, 0], np.zeros(len(t))], -1)
    return ri, vi


class EphemerisOrbit:
    """A satellite's broadcast ephemeris, wrapped to look like orbitfit.Orbit to the rest of the app.

    p = [node, inclination, u0, n, a] at t0 = Toe like orbitfit's circular model: node/inclination/
    argument of latitude of the inertial state at Toe (rad), n the mean angular rate (rad/s), a the
    semi-major axis (km). ecef() itself always uses the full broadcast model, not p."""

    def __init__(self, eph, received=None):
        self.eph = eph
        self.t0 = eph['toe']
        mu = CONSTANTS[eph['sys']][0]
        n = math.sqrt(mu / eph['A'] ** 3) + eph['deln']
        ri, vi = _inertial(eph, self.t0)
        node, inc, u0 = (float(x[0]) for x in _plane(ri, vi))
        self.p = np.array([node, inc, u0 % (2 * math.pi), n, eph['A'] / 1000.0])
        self.cov = None
        t_rx = eph['ttr'] if received is None else received
        self.info = {'fixes': 0, 'passes': 1, 'doppler_groups': 0, 'doppler_points': 0,
                     'first_obs': float(t_rx), 'last_obs': float(t_rx), 'fix_rms_km': 0.0,
                     'ephemeris': True, 'sat': eph['sat'], 'sig': eph['sig'], 'toe': eph['toe'],
                     'health': eph['health'], 'geo': eph['geo']}

    def ecef(self, t):
        return sat_ecef(self.eph, np.atleast_1d(np.asarray(t, float)))[0]

    def sigma_km(self, t):
        """1-sigma position uncertainty (km) at time(s) t. Model: SIG_FIT_KM within SIG_HOURS of Toe
        (the fit interval: GPS 4 h, Galileo and BeiDou are sent fresher still), then growing by
        SIG_RATE_KM per hour beyond it plus SIG_GROWTH_KM * hours^2. Checked against precise (SP3)
        orbits for 96 GPS/Galileo/BeiDou satellites over -1..+4 days: the error is 1-3 m in the fit
        interval, worst ~0.2 km at +-4 h, ~3 km at a day (BeiDou GEO/IGSO; 0.6 km median), ~15 km at
        4 days, all but 0.3 % inside 3 sigma. The model is 4.6 km at a day, 54 km at 4 days, 150 km
        (drawn faint) after about a week and 1500 km (hidden) after about three weeks."""
        x = np.maximum(np.abs(np.atleast_1d(np.asarray(t, float)) - self.t0) / 3600.0 - SIG_HOURS, 0.0)
        return SIG_FIT_KM + SIG_RATE_KM * x + SIG_GROWTH_KM * x * x

    def plane_at(self, t):
        """(node, inclination) in radians of the orbit plane at time t, in orbitfit's inertial frame"""
        ri, vi = _inertial(self.eph, t)
        node, inc, _ = _plane(ri, vi)
        if np.ndim(t) == 0:
            return float(node[0]), float(inc[0])
        return node, inc

    @property
    def plane_sigma_km(self):
        return SIG_FIT_KM

    @property
    def period_min(self):
        return 2 * math.pi / self.p[3] / 60
