"""Running catalog of every satellite heard across all captures (Iridium and Orbcomm).

Usage: python catalog.py            (adds any new captures, then writes catalog.txt)
       python catalog.py --redo     (re-analyzes every capture from scratch)

Each capture is analyzed once and stored in catalog-data.json, so results don't change
later when the satellite orbit data (TLEs) gets older or is refreshed.
Satellites are named two ways:
  position  - a ring alert reported its position; the satellite overhead there is named
  Doppler   - the frequency curve of a plane/slot's ITL frames matches one satellite's orbit
Orbcomm satellites send their own position (ephemeris packets); the satellite at that spot is named,
and that name sticks to the satellite ID in its other packets.
"""
import collections
import datetime
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths  # noqa: E402  (where the data lives)
import storage  # noqa: E402
import systems  # noqa: E402
# iridium-toolkit: inside this folder (a packaged copy) or next to it
TOOLKIT = next((d for d in (os.path.join(HERE, 'iridium-toolkit'), os.path.join(HERE, '..', 'iridium-toolkit'))
                if os.path.exists(os.path.join(d, 'iridium-parser.py'))), os.path.join(HERE, 'iridium-toolkit'))
PARSER = os.path.join(TOOLKIT, 'iridium-parser.py')
# public orbit list, used only to put names on satellites (positions always come from our own fixes);
# kept fresh from CelesTrak, with SatDump's copy as a fallback
TLE_FILE = os.path.join(paths.DATA, 'iridium-names.tle')
TLE_URL = 'https://celestrak.org/NORAD/elements/gp.php?GROUP=%s&FORMAT=tle'


def tle_file(system='iridium'):
    """the name list of a system (both ham bands share the ham one)"""
    s = systems.get(system)
    return os.path.join(paths.DATA, '%s-names.tle' % s.get('system', s['key']))


SATDUMP_TLES = os.path.join(os.environ.get('APPDATA', ''), 'satdump', 'satdump_tles.txt')
DATA = os.path.join(paths.DATA, 'catalog-data.json')
REPORT = os.path.join(paths.DATA, 'catalog.txt')
SETTINGS = os.path.join(paths.DATA, 'settings.json')


def load_home():
    """(lat, lon, altitude km) of the antenna from settings.json, or None if not set up yet"""
    try:
        lat, lon, alt = json.load(open(SETTINGS))['home']
        return float(lat), float(lon), float(alt)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_home(lat, lon, alt_km):
    try:
        settings = json.load(open(SETTINGS))
    except (OSError, ValueError):
        settings = {}
    settings['home'] = [lat, lon, alt_km]
    json.dump(settings, open(SETTINGS, 'w'), indent=1)


HOME = load_home()
UTC = datetime.timezone.utc

# ---------- orbits (TLE mean elements + J2 drift; good to a few km over days) ----------
MU, RE, J2, D2R, C = 398600.4418, 6378.137, 1.08262668e-3, math.pi / 180, 299792.458


class Sat:
    def __init__(self, name, l1, l2):
        self.name = name
        yy, doy = int(l1[18:20]), float(l1[20:32])
        self.epoch = datetime.datetime(2000 + yy, 1, 1, tzinfo=UTC) + datetime.timedelta(days=doy - 1)
        self.i, self.raan = float(l2[8:16]) * D2R, float(l2[17:25]) * D2R
        self.e = float('0.' + l2[26:33].strip())
        self.w, self.m = float(l2[34:42]) * D2R, float(l2[43:51]) * D2R
        self.n = float(l2[52:63]) * 2 * math.pi / 86400
        self.a = (MU / self.n ** 2) ** (1 / 3)
        p = self.a * (1 - self.e ** 2)
        k = 1.5 * self.n * J2 * (RE / p) ** 2
        self.raan_dot = -k * math.cos(self.i)
        self.w_dot = 0.5 * k * (5 * math.cos(self.i) ** 2 - 1)

    def ecef(self, t):
        dt = (t - self.epoch).total_seconds()
        M, raan, w = self.m + self.n * dt, self.raan + self.raan_dot * dt, self.w + self.w_dot * dt
        E = M
        for _ in range(8):
            E -= (E - self.e * math.sin(E) - M) / (1 - self.e * math.cos(E))
        nu = 2 * math.atan2(math.sqrt(1 + self.e) * math.sin(E / 2), math.sqrt(1 - self.e) * math.cos(E / 2))
        r, u = self.a * (1 - self.e * math.cos(E)), w + nu
        cO, sO, ci, si, cu, su = math.cos(raan), math.sin(raan), math.cos(self.i), math.sin(self.i), math.cos(u), math.sin(u)
        x, y, z = r * (cO * cu - sO * su * ci), r * (sO * cu + cO * su * ci), r * su * si
        jd = 2440587.5 + t.timestamp() / 86400
        th = (280.46061837 + 360.98564736629 * (jd - 2451545.0)) % 360 * D2R
        return x * math.cos(th) + y * math.sin(th), -x * math.sin(th) + y * math.cos(th), z


