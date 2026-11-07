"""Named reference positions (like a handheld's averaged waypoints) and the local sea-level offset.

References (data\\references.json): a position known better than one session gives - the tracker's own long-term
average, a Garmin's averaged waypoint, or a stretch where a Garmin sat still beside the antenna. The 'Your position'
line measures its fixes against the nearest one (within NEAR_M), so accuracy is checked against something
independent of the fixes themselves.

Geoid (data\\geoid.json): how far the WGS84 ellipsoid (GPS's model, where the fixes' heights are measured from) is
above sea level here - a Garmin reports it with each position (garmin_usb.py writes it); used to show sea-level
heights and to write GPX."""
import json
import math
import os
import time

import numpy as np

import paths

REFS = os.path.join(paths.DATA, 'references.json')
GEOID = os.path.join(paths.DATA, 'geoid.json')
NEAR_M = 500.0
STILL_M = 30.0          # a Garmin track counts as sitting still while within this of its stretch's middle (indoors
#                         its fixes wander ~20 m; someone walking leaves 30 m in ~20 s)...
STILL_MIN_S = 180.0     # ...for at least this long
# A stretch is good enough to measure against only with dense points or a tight scatter: of 39 stretches imported by
# 2026-11-06, 35 were indoors, one point per ~40 s (the Garmin's 'Auto' record mode), 10-19 m from home - the one
# outdoor stretch (640 points at 1 s, scatter 2.6 m) was the only truth among them
GOOD_N, GOOD_RATE, GOOD_SCATTER_M = 60, 0.5, 10.0
STAR_N, STAR_NEAR_M = 300, 25.0     # auto-star: the tightest stretch with this many points this close to home


def good_stretch(n, span_s, scatter95_m):
    """dense (60+ points at one every 2 s or better) and not wandering (95 % within 10 m): a handful of points can
    look tight by luck (5-11-point indoor stretches did), and an hour indoors drifts 14-19 m"""
    if not (n and span_s and scatter95_m is not None):
        return False
    return n >= GOOD_N and n / span_s >= GOOD_RATE and scatter95_m <= GOOD_SCATTER_M


def _dist(lat1, lon1, lat2, lon2):
    e = math.radians(lon2 - lon1) * 6371000 * math.cos(math.radians((lat1 + lat2) / 2))
    n = math.radians(lat2 - lat1) * 6371000
    return math.hypot(e, n)


def load():
    try:
        with open(REFS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save(refs):
    tmp = REFS + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(refs, f, indent=1)
    os.replace(tmp, REFS)


def add(ref):
    """ref: {'name','lat','lon', 'height_msl'?, 'height_ell'?, 'height_source'?, 'source', 'n'?, 'span_s'?,
    'avg95_m'?, 't'?}; a reference of the same name and source is replaced - keeping its star (the Garmin's points are
    imported again every time it is plugged in, and that must not lose which one was chosen)"""
    return add_many([ref])[0]


def add_many(new):
    """add() for a whole import at once: one read and one write (an import used to rewrite the file per point)"""
    refs = load()
    for ref in new:
        key = (ref['name'], ref.get('source'))
        old = [r for r in refs if (r['name'], r.get('source')) == key]
        refs = [r for r in refs if (r['name'], r.get('source')) != key]
        for k in ('primary', 'auto_star'):
            if any(r.get(k) for r in old) and k not in ref:
                ref[k] = True
        ref.setdefault('t', time.time())
        refs.append(ref)
    save(refs)
    return new


def starred(refs=None):
    """the reference the position panel measures against: the starred one, else one named 'antenna', else None"""
    refs = refs if refs is not None else load()
    return (next((r for r in refs if r.get('primary')), None) or
            next((r for r in refs if r['name'].lower() == 'antenna'), None))


def auto_star(home):
    """nothing starred and nothing named 'antenna': star the tightest Garmin stretch beside home (STAR_N+ points within
    STAR_NEAR_M) - the panel otherwise measures against the home setting, the thing it should be checking. Returns
    the reference starred, or None"""
    refs = load()
    if starred(refs) or not home:
        return None
    cand = [r for r in refs if r.get('scatter95_m') is not None and (r.get('n') or 0) >= STAR_N
            and _dist(home[0], home[1], r['lat'], r['lon']) <= STAR_NEAR_M]
    if not cand:
        return None
    best = min(cand, key=lambda r: r['scatter95_m'])
    best['primary'] = best['auto_star'] = True
    save(refs)
    return best


def remove(name):
    save([r for r in load() if r['name'] != name])


def nearest(lat, lon, refs=None):
    """(reference, distance m) of the nearest one within NEAR_M, else (None, None)"""
    best = (None, None)
    for r in (refs if refs is not None else load()):
        d = _dist(lat, lon, r['lat'], r['lon'])
        if d <= NEAR_M and (best[1] is None or d < best[1]):
            best = (r, d)
    return best


def still_stretches(points, near=None, good_only=False):
    """[(t0, t1, lat, lon, ele, n, scatter95_m)] of the stretches where a track sat still for STILL_MIN_S+ (points
    (t, lat, lon, ele)); near=(lat, lon): only those within NEAR_M of it; good_only: only good_stretch() ones.
    The stretch's centre while it grows is a running mean (the median of the whole growing stretch, recomputed at
    every point, made this quadratic: 1.3 s on the GUI thread for one day's track, minutes for a day of 1 s logging);
    the median is taken once the stretch is complete"""
    out, i = [], 0
    pts = sorted(points)
    while i < len(pts):
        j = i
        sla, slo, k = pts[i][1], pts[i][2], 1
        la, lo = sla, slo
        while j + 1 < len(pts) and pts[j + 1][0] - pts[j][0] < 60 and _dist(la, lo, pts[j + 1][1], pts[j + 1][2]) < STILL_M:
            j += 1
            sla, slo, k = sla + pts[j][1], slo + pts[j][2], k + 1
            la, lo = sla / k, slo / k
        if pts[j][0] - pts[i][0] >= STILL_MIN_S:
            seg = pts[i:j + 1]
            la, lo = float(np.median([p[1] for p in seg])), float(np.median([p[2] for p in seg]))
            eles = [p[3] for p in seg if p[3] is not None]
            r = sorted(_dist(la, lo, p[1], p[2]) for p in seg)
            sc = r[int(0.95 * (len(r) - 1))]
            if (near is None or _dist(la, lo, near[0], near[1]) <= NEAR_M) and \
                    (not good_only or good_stretch(len(seg), seg[-1][0] - seg[0][0], sc)):
                out.append((seg[0][0], seg[-1][0], la, lo, float(np.median(eles)) if eles else None, len(seg), sc))
        i = j + 1
    return out


def geoid_sep():
    """ellipsoid minus sea level here (m; negative in most of the US), or None if not known yet"""
    try:
        with open(GEOID) as f:
            return float(json.load(f)['sep_m'])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def set_geoid_sep(sep_m, source):
    tmp = GEOID + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({'sep_m': round(float(sep_m), 2), 'source': source, 't': time.time()}, f)
    os.replace(tmp, GEOID)
