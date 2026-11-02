"""Running catalog of every Iridium satellite heard across all captures.

Usage: python catalog.py            (adds any new captures, then writes catalog.txt)
       python catalog.py --redo     (re-analyzes every capture from scratch)

Each capture is analyzed once and stored in catalog-data.json, so results don't change
later when the satellite orbit data (TLEs) gets older or is refreshed.
Satellites are named two ways:
  position  - a ring alert reported its position; the satellite overhead there is named
  Doppler   - the frequency curve of a plane/slot's ITL frames matches one satellite's orbit
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
# iridium-toolkit: inside this folder (a packaged copy) or next to it
TOOLKIT = next((d for d in (os.path.join(HERE, 'iridium-toolkit'), os.path.join(HERE, '..', 'iridium-toolkit'))
                if os.path.exists(os.path.join(d, 'iridium-parser.py'))), os.path.join(HERE, 'iridium-toolkit'))
PARSER = os.path.join(TOOLKIT, 'iridium-parser.py')
# public orbit list, used only to put names on satellites (positions always come from our own fixes);
# kept fresh from CelesTrak, with SatDump's copy as a fallback
TLE_FILE = os.path.join(paths.DATA, 'iridium-names.tle')
TLE_URL = 'https://celestrak.org/NORAD/elements/gp.php?GROUP=iridium-NEXT&FORMAT=tle'
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


def refresh_tles(max_age_days=2):
    """download a fresh name list now and then (CelesTrak asks for no more than one fetch every couple of hours)"""
    if os.path.exists(TLE_FILE) and time.time() - os.path.getmtime(TLE_FILE) < max_age_days * 86400:
        return
    try:
        import requests
        r = requests.get(TLE_URL, timeout=15, headers={'User-Agent': 'IridiumTracker/1.0'})
        if r.ok and 'IRIDIUM' in r.text:
            open(TLE_FILE, 'w', newline='\n').write(r.text.replace('\r\n', '\n'))
    except Exception:
        pass        # offline: keep using what we have


def load_tles():
    refresh_tles()
    for path in (TLE_FILE, SATDUMP_TLES):
        if os.path.exists(path):
            lines = open(path).read().splitlines()
            return [Sat(lines[i].strip(), lines[i + 1], lines[i + 2])
                    for i in range(len(lines) - 2) if lines[i].startswith('IRIDIUM')]
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
    cap = {'capture': os.path.basename(stem), 'start_utc': iso(start), 'clock': how,
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
    sats = load_tles()
    recording = set()
    for name, files in sorted(storage.captures().items()):
        stem = os.path.join(paths.CAPTURES, name)
        bits = stem + '.bits'
        if os.path.exists(bits) and name not in ready and time.time() - os.path.getmtime(bits) < 20:
            recording.add(name)         # still being recorded
            continue
        if name in data and not redo:
            continue
        if not frames_source(stem) and os.path.exists(bits) and os.path.getsize(bits) >= 200:
            subprocess.run([sys.executable, PARSER, '--no-stats', bits], cwd=paths.CAPTURES, capture_output=True)
        src = frames_source(stem)
        if not src:
            continue
        print('analyzing %s ...' % name)
        cap = analyze(stem, sats)
        if not src.endswith(storage.REDUCED):
            storage.write_reduced(stem, src, cap['counts'], cap['hours'])
        data[name] = cap
    json.dump(data, open(DATA, 'w'), separators=(',', ':'))
    caps = [c for _, c in sorted(data.items()) if sum(c['counts'].values())]
    open(REPORT, 'w').write(build_report(caps))
    print('catalog written to %s' % REPORT)
    n, freed = storage.enforce(set(data), protect=recording)
    if n:
        print('deleted %d raw capture files (%s) - see storage settings' % (n, storage.fmt_bytes(freed)))


if __name__ == '__main__':
    main()