def site(lat, lon, h):
    f = 1 / 298.257223563
    e2, la, lo = f * (2 - f), lat * D2R, lon * D2R
    N = RE / math.sqrt(1 - e2 * math.sin(la) ** 2)
    return ((N + h) * math.cos(la) * math.cos(lo), (N + h) * math.cos(la) * math.sin(lo), (N * (1 - e2) + h) * math.sin(la))


def look(sat, t, lat, lon, h=0.0):
    """elevation (deg), azimuth (deg), range (km), range rate (km/s) from a ground point"""
    o, p = site(lat, lon, h), sat.ecef(t)
    p2 = sat.ecef(t + datetime.timedelta(seconds=1))
    d = [p[k] - o[k] for k in range(3)]
    rng = math.sqrt(sum(v * v for v in d))
    rng2 = math.sqrt(sum((p2[k] - o[k]) ** 2 for k in range(3)))
    la, lo = lat * D2R, lon * D2R
    S = math.sin(la) * math.cos(lo) * d[0] + math.sin(la) * math.sin(lo) * d[1] - math.cos(la) * d[2]
    Ea = -math.sin(lo) * d[0] + math.cos(lo) * d[1]
    Z = math.cos(la) * math.cos(lo) * d[0] + math.cos(la) * math.sin(lo) * d[1] + math.sin(la) * d[2]
    return math.degrees(math.asin(Z / rng)), (math.degrees(math.atan2(Ea, -S)) + 360) % 360, rng, rng2 - rng


def refresh_tles(max_age_days=2, system='iridium'):
    """download a fresh name list now and then (CelesTrak asks for no more than one fetch every couple of hours)"""
    s = systems.get(system)
    path = tle_file(system)
    groups = s.get('tle_groups') or (s['tle_group'],)
    try:            # (the groups the list was made from: a list missing a newly added group is fetched again)
        same = open(path + '.groups').read().split() == list(groups)
    except OSError:
        same = len(groups) == 1
    if same and os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age_days * 86400:
        return
    try:
        import requests
        text = ''
        for group in groups:
            r = requests.get(TLE_URL % group, timeout=15, headers={'User-Agent': 'IridiumTracker/1.0'})
            if not (r.ok and '\n1 ' in r.text and (not s['prefix'] or s['prefix'].strip() in r.text)):
                return          # a group failed: keep the old list rather than half of a new one
            text += r.text.replace('\r\n', '\n').rstrip('\n') + '\n'
        open(path, 'w', newline='\n').write(text)
        open(path + '.groups', 'w').write(' '.join(groups))
    except Exception:
        pass        # offline: keep using what we have


def load_tles(system='iridium'):
    """the public catalog's satellites of one system (for names only)"""
    refresh_tles(system=system)
    prefix = (systems.get(system)['prefix'] or '').strip()
    for path in (tle_file(system), SATDUMP_TLES):
        if os.path.exists(path):
            lines = open(path).read().splitlines()
            sats = [Sat(lines[i].strip(), lines[i + 1], lines[i + 2])
                    for i in range(len(lines) - 2) if lines[i].startswith(prefix) and lines[i + 1].startswith('1 ')
                    and lines[i + 2].startswith('2 ') and not lines[i].startswith(('1 ', '2 '))]
            if sats:
                return sats
    return []


# ---------- reading a capture ----------
def capture_start(stem):
    """(UTC start of the burst clock, how it was known)"""
    try:
        text = open(stem + '.start').read().strip()
        t = datetime.datetime.strptime(text.rstrip('Z'), '%Y-%m-%d %H:%M:%S.%f')
        if text.endswith('Z'):
            return t.replace(tzinfo=UTC), 'measured'
        return t.astimezone(UTC), 'measured'
    except (OSError, ValueError):
        t = datetime.datetime.strptime(re.search(r'(\d{8}-\d{6})', stem).group(1), '%Y%m%d-%H%M%S')
        # older captures: the decoder started ~10-45 s after the file was named
        return t.astimezone(UTC) + datetime.timedelta(seconds=30), 'estimated'


