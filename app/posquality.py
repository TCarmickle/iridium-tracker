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
import time

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


def _rstd(x):
    """a standard deviation that a few wild fixes don't inflate (1.4826 x the median absolute deviation)"""
    x = np.asarray(x, float)
    return float(1.4826 * np.median(np.abs(x - np.median(x)))) if len(x) else 0.0


def _corr_time(t, x):
    """how long (s) the errors take to change: where the autocorrelation of x (on a 1 s grid) falls below 1/e"""
    if len(t) < 10 or t[-1] - t[0] < 20:
        return 60.0
    g = np.arange(t[0], t[-1], 1.0)
    y = np.interp(g, t, x)
    y = y - y.mean()
    n = len(y)
    f = np.fft.rfft(y, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    if ac[0] <= 0:
        return 60.0
    ac = ac / ac[0]
    below = np.nonzero(ac < 1 / math.e)[0]
    lag = float(below[0]) if len(below) else float(n)
    return min(max(lag, 5.0), 600.0)


SESSION_GAP_S = 600.0
TAU_MIN_S = 120.0       # GNSS position errors are taken to change no faster than this (see quality)...
# ...per source, calibrated 2026-11-06 against the Garmin's outdoor point beside the antenna (build\_tmp\
# review-20261106\garmin-reference\calib.py): the own solver's errors are smoothed and slow (measured correlation
# ~51 s, but a bias that lasts the whole window), so with 120 s its '10-min average' claim held only ~52-80 % of
# the time - 240 s plus the window's own drift holds 92-97 %; pocket_trk's are nearly white (~9 s), so 120 s made
# its claim ~5x too pessimistic - 30 s still holds 100 %
TAU_FLOOR_S = {'own': 240.0, 'pocket': 30.0}
LT_MIN_SESSIONS = 4     # 'all N sessions' is shown from this many on (with 2 the spread is a guess)
_T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
         12: 2.179, 15: 2.131, 20: 2.086, 30: 2.042}


def t975(dof):
    """Student's t (97.5 %) for this many degrees of freedom: a spread from few sessions is itself uncertain"""
    if dof <= 0:
        return float('inf')
    keys = sorted(_T975)
    for k in keys:
        if dof <= k:
            return _T975[k]
    return 1.96
AVG_MIN_S = 300.0       # a window's average figure needs at least this much of it


def long_term(fixes, home=None):
    """your position from every session in the catalog: each session's middle (sessions apart by 10+ min, with 30+
    fixes over 2+ min), their average, and how sure that is - from how much the sessions disagree with each other
    (they differ in time, satellites and atmosphere, so their spread is the honest error). None without a session."""
    fx = sorted((f for f in fixes if f.get('nsat', 0) >= 4 and not f.get('quick')), key=lambda f: f['t'])
    sessions, cur = [], []
    for f in fx:
        if cur and f['t'] - cur[-1]['t'] > SESSION_GAP_S:
            sessions.append(cur)
            cur = []
        cur.append(f)
    if cur:
        sessions.append(cur)
    mids = []
    floor = TAU_FLOOR_S['own' if fx and 'hdop' in fx[-1] else 'pocket']
    for s in sessions:
        if len(s) >= MIN_FIXES and s[-1]['t'] - s[0]['t'] >= 120:
            hm = [f['height_m'] for f in s if not f.get('prior')]     # (measured heights only: see quality())
            # a session's weight: how many independent fixes it holds (a 2-minute one counted like an hour before)
            w = max(1.0, (s[-1]['t'] - s[0]['t']) / (2.0 * floor))
            mids.append((float(np.median([f['lat'] for f in s])), float(np.median([f['lon'] for f in s])),
                         float(np.median(hm)) if len(hm) >= MIN_FIXES else None, s[0]['t'], len(s), w))
    if not mids:
        return None
    wts = np.array([m[5] for m in mids])
    lat = float(np.average([m[0] for m in mids], weights=wts))
    lon = float(np.average([m[1] for m in mids], weights=wts))
    hs = [m[2] for m in mids if m[2] is not None]
    h = float(np.mean(hs)) if hs else float('nan')
    e, n = _enu_m(lat, lon, [m[0] for m in mids], [m[1] for m in mids])
    N = len(mids)
    out = {'sessions': N, 'lat': lat, 'lon': lon, 'height_m': h, 'fixes': sum(m[4] for m in mids),
           'first': mids[0][3], 'last': mids[-1][3],
           'spread_m': float(np.sqrt(np.mean(e * e + n * n))),
           'days': len({time.localtime(m[3])[:3] for m in mids})}
    if N >= 2:
        # the sessions' spread, widened by Student's t: from 2 sessions it is a guess (x6.5 at 95 %), from 10 fair
        sig = math.sqrt((np.var(e, ddof=1) + np.var(n, ddof=1)) / 2.0)
        n_w = float(wts.sum() ** 2 / (wts ** 2).sum())      # (the weighted mean's effective number of sessions)
        out['avg95_m'] = 2.45 * (t975(N - 1) / 1.96) * sig / math.sqrt(n_w)
        out['v95_m'] = (t975(len(hs) - 1) * float(np.std(hs, ddof=1)) / math.sqrt(len(hs))) if len(hs) >= 2 \
            else float('nan')
    if home:
        he, hn = _enu_m(lat, lon, home[0], home[1])
        out['from_home_m'] = float(math.hypot(he, hn))
        out['height_vs_home_m'] = h - home[2] * 1000.0
    return out


