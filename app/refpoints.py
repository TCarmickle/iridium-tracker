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
    """ref: {'name','lat','lon', 'height_msl'?, 'height_ell'?, 'source', 'n'?, 'span_s'?, 'avg95_m'?, 't'?};
    a reference of the same name and source is replaced - keeping its star (the Garmin's points are imported again
    every time it is plugged in, and that must not lose which one was chosen)"""
    old = [r for r in load() if r['name'] == ref['name'] and r.get('source') == ref.get('source')]
    refs = [r for r in load() if not (r['name'] == ref['name'] and r.get('source') == ref.get('source'))]
    if any(r.get('primary') for r in old) and 'primary' not in ref:
        ref['primary'] = True
    ref.setdefault('t', time.time())
    refs.append(ref)
    save(refs)
    return ref


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


def still_stretches(points, near=None):
    """[(t0, t1, lat, lon, ele, n, scatter95_m)] of the stretches where a track sat still for STILL_MIN_S+ (points
    (t, lat, lon, ele)); near=(lat, lon): only those within NEAR_M of it"""
    out, i = [], 0
    pts = sorted(points)
    while i < len(pts):
        j = i
        la, lo = pts[i][1], pts[i][2]
        while j + 1 < len(pts) and pts[j + 1][0] - pts[j][0] < 60 and _dist(la, lo, pts[j + 1][1], pts[j + 1][2]) < STILL_M:
            j += 1
            seg = pts[i:j + 1]
            la, lo = float(np.median([p[1] for p in seg])), float(np.median([p[2] for p in seg]))
        if pts[j][0] - pts[i][0] >= STILL_MIN_S:
            seg = pts[i:j + 1]
            la, lo = float(np.median([p[1] for p in seg])), float(np.median([p[2] for p in seg]))
            eles = [p[3] for p in seg if p[3] is not None]
            r = sorted(_dist(la, lo, p[1], p[2]) for p in seg)
            if near is None or _dist(la, lo, near[0], near[1]) <= NEAR_M:
                out.append((seg[0][0], seg[-1][0], la, lo, float(np.median(eles)) if eles else None, len(seg),
                            r[int(0.95 * (len(r) - 1))]))
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