def read_frames(lines):
    frames = []
    for line in lines:
        f = line.split()
        if len(f) < 6 or not f[0].endswith(':') or f[0] == 'ERR:':
            continue
        try:
            snr = float(f[5].split('|')[2]) if '|' in f[5] else 0.0
            frames.append((f[0][:-1], float(f[2]), int(f[3]), snr, line))
        except ValueError:
            continue
    return frames


# ---------- naming satellites ----------
def match_position(sats, t, lat, lon):
    """the satellite closest to straight above (lat, lon) within +-90 s of t"""
    best = None
    for s in sats:
        if look(s, t, lat, lon)[0] < 15:
            continue
        for dt in range(-90, 91, 2):
            el = look(s, t + datetime.timedelta(seconds=dt), lat, lon)[0]
            if best is None or el > best[1]:
                best = (s.name, el, dt)
    return best


def match_doppler(sats, pts, shifts):
    """fit the measured frequencies of one plane/slot pass to each satellite's Doppler curve"""
    fits = []
    for s in sats:
        best = None
        for sh in shifts:
            looks = [look(s, t + datetime.timedelta(seconds=sh), *HOME) for t, _ in pts]
            if min(l[0] for l in looks) < 0:
                continue
            diff = [hz + l[3] / C * 1626.1e6 for (_, hz), l in zip(pts, looks)]
            off = sum(diff) / len(diff)
            rms = math.sqrt(sum((d - off) ** 2 for d in diff) / len(diff))
            if best is None or rms < best:
                best = rms
        if best is not None:
            fits.append((best, s.name))
    fits.sort()
    return fits


def match_ecef(sats, t, xyz_km, max_km=300.0):
    """(name, km off) of the catalog satellite nearest an Earth-fixed position the satellite reported
    itself, or (None, km) if none is within max_km"""
    best = None
    for s in sats:
        p = s.ecef(t)
        d = math.sqrt(sum((a - b) ** 2 for a, b in zip(p, xyz_km)))
        if best is None or d < best[1]:
            best = (s.name, d)
    if best is None or best[1] > max_km:
        return None, best[1] if best else None
    return best


def ecef_to_lla(x, y, z):
    """Earth-fixed km -> latitude, longitude (degrees), height (km) on WGS84"""
    f = 1 / 298.257223563
    e2 = f * (2 - f)
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(5):
        N = RE / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - N
        lat = math.atan2(z, p * (1 - e2 * N / (N + h)))
    N = RE / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return math.degrees(lat), math.degrees(lon), p / math.cos(lat) - N


def read_orbcomm(lines):
    """Orbcomm receiver lines -> [(kind, unix t, Hz, sat id or None, snr, rest)]
       OEP: t Hz satid snr x y z vx vy vz   ephemeris: the satellite's own position (km) and velocity (km/s)
       OSY: t Hz satid snr                  sync packet
       OPK: t Hz satid snr type             any other packet
       OBAD: t Hz - snr                     heard, but failed its checksum"""
    out = []
    for line in lines:
        f = line.split()
        if len(f) < 5 or not f[0].endswith(':') or not f[0].startswith('O'):
            continue
        try:
            sid = None if f[3] == '-' else int(f[3], 16)
            out.append((f[0][:-1], float(f[1]), float(f[2]), sid, float(f[4]), f[5:]))
        except ValueError:
            continue
    return out