def grade(r95):
    for lim, word in GRADES:
        if r95 < lim:
            return word
    return 'bad'


def quality(fixes, now=None, home=None, azel=None, source=None):
    """fixes: [{'t','lat','lon','height_m','nsat'}]; now: the end of the window (default: the last fix);
    home: (lat, lon, alt_km) or None; azel: the tracked satellites' (az, el) for the geometry; source: 'own' (our
    solver's GSPP fixes) or 'pocket' (pocket_trk's GPOS) - by default told apart by the own fixes' 'hdop' key.
    Returns a dict (see the module notes), or None without enough fixes."""
    if source is None:
        source = 'own' if fixes and 'hdop' in fixes[-1] else 'pocket'
    fx = sorted((f for f in fixes if f.get('nsat', 0) >= 4 and not f.get('quick')), key=lambda f: f['t'])
    if not fx:
        return None
    end = now if now is not None else fx[-1]['t']
    fx = [f for f in fx if end - WINDOW_S <= f['t'] <= end]
    if len(fx) < MIN_FIXES:
        return {'n': len(fx), 'enough': False, 't_last': fx[-1]['t'] if fx else None}
    lat = np.array([f['lat'] for f in fx])
    lon = np.array([f['lon'] for f in fx])
    # heights: only from fixes that measured it - where the static antenna's height row (spp.py 'prior') held the
    # solution, the height is the antenna's known one, and counting it would make the height look perfect
    hm = [f for f in fx if not f.get('prior')]
    h = np.array([f['height_m'] for f in (hm if len(hm) >= MIN_FIXES else fx)])
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
    # how sure the window's middle is: the scatter shrinks with the number of independent fixes, and fixes are only
    # independent once the errors have had time to change (their correlation time, from the fixes themselves)
    # (at least TAU_MIN_S: the atmosphere, reflections and the carrier smoothing make errors last minutes, which a
    # short window can't see - without the floor a 2-minute average claimed 0.6 m where the Garmin beside the antenna
    # showed 4 m, and separate sessions disagree by 1.4 m (WAAS-corrected) to 5 m (pocket_trk))
    tau = max(_corr_time(t, e), _corr_time(t, n), TAU_FLOOR_S.get(source, TAU_MIN_S))
    n_eff = max(1.0, q['span_s'] / (2.0 * tau))
    sig = math.sqrt((_rstd(e) ** 2 + _rstd(n) ** 2) / 2.0)
    q.update(tau_s=tau, n_eff=n_eff, avg95_m=2.45 * sig / math.sqrt(n_eff), source_kind=source,
             avg_v95_m=1.96 * _rstd(h - h0) / math.sqrt(n_eff))
    # drift: the middle of the first two minutes against the last two
    a, b = t <= t[0] + 120, t >= t[-1] - 120
    if q['span_s'] >= 300 and a.sum() >= 10 and b.sum() >= 10:
        ea, na = float(np.median(e[a])), float(np.median(n[a]))
        eb, nb = float(np.median(e[b])), float(np.median(n[b]))
        q['drift_m'] = math.hypot(eb - ea, nb - na)
        q['drift_min'] = (np.median(t[b]) - np.median(t[a])) / 60.0
        # a wander over the window is an error the scatter inside it cannot show: half of it goes into the
        # average's uncertainty (measured: the own solver's drift medians 2-3.7 m exceeded its claimed 1.8-2.2 m)
        q['avg95_m'] = math.hypot(q['avg95_m'], q['drift_m'] / 2.0)
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
    parts = ['Your position%s: ±%.0f m per fix (95%%), %s' % (
        ' (WAAS-corrected)' if q.get('source') == 'waas' else '', q['r95_m'], q['grade'])]
    if q['span_s'] >= AVG_MIN_S:
        avg = '%.0f-min average ±%.1f m' % (q['span_s'] / 60, q['avg95_m'])
    else:
        avg = 'average: needs %d+ min' % (AVG_MIN_S / 60)
    ref = q.get('ref')
    if ref:                         # measured against a reference point (independent of these fixes) when there is one
        avg += ', %.1f m from %s' % (q['ref_m'], ref['name'])
    elif 'from_home_m' in q:        # (the home setting is what this should be checking: say there is nothing better)
        avg += ', %.1f m from home (no reference starred)' % q['from_home_m']
    parts.append(avg)
    lt = q.get('long')
    if lt and lt['sessions'] >= LT_MIN_SESSIONS and 'avg95_m' in lt:
        far = (', %.1f m from %s' % (lt['ref_m'], ref['name']) if ref and 'ref_m' in lt else
               ', %.1f m from home' % lt['from_home_m'] if 'from_home_m' in lt else '')
        parts.append('all %d sessions ±%.1f m%s' % (lt['sessions'], lt['avg95_m'], far))
    if 'drift_m' in q:
        parts.append('drift %.0f m over %.0f min' % (q['drift_m'], q['drift_min']))
    if 'hdop' in q:
        parts.append('HDOP %.1f' % q['hdop'])
    parts.append('%.0f satellites' % q['nsat'])
    if 'pocket_r95_m' in q:
        parts.append('pocket_trk alone ±%.0f m' % q['pocket_r95_m'])
    return '  ·  '.join(parts)


