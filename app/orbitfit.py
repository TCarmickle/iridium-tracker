"""Satellite orbits computed only from our own observations.

Measurements used (all decoded by our antenna):
  position fixes  - the Earth-fixed x/y/z a satellite sends in its ring alerts (4 km steps)
  Doppler curves  - the frequency of every frame we received from it, which changes as it
                    moves toward / away from us (each pass gets its own unknown channel offset)
Only physics and fixed design values are added: Earth's gravity and oblateness (J2),
Earth's rotation, and Iridium's design inclination (86.4 deg) as a weak starting hint.

Model: a circular orbit whose plane slowly turns because of J2. Parameters at time t0:
  node (deg), inclination (deg), position in orbit u0 (deg), angular rate n (rad/s), radius a (km)
"""
import math

import numpy as np
from scipy.optimize import least_squares

MU, RE, J2, C = 398600.4418, 6378.137, 1.08262668e-3, 299792.458
F = 1 / 298.257223563
E2 = F * (2 - F)
CARRIER = 1626.0e6
INC_HINT, INC_HINT_SIGMA = math.radians(86.4), math.radians(1.0)   # Iridium design value
SIG_POS = 2.5      # km: position fixes are rounded to 4 km steps
SIG_HZ = 150.0     # Hz: frequency measurement noise
SIG_RADIUS = 6.0   # km: how far the orbit's average radius can sit from the measured radius
PLANE_MATCH_DEG = 3.0    # a one-fix orbit this close to a measured plane belongs to it...
PLANE_REJECT_DEG = 8.0   # ...as long as the other direction is at least this far from every plane


def gmst(t):
    jd = 2440587.5 + np.asarray(t) / 86400
    return np.radians((280.46061837 + 360.98564736629 * (jd - 2451545.0)) % 360)


def ecef_to_eci(xyz, t):
    th = gmst(t)
    x, y, z = xyz[..., 0], xyz[..., 1], xyz[..., 2]
    return np.stack([x * np.cos(th) - y * np.sin(th), x * np.sin(th) + y * np.cos(th), z], -1)


def eci_to_ecef(xyz, t):
    th = gmst(t)
    x, y, z = xyz[..., 0], xyz[..., 1], xyz[..., 2]
    return np.stack([x * np.cos(th) + y * np.sin(th), -x * np.sin(th) + y * np.cos(th), z], -1)


def site_xyz(lat, lon, h):
    la, lo = math.radians(lat), math.radians(lon)
    N = RE / math.sqrt(1 - E2 * math.sin(la) ** 2)
    return np.array([(N + h) * math.cos(la) * math.cos(lo), (N + h) * math.cos(la) * math.sin(lo), (N * (1 - E2) + h) * math.sin(la)])


def rate_for_radius(a, inc):
    """argument-of-latitude rate (rad/s) of a circular orbit of radius a, including J2"""
    n0 = math.sqrt(MU / a ** 3)
    k = J2 * (RE / a) ** 2
    s2 = math.sin(inc) ** 2
    return n0 * (1 + 1.5 * k * (1 - 1.5 * s2) + 0.75 * k * (4 - 5 * s2))


def model_eci(p, t0, t):
    node, inc, u0, n, a = p
    dt = np.asarray(t, float) - t0
    node_t = node - 1.5 * n * J2 * (RE / a) ** 2 * math.cos(inc) * dt
    u = u0 + n * dt
    cO, sO, ci, si, cu, su = np.cos(node_t), np.sin(node_t), math.cos(inc), math.sin(inc), np.cos(u), np.sin(u)
    return np.stack([a * (cO * cu - sO * su * ci), a * (sO * cu + cO * su * ci), a * su * si], -1)


class Orbit:
    def __init__(self, p, t0, cov, info):
        self.p, self.t0, self.cov, self.info = np.asarray(p, float), t0, cov, info

    def ecef(self, t):
        t = np.atleast_1d(np.asarray(t, float))
        return eci_to_ecef(model_eci(self.p, self.t0, t), t)

    def sigma_km(self, t):
        """1-sigma position uncertainty (km) at time(s) t, from the fit's covariance"""
        dt = np.atleast_1d(np.asarray(t, float)) - self.t0
        c = self.cov
        a = self.p[4]
        var_u = c[2, 2] + 2 * dt * c[2, 3] + dt * dt * c[3, 3]
        var_plane = c[0, 0] + c[1, 1]
        return a * np.sqrt(np.maximum(var_u, 0) + var_plane) + 1.0

    def plane_at(self, t):
        """(node, inclination) in radians of the orbit plane at time t (inertial frame)"""
        node, inc, _, n, a = self.p
        return node - 1.5 * n * J2 * (RE / a) ** 2 * math.cos(inc) * (t - self.t0), inc

    @property
    def plane_sigma_km(self):
        """1-sigma uncertainty of where the orbit plane sits, as km at orbit height"""
        return self.p[4] * math.sqrt(max(self.cov[0, 0] + self.cov[1, 1], 0))

    @property
    def period_min(self):
        return 2 * math.pi / self.p[3] / 60


def _range_rate(p, t0, t, home):
    r1 = eci_to_ecef(model_eci(p, t0, t), t)
    r2 = eci_to_ecef(model_eci(p, t0, t + 1.0), t + 1.0)
    return np.linalg.norm(r2 - home, axis=-1) - np.linalg.norm(r1 - home, axis=-1)