def analyze_orbcomm(stem, sats):
    """an Orbcomm capture: every position fix (named by matching it to the catalog) and, per satellite and
    pass, the measured carrier frequency of all its packets (its Doppler curve)"""
    start, how = capture_start(stem)
    src = frames_source(stem)
    if src.endswith(storage.REDUCED):
        meta, lines = storage.read_reduced(src)
    else:
        meta, lines = {}, open(src, errors='replace').read().splitlines()
    pk = read_orbcomm(lines)
    iso = lambda t: datetime.datetime.fromtimestamp(t, UTC).isoformat(timespec='seconds')
    heard = [p[1] for p in pk if p[0] != 'OEP'] or [p[1] for p in pk]     # reception times (ephemeris times are the satellite's)
    span = (max(heard) - min(heard)) / 3600 if heard else 0
    cap = {'capture': os.path.basename(stem), 'system': 'orbcomm', 'start_utc': iso(start.timestamp()), 'clock': how,
           'hours': meta.get('hours', round(span, 3)), 'counts': meta.get('counts', dict(collections.Counter(p[0] for p in pk))),
           'slots': [], 'positions': [], 'passes': []}
    ids = collections.defaultdict(collections.Counter)      # satellite ID -> names its positions matched
    for kind, t, hz, sid, snr, rest in pk:
        if kind != 'OEP' or len(rest) < 6:
            continue
        xyz = [float(v) for v in rest[:3]]
        vel = [float(v) for v in rest[3:6]]
        name, off = match_ecef(sats, datetime.datetime.fromtimestamp(t, UTC), xyz)
        if name:
            ids[sid][name] += 1
        lat, lon, alt = ecef_to_lla(*xyz)
        cap['positions'].append({'time': iso(t), 't': round(t, 3), 'net_id': sid, 'beam': None, 'lat': round(lat, 4),
                                 'lon': round(lon, 4), 'alt_km': round(alt), 'snr': snr, 'name': name, 'hz': hz,
                                 'catalog_off_km': round(off, 1) if off is not None else None,
                                 'xyz_km': [round(v, 3) for v in xyz], 'vel_kms': [round(v, 4) for v in vel]})
    id_name = {sid: c.most_common(1)[0][0] for sid, c in ids.items()}
    for p in cap['positions']:          # a position that matched nothing still belongs to its satellite ID
        p['name'] = p['name'] or id_name.get(p['net_id'])
    by_sat = collections.defaultdict(list)
    for kind, t, hz, sid, snr, rest in pk:
        if kind != 'OBAD' and sid in id_name:
            by_sat[id_name[sid], sid].append((t, hz, snr))
    for (name, sid), hits in sorted(by_sat.items()):
        hits.sort()
        groups, cur = [], [hits[0]]
        for h in hits[1:]:      # separate passes at gaps over 15 minutes
            if h[0] - cur[-1][0] > 900:
                groups.append(cur)
                cur = []
            cur.append(h)
        groups.append(cur)
        for g in groups:
            cap['passes'].append({'name': name, 'sat_id': sid, 'first': iso(g[0][0]), 'last': iso(g[-1][0]),
                                  'frames': len(g), 'best_snr': max(h[2] for h in g),
                                  'doppler': [[round(h[0], 3), h[1]] for h in g]})
    return cap


def norad_names(system='ham'):
    """NORAD number -> name in the public catalog's list (ham satellites are named the catalog's way,
    e.g. gr-satellites' 'AO-73' -> 'FUNCUBE-1 (AO-73)', so the info window can find them)"""
    out = {}
    try:
        lines = open(tle_file(system)).read().splitlines()
    except OSError:
        return out
    for i in range(len(lines) - 2):
        if lines[i + 1].startswith('1 ') and lines[i + 2].startswith('2 ') and not lines[i].startswith(('1 ', '2 ')):
            try:
                out[int(lines[i + 1][2:7])] = lines[i].strip()
            except ValueError:
                pass
    return out


def read_ham(lines):
    """ham receiver lines -> [(kind, unix t, Hz, norad, name or None, snr, callsign, candidates)]
       HFR: t Hz norad name snr bytes callsign candidates   a decoded frame (name '?' = which satellite is unknown)
       HSIG: t Hz - snr seconds                             a signal heard but not decoded"""
    out = []
    for line in lines:
        f = line.split()
        if len(f) < 5 or f[0] not in ('HFR:', 'HSIG:'):
            continue
        try:
            if f[0] == 'HFR:' and len(f) >= 8:
                name = None if f[4] == '?' else f[4].replace('_', ' ')
                out.append(('HFR', float(f[1]), float(f[2]), int(f[3]), name, float(f[5]), f[7],
                            f[8].replace('_', ' ').split('|') if len(f) > 8 else []))
            elif f[0] == 'HSIG:':
                out.append(('HSIG', float(f[1]), float(f[2]), 0, None, float(f[4]), '-', []))
        except ValueError:
            continue
    return out


def ham_name(norad, name, names):
    """the catalog's name for a decoded satellite (falls back to gr-satellites' name)"""
    return names.get(norad) or name


