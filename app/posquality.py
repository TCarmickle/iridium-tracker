"""How good is the GNSS position? From the receiver's own position fixes (GPOS, about one a second) and the
geometry of the satellites it tracks.

  Scatter (precision): each fix's horizontal distance from the middle (median) of the recent fixes -
    CEP50  half the fixes fall within this radius
    R95    95 % of them do (the usual 'accuracy' figure for a GPS)
    vertical 95 %: the same for the height (GPS heights wander about twice as much)
  Drift: how far the middle of the fixes moved over the window (the first two minutes against the last two): an
    antenna that doesn't move should stay put; a slow wander is the atmosphere, satellites changing, or multipath.
  Geometry (DOP): from where the satellites are in the sky (az / el from their own broadcast orbits). Satellites
    spread over the sky pin the position down; bunched together they don't. HDOP / VDOP / PDOP multiply the
    ranging error: expected horizontal 95 % ~ 2 x HDOP x UERE, with UERE ~5 m for single-frequency L1 with the
    broadcast ionosphere model (no corrections).
  From home: the median fix against the location in Settings (meaningful if that was set independently).
Fixes one second apart are not independent (the errors change over minutes), so a short window looks better than
it is: the figures need MIN_FIXES and are best over the full WINDOW_S.
"""
import math

import numpy as np

WINDOW_S = 600.0        # the last 10 minutes of fixes
MIN_FIXES = 30
UERE_M = 5.0            # user equivalent range error, 1 sigma (L1 C/A, broadcast clock / orbit / ionosphere)
GRADES = [(5, 'excellent'), (10, 'good'), (25, 'fair'), (50, 'poor')]      # R95 (m) -> words; beyond: bad
R_EARTH = 6371000.0


def _enu_m(lat0, lon0, lat, lon):
    """east, north (m) of points from (lat0, lon0) - a flat approximation, fine over kilometres"""
    e = np.radians(np.asarray(lon) - lon0) * R_EARTH * math.cos(math.radians(lat0))
    n = np.radians(np.asarray(lat) - lat0) * R_EARTH
    return e, n


def dop(azel):
    """[(azimuth deg, elevation deg)] of the satellites used -> {'hdop','vdop','pdop','n_geo'}, or None (needs 4)"""
    if len(azel) < 4:
        return None
    G = []
    for az, el in azel:
        a, e = math.radians(az), math.radians(el)
        G.append([-math.cos(e) * math.sin(a), -math.cos(e) * math.cos(a), -math.sin(e), 1.0])
    G = np.array(G)
    try:
        Q = np.linalg.inv(G.T @ G)
    except np.linalg.LinAlgError:
        return None
    h, v = math.sqrt(max(Q[0, 0] + Q[1, 1], 0)), math.sqrt(max(Q[2, 2], 0))
    return {'hdop': h, 'vdop': v, 'pdop': math.sqrt(h * h + v * v), 'n_geo': len(azel)}


def grade(r95):
    for lim, word in GRADES:
        if r95 < lim:
            return word
    return 'bad'