def details(q):
    """the full breakdown (tooltip / panel)"""
    if not q or not q['enough']:
        return ('Needs %d position fixes with 4+ satellites (about half a minute of GNSS L1 with a fix). The figures '
                'are from the last %d minutes.' % (MIN_FIXES, WINDOW_S / 60))
    L = ['From your last %d position fixes (%.0f min, median %.0f satellites):' % (q['n'], q['span_s'] / 60, q['nsat'])]
    if q.get('source') == 'waas':
        L += ['These are the tracker\'s own fixes (spp.py): the raw satellite ranges, carrier-smoothed, with the WAAS '
              'corrections (satellite orbits and clocks, and the ionosphere over North America).' +
              (' pocket_trk\'s own fixes over the same time: 95 %% within %.0f m.' % q['pocket_r95_m']
               if 'pocket_r95_m' in q else '')]
    L += ['',
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
    L += ['', 'The average - how sure the middle of these fixes is:',
          ('   95 %% within %.1f m horizontally, %.1f m in height' % (q['avg95_m'], q['avg_v95_m'])
           if q['span_s'] >= AVG_MIN_S else
           '   needs %d minutes or more of fixes (errors last minutes, so a shorter stretch can\'t say)' % (AVG_MIN_S / 60)),
          '   (the errors change over about %.0f s, so these %d fixes count as about %.0f independent ones; single '
          'fixes jump, but their middle settles)' % (q['tau_s'], q['n'], q['n_eff'])]
    if 'from_home_m' in q:
        L.append('   the middle is %.1f m from your home location in Settings (height %+.0f m)' % (
            q['from_home_m'], q['height_vs_home_m']))
    lt = q.get('long')
    if lt:
        L += ['', 'Over time - every session in the catalog (%s fixes):' % lt.get('label', 'position')]
        if lt['sessions'] >= 2:
            L += ['   %d sessions on %d day%s, %s - %s: their middles agree to %.1f m (rms)' % (
                lt['sessions'], lt['days'], '' if lt['days'] == 1 else 's',
                time.strftime('%d %b', time.localtime(lt['first'])), time.strftime('%d %b', time.localtime(lt['last'])),
                lt['spread_m']),
                  '   so their average is good to %.1f m horizontally, %.1f m in height (95 %%) - it tightens as '
                  'sessions on other days and times are added' % (lt['avg95_m'], lt['v95_m'])]
        else:
            L.append('   one session so far - the over-time figure needs two or more (other days, other times)')
        if 'from_home_m' in lt:
            L.append('   that average is %.1f m from your home location (height %+.0f m)' % (lt['from_home_m'],
                                                                                         lt['height_vs_home_m']))
    if q.get('ref'):
        r = q['ref']
        L += ['', 'Reference point "%s" (%s%s): the recent middle is %.1f m from it%s' % (
            r['name'], r.get('source', ''), ', ±%.1f m' % r['avg95_m'] if r.get('avg95_m') else '', q['ref_m'],
            '; the all-sessions average %.1f m' % lt['ref_m'] if lt and 'ref_m' in lt else '')]
    if q.get('geoid_sep') is not None:
        L.append('Height: %.0f m above sea level (%.0f m on GPS\'s ellipsoid; the two differ by %.1f m here)' % (
            q['height_m'] - q['geoid_sep'], q['height_m'], -q['geoid_sep']))
    L += ['', 'Grades by the 95 % figure: under 5 m excellent, 10 good, 25 fair, 50 poor.',
          'Fixes a second apart aren\'t independent, so the longer the window (up to %d min) the more honest the '
          'figures.' % (WINDOW_S / 60)]
    return '\n'.join(L)