def analyze_ham(stem, names):
    """a 2 m / 70 cm capture: per identified satellite and pass, its frames' measured frequencies (its Doppler
    curve) and callsigns; frames that can't be tied to one satellite are only counted"""
    start, how = capture_start(stem)
    src = frames_source(stem)
    if src.endswith(storage.REDUCED):
        meta, lines = storage.read_reduced(src)
    else:
        meta, lines = {}, open(src, errors='replace').read().splitlines()
    fr = read_ham(lines)
    iso = lambda t: datetime.datetime.fromtimestamp(t, UTC).isoformat(timespec='seconds')
    span = (max(f[1] for f in fr) - min(f[1] for f in fr)) / 3600 if fr else 0
    key = 'ham70' if os.path.basename(stem).startswith('ham70') else 'ham2m'
    cap = {'capture': os.path.basename(stem), 'system': key, 'start_utc': iso(start.timestamp()), 'clock': how,
           'hours': meta.get('hours', round(span, 3)),
           'counts': meta.get('counts', dict(collections.Counter(f[0] for f in fr))),
           'slots': [], 'positions': [], 'passes': [],
           'unidentified': sum(1 for f in fr if f[0] == 'HFR' and not f[4])}
    by_sat = collections.defaultdict(list)
    for kind, t, hz, norad, name, snr, call, _ in fr:
        if kind == 'HFR' and name:
            by_sat[ham_name(norad, name, names), norad].append((t, hz, snr, call))
    for (name, norad), hits in sorted(by_sat.items()):
        hits.sort()
        groups, cur = [], [hits[0]]
        for h in hits[1:]:      # separate passes at gaps over 15 minutes
            if h[0] - cur[-1][0] > 900:
                groups.append(cur)
                cur = []
            cur.append(h)
        groups.append(cur)
        for g in groups:
            cap['passes'].append({'name': name, 'norad': norad, 'first': iso(g[0][0]), 'last': iso(g[-1][0]),
                                  'frames': len(g), 'best_snr': max(h[2] for h in g),
                                  'callsigns': sorted({h[3] for h in g if h[3] != '-'}),
                                  'doppler': [[round(h[0], 3), h[1]] for h in g]})
    return cap


GNSS_NAMES = {'G': 'GPS', 'E': 'Galileo', 'C': 'BeiDou', 'J': 'QZSS', 'R': 'GLONASS'}


def gnss_name(sat):
    """'G05' -> 'GPS G05', 'E11' -> 'Galileo E11', 'C23' -> 'BeiDou C23', 'S31' -> 'WAAS 131'"""
    if sat[:1] == 'S':
        return 'WAAS %d' % (100 + int(sat[1:]))
    return '%s %s' % (GNSS_NAMES.get(sat[:1], 'GNSS'), sat)


def gnss_catalog_names():
    """'G22' / 'C06' -> the public catalog's name ('GPS BIIR-5  (PRN 22)', 'BEIDOU-2 IGSO-1 (C06)'), for the info
    window. (Galileo's catalog names don't carry the PRN; those are matched by position instead.)"""
    out = {}
    try:
        lines = open(tle_file('gnss')).read().splitlines()
    except OSError:
        return out
    for i in range(len(lines) - 2):
        n = lines[i].strip()
        if not lines[i + 1].startswith('1 ') or '(' not in n:
            continue
        tag = n[n.rindex('(') + 1:].rstrip(')').strip()
        if tag.startswith('PRN ') and n.startswith('GPS '):
            out['G%02d' % int(tag[4:])] = n
        elif '/PRN ' in tag and 120 <= int(tag.split('/PRN ')[1]) <= 158:     # SBAS: 'SES-15 (WAAS/PRN 133)' -> S33
            out['S%02d' % (int(tag.split('/PRN ')[1]) - 100)] = n
        elif tag[:1] == 'C' and tag[1:].isdigit():
            out['C%02d' % int(tag[1:])] = n
    return out


def read_gnss(lines):
    """GNSS receiver lines -> [(kind, unix t, fields)]
       GCH: t sat sig cn0 doppler_hz lock_s    GEPH: t sat <$EPH fields>    GPOS: t lat lon height nsat"""
    out = []
    for line in lines:
        f = line.split(None, 3)
        if len(f) < 3 or f[0] not in ('GCH:', 'GEPH:', 'GPOS:'):
            continue
        try:
            out.append((f[0][:-1], float(f[1]), line.split()[2:]))
        except ValueError:
            continue
    return out