def quality(fixes, now=None, home=None, azel=None):
    """fixes: [{'t','lat','lon','height_m','nsat'}]; now: the end of the window (default: the last fix);
    home: (lat, lon, alt_km) or None; azel: the tracked satellites' (az, el) for the geometry.
    Returns a dict (see the module notes), or None without enough fixes."""
    fx = sorted((f for f in fixes if f.get('nsat', 0) >= 4), key=lambda f: f['t'])
    if not fx:
        return None
    end = now if now is not None else fx[-1]['t']
    fx = [f for f in fx if end - WINDOW_S <= f['t'] <= end]
    if len(fx) < MIN_FIXES:
        return {'n': len(fx), 'enough': False, 't_last': fx[-1]['t'] if fx else None}
    lat = np.array([f['lat'] for f in fx])
    lon = np.array([f['lon'] for f in fx])
    h = np.array([f['height_m'] for f in fx])
    t = np.array([f['t'] for f in fx])
    lat0, lon0, h0 = float(np.median(lat)), float(np.median(lon)), float(np.median(h))
    e, n = _enu_m(lat0, lon0, lat, lon)
    r = np.hypot(e, n)
    q = {'n': len(fx), 'enough': True, 'span_s': float(t[-1] - t[0]), 't_last': float(t[-1]),
         'lat': lat0, 'lon': lon0, 'height_m': h0,
         'cep50_m': float(np.median(r)), 'r95_m': float(np.percentile(r, 95)),
         'v95_m': float(np.percentile(np.abs(h - h0), 95)),
         'nsat': float(np.median([f['nsat'] for f in fx]))}
    q['grade'] = grade(q['r95_m'])
    # drift: the middle of the first two minutes against the last two
    a, b = t <= t[0] + 120, t >= t[-1] - 120
    if q['span_s'] >= 300 and a.sum() >= 10 and b.sum() >= 10:
        ea, na = float(np.median(e[a])), float(np.median(n[a]))
        eb, nb = float(np.median(e[b])), float(np.median(n[b]))
        q['drift_m'] = math.hypot(eb - ea, nb - na)
        q['drift_min'] = (np.median(t[b]) - np.median(t[a])) / 60.0
    if home:
        he, hn = _enu_m(lat0, lon0, home[0], home[1])
        q['from_home_m'] = float(math.hypot(he, hn))
        q['height_vs_home_m'] = h0 - home[2] * 1000.0
    g = dop(azel or [])
    if g:
        q.update(g)
        q['expected_r95_m'] = 2.0 * g['hdop'] * UERE_M
    return q


def headline(q):
    """one line for the panel"""
    if not q:
        return 'Your position: no GNSS position fixes yet (receive GNSS L1)'
    if not q['enough']:
        return 'Your position: %d fixes so far - needs %d for an accuracy figure' % (q['n'], MIN_FIXES)
    parts = ['Your position: ±%.0f m (95%%), %s' % (q['r95_m'], q['grade']),
             'half within %.0f m' % q['cep50_m']]
    if 'drift_m' in q:
        parts.append('drift %.0f m over %.0f min' % (q['drift_m'], q['drift_min']))
    if 'hdop' in q:
        parts.append('HDOP %.1f' % q['hdop'])
    parts.append('%.0f satellites' % q['nsat'])
    return '  ·  '.join(parts)


def details(q):
    """the full breakdown (tooltip / panel)"""
    if not q or not q['enough']:
        return ('Needs %d position fixes with 4+ satellites (about half a minute of GNSS L1 with a fix). The figures '
                'are from the last %d minutes.' % (MIN_FIXES, WINDOW_S / 60))
    L = ['From your last %d position fixes (%.0f min, median %.0f satellites):' % (q['n'], q['span_s'] / 60, q['nsat']),
         '',
         'Scatter - how far each fix is from their middle:',
         '   half of them within %.1f m (CEP50)' % q['cep50_m'],
         '   95 %% within %.1f m (R95) - %s' % (q['r95_m'], q['grade']),
         '   height: 95 %% within %.1f m' % q['v95_m']]
    if 'drift_m' in q:
        L.append('Drift - the middle moved %.1f m over %.0f min (the antenna doesn\'t move: this is the satellites '
                 'and the atmosphere)' % (q['drift_m'], q['drift_min']))
    if 'hdop' in q:
        L += ['Geometry of the %d satellites tracked now: HDOP %.1f, VDOP %.1f, PDOP %.1f' % (q['n_geo'], q['hdop'],
                                                                                           q['vdop'], q['pdop']),
              '   expected 95 %% horizontal error about %.0f m (2 x HDOP x %.0f m ranging error)' % (q['expected_r95_m'],
                                                                                               UERE_M),
              '   (under 2 is good geometry; over 5, the satellites are bunched up)']
    if 'from_home_m' in q:
        L.append('From your home location in Settings: %.1f m (height %+.0f m)' % (q['from_home_m'],
                                                                                   q['height_vs_home_m']))
    L += ['', 'Grades by the 95 % figure: under 5 m excellent, 10 good, 25 fair, 50 poor.',
          'Fixes a second apart aren\'t independent, so the longer the window (up to %d min) the more honest the '
          'figures.' % (WINDOW_S / 60)]
    return '\n'.join(L)
