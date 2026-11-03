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
from scipy.stats import f as f_dist

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


def fit(fixes, doppler_groups, home_lla, plane_nodes_at=None, carrier=CARRIER, inc_hint=INC_HINT):
    """fixes: list of (unix t, [x, y, z] km Earth-fixed); doppler_groups: list of lists of (unix t, Hz).
    plane_nodes_at: optional function t -> nodes (rad) of orbit planes we have already measured; with
    a single fix it decides whether the satellite was heading north or south.
    carrier: the frequency the Doppler shifts are for; inc_hint: the system's design inclination
    (radians) or None when it has none - a single fix then can't be fitted.
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
        if inc_hint is None:
            return None, 'needs a second fix from the same pass'
        dec = math.asin(r0[2] / np.linalg.norm(r0))
        ra = math.atan2(r0[1], r0[0])
        su = max(-1.0, min(1.0, math.sin(dec) / math.sin(inc_hint)))
        for u0 in (math.asin(su), math.pi - math.asin(su)):
            node = ra - math.atan2(math.cos(inc_hint) * math.sin(u0), math.cos(u0))
            seeds.append((node, inc_hint, u0))

    def residuals(p):
        node, inc, u0, n, a = p
        res = [((eci_to_ecef(model_eci(p, t0, ft), ft) - fx) / SIG_POS).ravel()]
        for gt, ghz in groups:
            pred = -_range_rate(p, t0, gt, home) / C * carrier
            d = ghz - pred
            res.append((d - d.mean()) / SIG_HZ)
        if inc_hint is not None:
            res.append([(inc - inc_hint) / INC_HINT_SIGMA])
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


# ---------- orbits from Doppler alone (satellites that don't report their position) ----------
# Each pass gives a Doppler curve with an unknown constant offset (the receiver's tuning error).
# One pass can't tell which side of us the satellite went by (the mirror orbit fits just as well),
# so an orbit is only returned when several passes pin down a single one - see DOP_* below.
DOP_SIG_HZ = 150.0          # Hz: nominal frequency noise per frame (scaled up when the fit says it's worse)
DOP_SIG_RADIUS = 10.0       # km: how far the orbit's radius may sit from the one its period implies
DOP_MIN_FRAMES = 3          # frames a pass needs before its curve is used (the offset eats one)
DOP_PASS_GAP = 1500.0       # s: Doppler groups closer than this are the same pass
DOP_MIN_PASSES = 2
DOP_MAX_RMS_HZ = 450.0      # residual must be near the measurement noise
DOP_MIN_DCHI2 = 25.0        # the best other orbit must fit this much worse (chi-square units)
DOP_MAX_SIGMA_KM = 75.0     # stated position uncertainty (1 sigma) at the last observation
DOP_SIGMA_SCALE = 1.5       # covariance inflation for what the circular model leaves out (eccentricity, drag)
DOP_DISTINCT_KM = 40.0      # search candidates further apart than this are different orbits
DOP_DISTINCT_MAHAL = 25.0   # final solutions further apart than this (squared, in the best fit's sigmas) are different
DOP_FIT_PROB = 0.999        # the passes must fit one orbit as well as they fit one by one (F-test at this level)
DOP_MIN_NOISE_DOF = 6       # frames beyond 5 per pass, summed: what the noise estimate needs
_trace = None               # tests: a list here collects the candidates of each search stage


def _eci_many(P, t0, t):
    """positions (M, N, 3) of M parameter sets P (M, 5) at times t (N,); t0 scalar or (M,)"""
    node, inc, u0, n, a = (P[:, k:k + 1] for k in range(5))
    dt = t[None, :] - np.reshape(t0, (-1, 1))
    node_t = node - 1.5 * n * J2 * (RE / a) ** 2 * np.cos(inc) * dt
    u = u0 + n * dt
    cO, sO, ci, si, cu, su = np.cos(node_t), np.sin(node_t), np.cos(inc), np.sin(inc), np.cos(u), np.sin(u)
    return np.stack([a * (cO * cu - sO * su * ci), a * (sO * cu + cO * su * ci), a * su * si], -1)


def _radius_for_rate(n, inc):
    """inverse of rate_for_radius (works on arrays)"""
    n = np.asarray(n, float)
    s2 = np.sin(inc) ** 2
    a = (MU / n ** 2) ** (1 / 3)
    for _ in range(3):
        k = J2 * (RE / a) ** 2
        a = (MU * (1 + 1.5 * k * (1 - 1.5 * s2) + 0.75 * k * (4 - 5 * s2)) ** 2 / n ** 2) ** (1 / 3)
    return a


class _DopData:
    """Doppler frames of the passes in use, with the site in the inertial frame at every frame"""

    def __init__(self, groups, home, carrier):
        self.groups, self.home, self.carrier = groups, home, carrier
        self.t = np.concatenate([g[0] for g in groups])
        self.f = np.concatenate([g[1] for g in groups])
        self.counts = np.array([len(g[0]) for g in groups])
        self.starts = np.concatenate([[0], np.cumsum(self.counts)[:-1]])
        hs = np.broadcast_to(home, (len(self.t), 3))
        self.h1, self.h2 = ecef_to_eci(hs, self.t), ecef_to_eci(hs, self.t + 1.0)
        self.up = self.h1 / np.linalg.norm(self.h1, axis=1, keepdims=True)

    def subset(self, idx):
        return _DopData([self.groups[i] for i in sorted(idx)], self.home, self.carrier)

    def resid(self, P, t0):
        """(M, N) offset-free residuals in Hz, and (M,) count of frames the orbit puts below the horizon"""
        r1 = _eci_many(P, t0, self.t)
        r2 = _eci_many(P, t0, self.t + 1.0)
        d1 = r1 - self.h1
        rng1 = np.linalg.norm(d1, axis=-1)
        pred = -(np.linalg.norm(r2 - self.h2, axis=-1) - rng1) / C * self.carrier
        d = self.f - pred
        mean = np.add.reduceat(d, self.starts, axis=1) / self.counts
        below = (np.sum(d1 * self.up, -1) / rng1 < -0.05).sum(1)
        return d - np.repeat(mean, self.counts, axis=1), below

    def score(self, P, t0, chunk=3000):
        """chi-square at the nominal noise, plus a heavy penalty for frames heard below the horizon"""
        P = np.atleast_2d(P)
        t0 = np.broadcast_to(np.asarray(t0, float), (len(P),))
        out = np.empty(len(P))
        for i in range(0, len(P), chunk):
            d, below = self.resid(P[i:i + chunk], t0[i:i + chunk])
            out[i:i + chunk] = np.sum(d * d, 1) / DOP_SIG_HZ ** 2 + 400.0 * below
        return out


def _flyby_seeds(t, f, carrier, R, alt_range, keep=8):
    """times of closest approach and cross-track angles that fit one pass, from a 'flyby' model
    (satellite on a circle around the Earth's centre, Earth's rotation only as a speed change)"""
    tcs = np.arange(t[0] - 420, t[-1] + 420, 10.0)
    betas = np.radians(np.arange(0, 31, 0.75))
    A = RE + np.linspace(alt_range[0], alt_range[1], 6)
    W = np.sqrt(MU / A ** 3)[:, None] * (1 + np.array([-0.06, -0.03, 0.0, 0.03, 0.06]))[None, :]
    cb = np.cos(betas)[:, None, None, None]
    a = A[None, :, None, None]
    w = W[None, :, :, None]
    best = []
    for tc in tcs:
        th = w * (t - tc)
        rho = np.sqrt(a * a + R * R - 2 * a * R * cb * np.cos(th))
        d = f + a * R * cb * np.sin(th) * w / rho / C * carrier
        d -= d.mean(-1, keepdims=True)
        ssr = np.sum(d * d, -1) / DOP_SIG_HZ ** 2 + 400.0 * ((a * cb * np.cos(th) - R) / rho < -0.05).sum(-1)
        ssr = ssr.reshape(len(betas), -1).min(1)      # best per cross-track angle: near and far geometries both survive
        best.extend(zip(ssr, [tc] * len(betas), betas))
    best.sort(key=lambda b: b[0])
    out = []
    for s, tc, b in best:
        if s > best[0][0] + max(60.0, 0.5 * best[0][0]) or len(out) >= keep:
            break
        if all(abs(tc - o[1]) > 25 or abs(b - o[2]) > math.radians(2.5) for o in out):
            out.append((s, tc, b))
    return [(tc, b) for _, tc, b in out]


def _flyby_fits(t, f, carrier, R, alt_range, seeds):
    """free 'flyby' fits to one pass on its own (4 shape numbers + offset), refined from the grid seeds:
    [(time of closest approach, cross-track angle, chi-square)], best first. The flyby model is good to
    ~30 Hz, so the best chi-square is what the noise alone leaves."""
    out = []
    for tc, b in seeds[:6]:
        def res(q):
            tc, b, a, w = q
            th = w * (t - tc)
            rho = np.sqrt(a * a + R * R - 2 * a * R * math.cos(b) * np.cos(th))
            d = f + a * R * math.cos(b) * np.sin(th) * w / rho / C * carrier
            return (d - d.mean()) / DOP_SIG_HZ
        a0 = RE + sum(alt_range) / 2
        r = least_squares(res, [tc, b, a0, math.sqrt(MU / a0 ** 3)], x_scale=[10, 0.01, 50, 1e-5], max_nfev=100)
        out.append((float(r.x[0]), abs(float(r.x[1])), 2 * float(r.cost)))
    out.sort(key=lambda x: x[2])
    keep = []
    for x in out:
        if x[2] <= out[0][2] + max(20.0, 0.5 * out[0][2]) and                 all(abs(x[0] - k[0]) > 8 or abs(x[1] - k[1]) > math.radians(0.7) for k in keep):
            keep.append(x)
    return keep


def _two_pass_seeds(f1, f2, T, home, alt_range):
    """orbits that pass each of two passes' closest-approach points at the measured cross-track angles:
    two planes (at most) for each side combination, height grid only for the plane's slow J2 turn;
    the period follows from the whole number of orbits between the passes"""
    alts = RE + np.arange(alt_range[0], alt_range[1] + 1, 50.0)
    jit = np.radians([-1.0, 0.0, 1.0])
    out = []
    for tc1, b1, _ in f1:
        h1 = ecef_to_eci(home, tc1)
        h1 /= np.linalg.norm(h1)
        for tc2, b2, _ in f2:
            h2 = ecef_to_eci(home, tc2)
            h2 /= np.linalg.norm(h2)
            dt = tc2 - tc1
            # every combination of side (sign), cross-track jitter, height and branch, vectorised
            S1, S2, J1, J2_, A, G = np.meshgrid([-1, 1], [-1, 1], jit, jit, alts, [-1, 1], indexing='ij')
            s1 = np.sin(S1 * (b1 + J1)).ravel()
            s2 = np.sin(S2 * (b2 + J2_)).ravel()
            a, g = A.ravel(), G.ravel()
            drift = np.zeros_like(a)
            ok = np.ones(len(a), bool)
            for _ in range(3):      # the plane turns (J2) between the passes: solve, turn, solve again
                cd, sd = np.cos(-drift), np.sin(-drift)
                h2r = np.stack([cd * h2[0] - sd * h2[1], sd * h2[0] + cd * h2[1], np.full_like(a, h2[2])], -1)
                c = h2r @ h1
                cr = np.cross(h1, h2r)
                crn = np.linalg.norm(cr, axis=1)
                al = (s1 - c * s2) / (1 - c * c)
                be = (s2 - c * s1) / (1 - c * c)
                base = al[:, None] * h1 + be[:, None] * h2r
                rest = 1 - np.sum(base * base, 1)
                ok = (rest > 0) & (crn > 0.05)
                nv = base + (g * np.sqrt(np.maximum(rest, 0)) / np.maximum(crn, 1e-9))[:, None] * cr
                inc = np.arccos(np.clip(nv[:, 2], -1, 1))
                drift = -1.5 * _rate_many(a, inc) * J2 * (RE / a) ** 2 * np.cos(inc) * dt
            nv, inc, a, drift = nv[ok], inc[ok], a[ok], drift[ok]
            node = np.arctan2(nv[:, 0], -nv[:, 1])
            P = np.stack([node, inc, np.zeros_like(a), _rate_many(a, inc), a], 1)
            u1 = _phase_at(P, tc1, tc1, home)
            u2 = _phase_at(P, tc1, tc2, home)
            m = np.round((P[:, 3] * dt - (u2 - u1)) / (2 * math.pi))
            n = (u2 - u1 + 2 * math.pi * m) / dt
            P[:, 2] = u1
            P = _with_rate(P, n)
            P = P[(np.abs(P[:, 4] - a) < 40) & (n > 0)]
            out.append(_to_epoch(P, np.full(len(P), tc1), T))
    return np.concatenate(out) if out else np.zeros((0, 5))


def _plane_seeds(tc, beta, home, alts):
    """orbits (node, inc, u, n, a at time tc) whose plane passes about beta from the site at tc, closest there"""
    h = ecef_to_eci(home, tc)
    h = h / np.linalg.norm(h)
    hxy, psi = math.hypot(h[0], h[1]), math.atan2(h[1], h[0])
    out = []
    db = math.radians(1)
    for inc in np.radians(np.arange(1.0, 111.0, 2.0)):
        for b in {beta, -beta, max(beta - db, 0), -max(beta - db, 0), beta + db, -beta - db}:
            q = (math.sin(b) - math.cos(inc) * h[2]) / (math.sin(inc) * hxy)
            if abs(q) > 1:
                continue
            for x in {math.asin(q), math.pi - math.asin(q)}:
                node = psi + x
                e1 = np.array([math.cos(node), math.sin(node), 0.0])
                e2 = np.array([-math.sin(node) * math.cos(inc), math.cos(node) * math.cos(inc), math.sin(inc)])
                u = math.atan2(h @ e2, h @ e1)
                out.extend((node, inc, u, rate_for_radius(RE + alt, inc), RE + alt) for alt in alts)
    return np.array(out)


def _to_epoch(P, t0, T):
    """the same orbits P (M, 5) given at times t0 (M,), re-expressed at the common time T"""
    P = P.copy()
    dt = T - t0
    P[:, 0] -= 1.5 * P[:, 3] * J2 * (RE / P[:, 4]) ** 2 * np.cos(P[:, 1]) * dt
    P[:, 2] += P[:, 3] * dt
    return P


def _phase_at(P, T, tk, home):
    """argument of latitude (M,) at time tk of the point of each orbit closest to the site"""
    h = ecef_to_eci(home, tk)
    h = h / np.linalg.norm(h)
    node = P[:, 0] - 1.5 * P[:, 3] * J2 * (RE / P[:, 4]) ** 2 * np.cos(P[:, 1]) * (tk - T)
    inc = P[:, 1]
    e1 = np.stack([np.cos(node), np.sin(node), 0 * node], -1)
    e2 = np.stack([-np.sin(node) * np.cos(inc), np.cos(node) * np.cos(inc), np.sin(inc)], -1)
    return np.arctan2(e2 @ h, e1 @ h)


def _with_rate(P, n):
    P = np.array(P, float)
    P[:, 3] = n
    P[:, 4] = _radius_for_rate(n, P[:, 1])
    return P


def _distinct(P, T, ts, km, limit):
    """indices of P (sorted best first) of up to limit orbits that sit more than km apart at times ts"""
    ts = np.asarray(ts)[np.unique(np.linspace(0, len(ts) - 1, 12).round().astype(int))]
    pos = _eci_many(P, T, ts)
    idx = np.arange(len(P))
    keep = []
    while len(idx) and len(keep) < limit:
        keep.append(int(idx[0]))
        far = np.max(np.linalg.norm(pos[idx] - pos[idx[0]], axis=-1), axis=1) > km
        idx = idx[far]
    return keep


_FD_STEP = np.array([1e-6, 1e-6, 1e-6, 1e-10, 1e-3])
_SCALE = np.array([0.01, 0.01, 0.01, 1e-6, 5.0])


def _rate_many(a, inc):
    k, s2 = J2 * (RE / a) ** 2, np.sin(inc) ** 2
    return np.sqrt(MU / a ** 3) * (1 + 1.5 * k * (1 - 1.5 * s2) + 0.75 * k * (4 - 5 * s2))


def _rows(data, Q, T, tied=False):
    """residuals (M, N + 1): Doppler in units of the nominal noise, then the radius-vs-period prior
    (tied: the rate is set from the radius instead, so the prior is zero)"""
    if tied:
        Q = Q.copy()
        Q[:, 3] = _rate_many(Q[:, 4], Q[:, 1])
    d, _ = data.resid(Q, T)
    nh = _rate_many(Q[:, 4], Q[:, 1])
    return np.concatenate([d / DOP_SIG_HZ, ((Q[:, 3] - nh) / (nh * 1.5 * DOP_SIG_RADIUS / Q[:, 4]))[:, None]], 1)


def _refine_many(data, P, T, iters=8, tied=False):
    """Levenberg-Marquardt on many candidate orbits at once (vectorised), so the search compares
    converged minima rather than grid points; tied: the rate follows the radius (one pass alone)"""
    free = [0, 1, 2, 4] if tied else [0, 1, 2, 3, 4]
    P = np.array(P, float)
    lam = np.full(len(P), 1e-2)
    r = _rows(data, P, T, tied)
    cost = np.sum(r * r, 1)
    for _ in range(iters):
        J = np.empty(r.shape + (len(free),))
        for k, j in enumerate(free):
            Q = P.copy()
            Q[:, j] += _FD_STEP[j]
            J[:, :, k] = (_rows(data, Q, T, tied) - r) / _FD_STEP[j] * _SCALE[j]
        A = np.einsum('mni,mnj->mij', J, J)
        g = np.einsum('mni,mn->mi', J, r)
        dg = np.einsum('mii->mi', A)
        step = -np.linalg.solve(A + (lam[:, None] * dg + 1e-12)[:, :, None] * np.eye(len(free)), g[:, :, None])[:, :, 0]
        Q = P.copy()
        Q[:, free] += step * _SCALE[free]
        Q[:, 4] = np.clip(Q[:, 4], RE + 100, RE + 3000)
        r2 = _rows(data, Q, T, tied)
        c2 = np.sum(r2 * r2, 1)
        ok = c2 < cost
        P[ok], r[ok], cost[ok] = Q[ok], r2[ok], c2[ok]
        lam = np.where(ok, lam / 3, lam * 4)
    if tied:
        P[:, 3] = _rate_many(P[:, 4], P[:, 1])
    return P


def _lsq(data, p, T, max_iter=60):
    def rows(Q):
        return _rows(data, Q, T)

    def jac(q):         # forward differences, all five in one vectorised call
        R = rows(np.vstack([q, q + np.diag(_FD_STEP)]))
        return ((R[1:] - R[0]) / _FD_STEP[:, None]).T
    p = np.array(p, float)
    p[4] = min(max(p[4], RE + 101), RE + 2999)
    return least_squares(lambda q: rows(q[None])[0], p, jac=jac, x_scale=[0.01, 0.01, 0.01, 1e-6, 5.0], max_nfev=max_iter,
                         bounds=([-np.inf] * 4 + [RE + 100], [np.inf] * 4 + [RE + 3000]))


def _normalise(p):
    """the same orbit with inclination in 0..180 deg and angles in 0..360"""
    p = np.array(p, float)
    p[1] = (p[1] + math.pi) % (2 * math.pi) - math.pi
    if p[1] < 0:            # tilting the plane by -i is the same as +i seen from the other node
        p[1], p[0], p[2] = -p[1], p[0] + math.pi, p[2] + math.pi
    p[0] %= 2 * math.pi
    p[2] %= 2 * math.pi
    return p


def _describe(alt, best):
    if abs(math.degrees(alt[1] - best[1])) > 5:
        return 'an orbit at %.0f deg inclination (instead of %.0f)' % (math.degrees(alt[1]), math.degrees(best[1]))
    if abs(alt[4] - best[4]) > 30:
        return 'an orbit %.0f km %s' % (abs(alt[4] - best[4]), 'higher' if alt[4] > best[4] else 'lower')
    return 'a slightly shifted orbit'


def fit_doppler(doppler_groups, home_lla, carrier, alt_range_km=(350, 900)):
    """An orbit from Doppler curves alone, for satellites that don't report their position.
    doppler_groups: list of passes, each a list of (unix t, measured Hz) with its own unknown constant
    offset; carrier: the nominal transmit frequency (Hz); alt_range_km: heights to search.
    Returns (Orbit, None, details) when the data pin down one orbit, else (None, reason, details);
    details holds the fit numbers behind the decision."""
    import time
    started = time.perf_counter()
    home = site_xyz(*home_lla)
    groups = []
    for g in doppler_groups:
        if len(g) >= DOP_MIN_FRAMES:
            g = sorted(g)
            groups.append((np.array([d[0] for d in g], float), np.array([d[1] for d in g], float)))
    details = {'groups_used': len(groups)}
    if not groups:
        return None, 'no pass with %d or more Doppler frames yet' % DOP_MIN_FRAMES, details
    groups.sort(key=lambda g: g[0][0])
    mids = np.array([g[0].mean() for g in groups])
    n_passes = 1 + int(np.sum(np.diff(mids) > DOP_PASS_GAP))
    details['passes'] = n_passes
    if n_passes < DOP_MIN_PASSES:
        return None, ("one pass can't tell which side of you the satellite went by (its mirror orbit on the "
                      "other side fits just as well) - needs a second pass"), details
    data = _DopData(groups, home, carrier)
    R = float(np.linalg.norm(home))
    # each pass on its own: times of closest approach and cross-track angles that fit it
    fly = [_flyby_seeds(*g, carrier, R, alt_range_km) if len(g[0]) > 3 else None for g in groups]
    fits = [_flyby_fits(*g, carrier, R, alt_range_km, f) if f else None for g, f in zip(groups, fly)]

    # 1. the pass with the most frames: orbits through each time of closest approach / distance that fits it
    ref = max(range(len(groups)), key=lambda i: (len(groups[i][0]), groups[i][0][-1] - groups[i][0][0]))
    alts = np.arange(alt_range_km[0], alt_range_km[1] + 1, 50.0)
    seeds = [(tc, _plane_seeds(tc, beta, home, alts)) for tc, beta in fly[ref]]
    T = float(np.median([s[0] for s in seeds]))
    P = np.concatenate([_to_epoch(s[1], np.full(len(s[1]), s[0]), T) for s in seeds])
    # one pass leaves a whole family (any heading, either side): keep its best height per geometry
    s = data.subset([ref]).score(P, T).reshape(-1, len(alts))
    j = np.argmin(s, 1)
    s = s[np.arange(len(s)), j]
    P = P.reshape(-1, len(alts), 5)[np.arange(len(s)), j]
    o = np.argsort(s)[:1500]
    d1 = data.subset([ref])
    P = _refine_many(d1, P[o], T, iters=6, tied=True)
    s = d1.score(P, T)
    o = np.argsort(s)
    o = o[s[o] <= s[o[0]] + max(30.0, 0.5 * s[o[0]])]
    P = P[o][_distinct(P[o], T, groups[ref][0], 25.0, 500)]
    if _trace is not None:
        _trace.append(('one pass', P, T, groups[ref][0]))

    # 2. a second pass (the fullest other one) fixes the plane and the period
    others = sorted((i for i in range(len(groups)) if i != ref), key=lambda i: abs(mids[i] - mids[ref]))
    anc = max((i for i in others if abs(mids[i] - mids[ref]) > DOP_PASS_GAP), key=lambda i: len(groups[i][0]))
    others.remove(anc)
    used = [ref, anc] + [i for i in others if abs(mids[i] - mids[ref]) <= DOP_PASS_GAP]
    others = [i for i in others if i not in used]
    # a) the one-pass family, timed so the satellite is closest to us near the second pass's own
    #    closest-approach times (fine steps), or anywhere near its middle (coarse steps)
    tcs = [f[0] for f in fits[anc] or []][:4]
    lo, hi = RE + alt_range_km[0] - 60, RE + alt_range_km[1] + 60
    P2 = []
    for tk, phis in [(mids[anc], np.arange(-30, 30.1, 3.0))] + [(tc, np.arange(-4.5, 4.6, 1.5)) for tc in tcs]:
        dt = tk - T
        u_req = _phase_at(P, T, tk, home)
        ms = sorted({math.floor(x) for x in (np.array([rate_for_radius(hi, 0), rate_for_radius(lo, 0)]) * abs(dt) / (2 * math.pi))})
        sgn = 1 if dt > 0 else -1
        P2 += [_with_rate(P, ((u_req + phi - P[:, 2]) * sgn % (2 * math.pi) + 2 * math.pi * m) / abs(dt))
               for m in range(ms[0] - 1, ms[-1] + 2) for phi in np.radians(phis)]
    P2 = np.concatenate(P2)
    P2 = P2[(P2[:, 4] > lo) & (P2[:, 4] < hi)]
    d2 = data.subset(used)
    P2 = P2[np.argsort(d2.score(P2, T))[:4000]]
    P2 = P2[_distinct(P2, T, d2.t, 10.0, 400)]
    # b) planes solved directly from both passes' closest approaches and cross-track angles
    if fits[ref] and fits[anc]:
        G = _two_pass_seeds(fits[ref], fits[anc], T, home, alt_range_km)
        G = G[(G[:, 4] > lo) & (G[:, 4] < hi)]
        if len(G):
            G = G[np.argsort(d2.score(G, T))[:2000]]
            P2 = np.concatenate([P2, G[_distinct(G, T, d2.t, 10.0, 300)]])
    if _trace is not None:
        _trace.append(('two passes', P2, T, d2.t))
    P2 = _refine_many(d2, P2, T, iters=10)
    P2 = P2[np.argsort(d2.score(P2, T))]
    sols = list(P2[_distinct(P2, T, d2.t, DOP_DISTINCT_KM, 12)])
    if _trace is not None:
        _trace.append(('two passes LSQ', np.array(sols), T, d2.t))

    # 3. add the other passes one at a time, nearest first, scanning the phase each one needs
    phis = np.radians(np.concatenate([np.arange(-45, 45.1, 1.0), 360 + np.arange(-12, 12.1, 2.0),
                                      -360 + np.arange(-12, 12.1, 2.0)]))
    for k in others:
        same_pass = min(abs(mids[k] - mids[i]) for i in used) <= DOP_PASS_GAP
        used = used + [k]
        dk = data.subset(used)
        dt = mids[k] - T
        new = []
        for p in sols:
            Pk = _with_rate(np.tile(p, (len(phis), 1)), p[3] + (0 if same_pass else phis / dt))
            sk = dk.score(Pk, T)
            o = np.argsort(sk)
            picks = [o[0]]          # the best phases, as long as they aren't clearly worse
            for j in o[1:]:
                if sk[j] - sk[o[0]] > 4 * DOP_MIN_DCHI2 or len(picks) >= 3:
                    break
                if all(abs(phis[j] - phis[q]) > math.radians(4) for q in picks):
                    picks.append(j)
            new.extend(Pk[j] for j in picks)
        new = _refine_many(dk, np.array(new), T, iters=10)
        new = new[np.argsort(dk.score(new, T))]
        sols = list(new[_distinct(new, T, dk.t, DOP_DISTINCT_KM, 10)])
        if _trace is not None:
            _trace.append(('%d passes' % len(used), np.array(sols), T, dk.t))

    # 4. every surviving solution against all the data, then the confidence gate
    fin = []
    for p in sols:
        r = _lsq(data, p, T)
        d, below = data.resid(r.x[None], T)
        fin.append((float(np.sum(d * d)), int(below[0]), r))
    fin.sort(key=lambda x: x[0] + 1e9 * x[1])
    ssr, below, best = fin[0]
    best = _lsq(data, _normalise(best.x), T)        # covariance in the tidy angles
    p = best.x
    N = len(data.t)
    rms = math.sqrt(ssr / max(1, N - len(groups) - 5))
    scale2 = max(DOP_SIG_HZ, rms) ** 2
    # how well each pass fits on its own: the noise level, and the yardstick for the one-orbit fit
    ssr_free = sum(f[0][2] for g, f in zip(groups, fits) if len(g[0]) > 5) * DOP_SIG_HZ ** 2
    dof_free = int(sum(max(len(g[0]) - 5, 0) for g in groups))
    df_diff = int(sum(min(len(g[0]), 5) for g in groups)) - 5 - len(groups)
    noise = math.sqrt(ssr_free / dof_free) if dof_free else float('nan')
    F = (ssr - ssr_free) / max(df_diff, 1) / (ssr_free / dof_free) if dof_free and ssr_free > 0 else float('inf')
    F_max = float(f_dist.ppf(DOP_FIT_PROB, max(df_diff, 1), dof_free)) if dof_free else float('inf')
    info_m = best.jac.T @ best.jac * DOP_SIG_HZ ** 2 / scale2
    try:
        cov = np.linalg.inv(info_m) * DOP_SIGMA_SCALE ** 2
    except np.linalg.LinAlgError:
        return None, 'fit failed', details
    # the best other orbit: a separate minimum that puts the satellite somewhere else, outside this fit's
    # own uncertainty (3 sigma) at some time from the first frame to a day after the last
    alt_p, dchi2 = None, float('inf')
    ts = np.concatenate([data.t, data.t[-1] + np.array([3, 6, 12, 24]) * 3600.0])
    sig_ts = Orbit(p, T, cov, {}).sigma_km(ts)
    pos_best = _eci_many(p[None], T, ts)[0]
    for s2, b2, r2 in fin[1:]:
        q = _normalise(r2.x)
        dp = q - p
        dp[[0, 2]] = (dp[[0, 2]] + math.pi) % (2 * math.pi) - math.pi
        apart = np.linalg.norm(_eci_many(q[None], T, ts)[0] - pos_best, axis=1)
        if dp @ info_m @ dp > DOP_DISTINCT_MAHAL and np.any(apart > np.maximum(3 * sig_ts, DOP_DISTINCT_KM)):
            dc = (s2 - ssr) / scale2 + 400.0 * (b2 - below)
            if dc < dchi2:
                dchi2, alt_p = dc, q
    info = {'fixes': 0, 'passes': n_passes, 'doppler_groups': len(groups), 'doppler_points': int(N),
            'first_obs': float(data.t.min()), 'last_obs': float(data.t.max()), 'fix_rms_km': 0.0,
            'chi2': float(rms ** 2 / DOP_SIG_HZ ** 2), 'direction_from_plane': False, 'doppler_only': True,
            'doppler_rms_hz': float(rms), 'dchi2_alternative': float(dchi2)}
    orbit = Orbit(p, T, cov, info)
    sig = float(orbit.sigma_km(data.t.max())[0])
    details.update({'rms_hz': rms, 'noise_hz': noise, 'F': F, 'F_max': F_max, 'noise_dof': dof_free, 'dchi2': dchi2, 'sigma_km': sig, 'inc_deg': math.degrees(p[1]),
                    'alt_km': p[4] - RE, 'solutions': len(fin), 'orbit': orbit, 'alternative': alt_p,
                    'seconds': time.perf_counter() - started})
    if below:
        return None, 'no orbit keeps the satellite above your horizon while it was heard', details
    if rms > DOP_MAX_RMS_HZ:
        return None, "the Doppler curves don't fit one orbit (%.0f Hz rms) - mixed-up signals?" % rms, details
    if dof_free < DOP_MIN_NOISE_DOF:
        return None, 'too few frames per pass to judge the fit against the noise - needs fuller passes', details
    if F > F_max:
        return None, "the passes don't fit one orbit as well as they fit one by one - mixed-up signals, or a missed orbit", details
    if not alt_range_km[0] - 50 <= p[4] - RE <= alt_range_km[1] + 50:
        return None, 'the best fit is at %.0f km, outside the expected heights' % (p[4] - RE), details
    if dchi2 < DOP_MIN_DCHI2:
        return None, '%s fits almost as well - needs another pass' % _describe(alt_p, p), details
    if sig > DOP_MAX_SIGMA_KM:
        return None, 'the orbit is still uncertain by %.0f km - needs another pass' % sig, details
    return orbit, None, details