def analyze_gnss(stem):
    """a GNSS capture: per satellite and pass its Doppler curve and signal level, the newest broadcast
    ephemeris of each satellite, and the position fixes"""
    start, how = capture_start(stem)
    src = frames_source(stem)
    if src.endswith(storage.REDUCED):
        meta, lines = storage.read_reduced(src)
    else:
        meta, lines = {}, open(src, errors='replace').read().splitlines()
    rows = read_gnss(lines)
    iso = lambda t: datetime.datetime.fromtimestamp(t, UTC).isoformat(timespec='seconds')
    ts = [t for _, t, _ in rows]
    cap = {'capture': os.path.basename(stem), 'system': systems.of_capture(os.path.basename(stem))['key'],
           'start_utc': iso(start.timestamp()), 'clock': how,
           'hours': meta.get('hours', round((max(ts) - min(ts)) / 3600, 3) if ts else 0),
           'counts': meta.get('counts', dict(collections.Counter(k for k, _, _ in rows))),
           'slots': [], 'positions': [], 'passes': [], 'ephemerides': {}, 'gnss_fixes': []}
    by_sat = collections.defaultdict(list)
    for kind, t, f in rows:
        if kind == 'GCH' and len(f) >= 4:
            by_sat[f[0]].append((t, GNSS_L1 + float(f[3]), float(f[2])))
        elif kind == 'GEPH' and len(f) >= 2:
            cap['ephemerides'][f[0]] = [round(t, 3), ' '.join(f[1:])]
        elif kind == 'GPOS' and len(f) >= 4:
            cap['gnss_fixes'].append({'t': round(t, 3), 'lat': float(f[0]), 'lon': float(f[1]), 'height_m': float(f[2]),
                                      'nsat': int(f[3])})
    for sat, hits in sorted(by_sat.items()):
        hits.sort()
        groups, cur = [], [hits[0]]
        for h in hits[1:]:
            if h[0] - cur[-1][0] > 900:
                groups.append(cur)
                cur = []
            cur.append(h)
        groups.append(cur)
        for g in groups:
            cap['passes'].append({'name': gnss_name(sat), 'sat': sat, 'first': iso(g[0][0]), 'last': iso(g[-1][0]),
                                  'frames': len(g), 'best_snr': max(h[2] for h in g),
                                  'doppler': [[round(h[0], 3), round(h[1], 1)] for h in g]})
    return cap


GNSS_L1 = 1575.42e6


def frames_source(stem):
    """the decoded frames of a capture: full (.parsed / .live) or the reduced .frames.gz"""
    for ext in ('.parsed', '.live', storage.REDUCED):
        if os.path.exists(stem + ext) and os.path.getsize(stem + ext) > 0:
            return stem + ext
    return None