def fit(fixes, doppler_groups, home_lla, plane_nodes_at=None):
    """fixes: list of (unix t, [x, y, z] km Earth-fixed); doppler_groups: list of lists of (unix t, Hz).
    plane_nodes_at: optional function t -> nodes (rad) of orbit planes we have already measured; with
    a single fix it decides whether the satellite was heading north or south.
    Returns an Orbit, or None with a reason string if the data can't pin down one orbit."""
    if not fixes:
        return None, 'no position fix'
    home = site_xyz(*home_lla)
    ft = np.array([f[0] for f in fixes], float)
    fx = np.array([f[1] for f in fixes], float)
    order = np.argsort(ft)
    ft, fx = ft[order], fx[order]
    fi = ecef_to_eci(fx, ft)
    groups = [(np.array([d[0] for d in g], float), np.array([d[1] for d in g], float)) for g in doppler_groups if len(g) >= 3]

    # seed from the pass with the most fixes
    passes = np.split(np.arange(len(ft)), np.where(np.diff(ft) > 1200)[0] + 1)
    best_pass = max(passes, key=len)
    t0 = float(ft[best_pass[0]])
    r0 = fi[best_pass[0]]
    a0 = float(np.mean(np.linalg.norm(fi, axis=1)))
    seeds = []
    if len(best_pass) >= 2 and ft[best_pass[-1]] - ft[best_pass[0]] >= 20:
        h = np.cross(r0, fi[best_pass[-1]])
        h /= np.linalg.norm(h)
        inc = math.acos(h[2])
        node = math.atan2(h[0], -h[1])
        nvec = np.array([math.cos(node), math.sin(node), 0.0])
        u0 = math.atan2(np.dot(np.cross(nvec, r0), h), np.dot(nvec, r0))
        seeds.append((node, inc, u0))
    else:
        # one fix: the satellite could be heading north or south through it
        dec = math.asin(r0[2] / np.linalg.norm(r0))
        ra = math.atan2(r0[1], r0[0])
        su = max(-1.0, min(1.0, math.sin(dec) / math.sin(INC_HINT)))
        for u0 in (math.asin(su), math.pi - math.asin(su)):
            node = ra - math.atan2(math.cos(INC_HINT) * math.sin(u0), math.cos(u0))
            seeds.append((node, INC_HINT, u0))

    def residuals(p):
        node, inc, u0, n, a = p
        res = [((eci_to_ecef(model_eci(p, t0, ft), ft) - fx) / SIG_POS).ravel()]
        for gt, ghz in groups:
            pred = -_range_rate(p, t0, gt, home) / C * CARRIER
            d = ghz - pred
            res.append((d - d.mean()) / SIG_HZ)
        res.append([(inc - INC_HINT) / INC_HINT_SIGMA])
        n_hint = rate_for_radius(a, inc)
        res.append([(n - n_hint) / (n_hint * 1.5 * SIG_RADIUS / a)])
        res.append([(a - a0) / 20.0])
        return np.concatenate([np.atleast_1d(r) for r in res])

    results = []
    for node, inc, u0 in seeds:
        p0 = [node, inc, u0, rate_for_radius(a0, inc), a0]
        r = least_squares(residuals, p0, x_scale=[0.01, 0.01, 0.01, 1e-6, 5.0], max_nfev=400)
        results.append(r)
    results.sort(key=lambda r: r.cost)
    best = results[0]
    dof = max(1, len(best.fun) - 5)
    chi2 = 2 * best.cost / dof
    direction_from_plane = False
    if len(results) > 1 and results[1].cost < max(4 * best.cost, best.cost + 8):
        # both directions fit: keep the one whose orbit plane lines up with a plane we have measured
        nodes = list(plane_nodes_at(t0)) if plane_nodes_at else []

        def off(r):
            return min((abs((math.degrees(r.x[0] - n) + 180) % 360 - 180) for n in nodes), default=999)
        good = [r for r in results if off(r) < PLANE_MATCH_DEG]
        if len(good) != 1 or min(off(r) for r in results if r is not good[0]) < PLANE_REJECT_DEG:
            return None, 'direction unknown - needs a second fix or Doppler from the same pass'
        best, direction_from_plane = good[0], True
        chi2 = 2 * best.cost / dof
    J = best.jac
    try:
        cov = np.linalg.inv(J.T @ J) * max(1.0, chi2)
    except np.linalg.LinAlgError:
        return None, 'fit failed'
    p = best.x.copy()
    p[2] %= 2 * math.pi
    pos_res = np.linalg.norm(eci_to_ecef(model_eci(p, t0, ft), ft) - fx, axis=1)
    info = {'fixes': len(ft), 'passes': len(passes), 'doppler_groups': len(groups),
            'doppler_points': int(sum(len(g[0]) for g in groups)), 'first_obs': float(min(ft.min(), min((g[0].min() for g in groups), default=ft.min()))),
            'last_obs': float(max(ft.max(), max((g[0].max() for g in groups), default=ft.max()))),
            'fix_rms_km': float(np.sqrt(np.mean(pos_res ** 2))), 'chi2': float(chi2),
            'direction_from_plane': direction_from_plane}
    return Orbit(p, t0, cov, info), None