def analyze(stem, sats):
    start, how = capture_start(stem)
    src = frames_source(stem)
    if src.endswith(storage.REDUCED):       # only ring alerts and time & location frames were kept
        meta, lines = storage.read_reduced(src)
    else:
        meta, lines = {}, open(src, errors='replace')
    frames = read_frames(lines)
    at = lambda ms: start + datetime.timedelta(milliseconds=ms)
    iso = lambda t: t.isoformat(timespec='seconds')
    cap = {'capture': os.path.basename(stem), 'system': 'iridium', 'start_utc': iso(start), 'clock': how,
           'hours': meta.get('hours', round(max([f[1] for f in frames] or [0]) / 3.6e6, 2)),
           'counts': meta.get('counts', dict(collections.Counter(f[0] for f in frames))), 'slots': [], 'positions': []}
    clock_errs = []

    for kind, ms, hz, snr, line in frames:
        m = re.search(r'sat:(\d+) beam:(\d+).*?pos=\(([-+\d.]+)/([-+\d.]+)\) alt=(-?\d+)', line)
        if kind != 'IRA' or not m or int(m.group(5)) < 100:
            continue
        lat, lon = float(m.group(3)), float(m.group(4))
        hit = match_position(sats, at(ms), lat, lon)
        name = hit[0] if hit and hit[1] > 85 else None
        if name:
            clock_errs.append(hit[2])
        x = re.search(r'xyz=\(([-+\d]+),([-+\d]+),([-+\d]+)\)', line)
        cap['positions'].append({'time': iso(at(ms)), 'ms': ms, 'net_id': int(m.group(1)), 'beam': int(m.group(2)),
                                 'lat': lat, 'lon': lon, 'alt_km': int(m.group(5)), 'snr': snr, 'name': name,
                                 'overhead_deg': round(hit[1], 1) if hit else None, 'hz': hz,
                                 # Earth-fixed position as sent by the satellite (4 km units -> km)
                                 'xyz_km': [4 * int(v) for v in x.groups()] if x else None})

    # a capture's measured clock error (from position fixes) also corrects its Doppler fits
    shift = sorted(clock_errs)[len(clock_errs) // 2] if clock_errs else 0
    shifts = [shift] if clock_errs or how == 'measured' else range(-20, 21, 5)

    passes = collections.defaultdict(list)
    for kind, ms, hz, snr, line in frames:
        m = re.search(r' P(\d) S(\d+) ', line)
        if kind == 'ITL' and m:
            passes[(int(m.group(1)), int(m.group(2)))].append((at(ms), hz, snr))
    for (p, s), hits in sorted(passes.items()):
        hits.sort()
        groups, cur = [], [hits[0]]
        for h in hits[1:]:   # split into separate passes at gaps over 15 minutes
            if (h[0] - cur[-1][0]).total_seconds() > 900:
                groups.append(cur)
                cur = []
            cur.append(h)
        groups.append(cur)
        for g in groups:
            slot = {'plane': p, 'slot': s, 'first': iso(g[0][0]), 'last': iso(g[-1][0]), 'frames': len(g),
                    'best_snr': max(h[2] for h in g), 'name': None, 'confidence': None, 'rms_hz': None,
                    # measured frequency of every frame: (unix time, Hz) - the Doppler curve used for orbit fitting
                    'doppler': [[round(h[0].timestamp(), 3), h[1]] for h in g]}
            if len(g) >= 3 and (g[-1][0] - g[0][0]).total_seconds() >= 20:
                fits = match_doppler(sats, [(h[0], h[1]) for h in g], shifts)
                if fits:
                    rms, name = fits[0]
                    second = fits[1][0] if len(fits) > 1 else 1e9
                    slot['name'], slot['rms_hz'] = name, round(rms)
                    slot['confidence'] = 'confident' if rms < 300 and second > 2.5 * rms else 'possible'
            cap['slots'].append(slot)
    return cap


# ---------- report ----------
def local(iso_utc):
    return datetime.datetime.fromisoformat(iso_utc).astimezone()


def fmt(iso_utc):
    t = local(iso_utc)
    return t.strftime('%b %d %I:%M %p').replace(' 0', ' ')


def build_report(caps):
    out = []
    w = out.append
    decoded = sum(n for c in caps for k, n in c['counts'].items() if k != 'RAW')
    raw = sum(c['counts'].get('RAW', 0) for c in caps)
    hours = sum(c['hours'] for c in caps)

    slots = collections.defaultdict(list)          # (plane, slot) -> sightings
    for c in caps:
        for s in c['slots']:
            slots[(s['plane'], s['slot'])].append(s)
    names = collections.defaultdict(lambda: {'positions': [], 'slots': set(), 'net_ids': set(), 'seen': []})
    for c in caps:
        for p in c['positions']:
            if p['name']:
                n = names[p['name']]
                n['positions'].append(p)
                n['net_ids'].add(p['net_id'])
                n['seen'].append((p['time'], p['snr']))
        for s in c['slots']:
            if s['name'] and s['confidence'] == 'confident':
                n = names[s['name']]
                n['slots'].add((s['plane'], s['slot']))
                n['seen'].append((s['first'], s['best_snr']))
    slot_name = {}
    for nm, n in names.items():
        for ps in n['slots']:
            slot_name.setdefault(ps, set()).add(nm)

    w('IRIDIUM CATALOG  -  updated %s' % datetime.datetime.now().strftime('%b %d %Y %I:%M %p'))
    w('=' * 78)
    w('Captures: %d    listening time: %.1f h    decoded: %d    damaged (RAW): %d'
      % (len(caps), hours, decoded, raw))
    w('Plane/slot positions heard: %d of 66    satellites named: %d    position fixes: %d'
      % (len(slots), len(names), sum(len(c['positions']) for c in caps)))
    w('')
    w('PLANES AND SLOTS HEARD  (x = heard, N = heard and named by Doppler)')
    w('            ' + ''.join('S%-3d' % s for s in range(1, 12)))
    for p in range(1, 7):
        row = ''
        for s in range(1, 12):
            row += ('N   ' if (p, s) in slot_name else 'x   ') if (p, s) in slots else '.   '
        w('  Plane %d   %s  %d/11' % (p, row, sum(1 for s in range(1, 12) if (p, s) in slots)))
    w('')
    w('EVERY PLANE/SLOT HEARD')
    w('  %-12s %-6s %-7s %-17s %-17s %-8s %s' % ('plane/slot', 'passes', 'frames', 'first heard', 'last heard', 'best dB', 'name'))
    for (p, s), lst in sorted(slots.items()):
        nm = ', '.join(sorted(slot_name.get((p, s), []))) or '-'
        w('  P%d S%-9s %-6d %-7d %-17s %-17s %-8.1f %s'
          % (p, '%02d' % s, len(lst), sum(x['frames'] for x in lst), fmt(min(x['first'] for x in lst)),
             fmt(max(x['last'] for x in lst)), max(x['best_snr'] for x in lst), nm))
    w('')
    w('SATELLITES IDENTIFIED BY NAME')
    w('  %-13s %-10s %-11s %-9s %-17s %-17s %s' % ('name', 'network ID', 'plane/slot', 'pos fixes', 'first seen', 'last seen', 'best dB'))
    for nm in sorted(names, key=lambda x: int(re.sub(r'\D', '', x) or 0)):
        n = names[nm]
        w('  %-13s %-10s %-11s %-9d %-17s %-17s %.1f'
          % (nm, ','.join('%03d' % i for i in sorted(n['net_ids'])) or '-',
             ','.join('P%d S%02d' % ps for ps in sorted(n['slots'])) or '-', len(n['positions']),
             fmt(min(t for t, _ in n['seen'])), fmt(max(t for t, _ in n['seen'])), max(x for _, x in n['seen'])))
    w('  (network ID = the number the satellite uses for itself in ring alerts)')
    w('')
    w('POSITION FIXES  (ring alerts that reported where the satellite was)')
    for c in caps:
        for p in c['positions']:
            w('  %-17s net ID %03d  lat %+6.2f lon %+7.2f  %d km  %4.1f dB  -> %s'
              % (fmt(p['time']), p['net_id'], p['lat'], p['lon'], p['alt_km'], p['snr'],
                 p['name'] or 'no orbit match'))
    w('')
    w('DOPPLER MATCHES  (plane/slot passes with 3+ frames; confident = clear best fit)')
    for c in caps:
        for s in c['slots']:
            if s['name']:
                w('  %-17s P%d S%02d  %3d frames  -> %-13s %-9s fit %d Hz'
                  % (fmt(s['first']), s['plane'], s['slot'], s['frames'], s['name'], s['confidence'], s['rms_hz']))
    w('')
    w('CAPTURES')
    for c in caps:
        dec = sum(n for k, n in c['counts'].items() if k != 'RAW')
        w('  %-24s %-17s %5.2f h  %5d decoded  %5d RAW  %2d fixes  clock %s'
          % (c['capture'], fmt(c['start_utc']), c['hours'], dec, c['counts'].get('RAW', 0), len(c['positions']), c['clock']))
    return '\n'.join(out) + '\n'


def main():
    if HOME is None:
        sys.exit('No antenna location yet - start the tracker once and enter it (it is saved in settings.json).')
    redo = '--redo' in sys.argv
    ready = {a[8:] for a in sys.argv if a.startswith('--ready=')}   # captures known to be finished
    data = json.load(open(DATA)) if os.path.exists(DATA) else {}
    if redo and data:     # captures whose files are gone keep their old results
        shutil.copy(DATA, DATA[:-5] + '.backup.json')
    sats = {s['key']: None for s in systems.ORDER}     # loaded when first needed
    recording = set()
    for name, files in sorted(storage.captures().items()):
        stem = os.path.join(paths.CAPTURES, name)
        bits = stem + '.bits'
        if os.path.exists(bits) and name not in ready and time.time() - os.path.getmtime(bits) < 20:
            recording.add(name)         # still being recorded
            continue
        if name in data and not redo:
            continue
        if systems.of_capture(name).get('system') == 'meteor':
            continue        # pictures, not frames: images.py makes them (receiver.py runs it after Stop)
        live = stem + '.live'
        if systems.of_capture(name) is not systems.IRIDIUM and name not in ready and os.path.exists(live) \
                and time.time() - os.path.getmtime(live) < 20:
            recording.add(name)
            continue
        if not frames_source(stem) and os.path.exists(bits) and os.path.getsize(bits) >= 200:
            subprocess.run([sys.executable, PARSER, '--no-stats', bits], cwd=paths.CAPTURES, capture_output=True)
        src = frames_source(stem)
        if not src:
            continue
        print('analyzing %s ...' % name)
        key = systems.of_capture(name)['key']
        if systems.get(key).get('system') == 'ham':
            load_tles(key)                  # refresh the name list
            cap = analyze_ham(stem, norad_names())
        elif systems.get(key).get('system') == 'gnss':
            load_tles(key)
            cap = analyze_gnss(stem)
        else:
            if sats[key] is None:
                sats[key] = load_tles(key)
            cap = analyze_orbcomm(stem, sats[key]) if key == 'orbcomm' else analyze(stem, sats[key])
        if not src.endswith(storage.REDUCED):
            storage.write_reduced(stem, src, cap['counts'], cap['hours'])
        data[name] = cap
    json.dump(data, open(DATA, 'w'), separators=(',', ':'))
    caps = [c for _, c in sorted(data.items()) if sum(c['counts'].values()) and c.get('system', 'iridium') == 'iridium']
    open(REPORT, 'w').write(build_report(caps))     # the text report covers Iridium
    print('catalog written to %s' % REPORT)
    n, freed = storage.enforce(set(data), protect=recording)
    if n:
        print('deleted %d raw capture files (%s) - see storage settings' % (n, storage.fmt_bytes(freed)))


if __name__ == '__main__':
    main()
