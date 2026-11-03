"""Keeping Satellite Tracker's data small.

Each receiver run makes a set of capture files in data\\captures:
  output-<stamp>.bits        raw bursts from the decoder (every burst, including voice and messaging)
  output-<stamp>.live        every burst decoded
  output-<stamp>.log         the decoder's messages (a status line every 10 s)
  output-<stamp>.start       when the capture's clock started (tiny, kept)
Once a capture is in the catalog it gets a small output-<stamp>.frames.gz holding only what the
app uses - ring alerts (phone paging IDs removed) and time & location frames - so the catalog
can always be rebuilt. The raw files are then deleted after keep_raw_days, or sooner when they
exceed max_raw_mb (oldest first). The map picture cache is held under max_tiles_mb.
"""
import glob
import gzip
import json
import os
import re
import shutil
import time

import paths
import systems

DEFAULTS = {'keep_raw_days': 7, 'max_raw_mb': 1024, 'max_tiles_mb': 200, 'min_free_gb': 1.0}
RAW_EXT = ('.bits', '.parsed', '.live', '.log')
REDUCED = '.frames.gz'
KEEP_KINDS = systems.keep_kinds()     # every system's position / pass frames
HEADER = '#iridium-tracker-frames '


def settings():
    try:
        s = json.load(open(os.path.join(paths.DATA, 'settings.json'))).get('storage', {})
    except (OSError, ValueError, AttributeError):
        s = {}
    return {k: type(v)(s.get(k, v)) for k, v in DEFAULTS.items()}


def save_settings(new):
    path = os.path.join(paths.DATA, 'settings.json')
    try:
        allset = json.load(open(path))
    except (OSError, ValueError):
        allset = {}
    allset['storage'] = {k: new[k] for k in DEFAULTS}
    json.dump(allset, open(path, 'w'), indent=1)


# ---- the reduced frames file
def write_reduced(stem, frames_path, counts, hours):
    """keep only ring alerts and time & location frames, without phone paging IDs"""
    out = stem + REDUCED
    tmp = out + '.tmp'
    with gzip.open(tmp, 'wt', newline='\n') as z:
        z.write(HEADER + json.dumps({'counts': counts, 'hours': hours}) + '\n')
        for line in open(frames_path, errors='replace'):
            if line.startswith(KEEP_KINDS):
                if line.startswith('IRA:'):
                    line = re.sub(r' PAGE\(.*', '', line.rstrip('\r\n'))
                z.write(line.rstrip('\r\n') + '\n')
    os.replace(tmp, out)
    return out


def read_reduced(path):
    """(header dict, frame lines) of a .frames.gz file"""
    with gzip.open(path, 'rt', errors='replace') as z:
        lines = z.read().splitlines()
    meta = json.loads(lines[0][len(HEADER):]) if lines and lines[0].startswith(HEADER) else {}
    return meta, [l for l in lines if not l.startswith('#')]


# ---- what's on disk
def _size(files):
    return sum(os.path.getsize(f) for f in files if os.path.exists(f))


def captures():
    """capture name -> its files"""
    caps = {}
    for f in glob.glob(os.path.join(paths.CAPTURES, '*-*')):
        name = re.match(r'((?:%s)\d{8}-\d{6})' % '|'.join(map(re.escape, systems.capture_prefixes())),
                        os.path.basename(f))
        if name:
            caps.setdefault(name.group(1), []).append(f)
    return caps


def raw_files(files):
    return [f for f in files if f.endswith(RAW_EXT)]


def tile_files():
    return [os.path.join(d, f) for d, _, fs in os.walk(paths.TILES) for f in fs]


def usage():
    caps = captures()
    raw = [f for fs in caps.values() for f in raw_files(fs)]
    other = [f for fs in caps.values() for f in fs if not f.endswith(RAW_EXT)]
    data_files = [os.path.join(paths.DATA, f) for f in os.listdir(paths.DATA)] if os.path.isdir(paths.DATA) else []
    u = {'raw': _size(raw), 'raw_captures': sum(1 for fs in caps.values() if raw_files(fs)),
         'reduced': _size(other), 'captures': len(caps),
         'catalog': _size([f for f in data_files if os.path.isfile(f)]), 'tiles': _size(tile_files()),
         'free': shutil.disk_usage(paths.DATA).free if os.path.isdir(paths.DATA) else 0}
    u['total'] = u['raw'] + u['reduced'] + u['catalog'] + u['tiles']
    return u


def free_gb():
    return shutil.disk_usage(paths.DATA).free / 2 ** 30


# ---- clean-up
def enforce(catalogued, protect=(), s=None):
    """delete raw capture files that are no longer needed. Only captures that are in the catalog
    and have their reduced frames file are touched; `protect` names are never touched.
    Returns (files deleted, bytes freed)."""
    s = s or settings()
    caps = captures()
    n = freed = 0
    for name, files in list(caps.items()):      # runs that received nothing (e.g. the app closed mid-run)
        if name in catalogued or name in protect or any(f.endswith(REDUCED) for f in files):
            continue
        bits = [f for f in files if f.endswith('.bits')]
        if time.time() - max(os.path.getmtime(f) for f in files) > 600 and not _has_frames(files) \
                and all(os.path.getsize(f) < 200 for f in bits):
            for f in files:
                try:
                    freed += os.path.getsize(f)
                    os.remove(f)
                    n += 1
                except OSError:
                    pass
            del caps[name]
    done = []
    for name, files in caps.items():
        raw = raw_files(files)
        if not raw or name not in catalogued or name in protect:
            continue
        if time.time() - max(os.path.getmtime(f) for f in raw) < 120:
            continue        # touched moments ago: may still be recording or decoding
        if not any(f.endswith(REDUCED) for f in files) and _has_frames(raw):
            continue        # not reduced yet - keep everything
        done.append((max(os.path.getmtime(f) for f in raw), name, raw))
    done.sort()
    total = sum(_size(r) for _, _, r in done)
    cutoff = time.time() - s['keep_raw_days'] * 86400
    for mtime, name, raw in done:
        if mtime < cutoff or total > s['max_raw_mb'] * 2 ** 20:
            size = _size(raw)
            for f in raw:
                try:
                    os.remove(f)
                    n += 1
                except OSError:
                    pass
            total -= size
            freed += size
    return n, freed


def _has_frames(raw):
    """a capture with no decoded frames at all has nothing to reduce"""
    return any(f.endswith(('.parsed', '.live')) and os.path.getsize(f) > 0 for f in raw)


def prune_tiles(max_mb=None):
    """drop the least recently used map pictures once the cache is over its limit"""
    max_bytes = (max_mb if max_mb is not None else settings()['max_tiles_mb']) * 2 ** 20
    files = [(os.path.getmtime(f), os.path.getsize(f), f) for f in tile_files()]
    total = sum(s for _, s, _ in files)
    if total <= max_bytes:
        return 0
    files.sort()
    n = 0
    for _, size, f in files:
        if total <= max_bytes * 0.9:
            break
        try:
            os.remove(f)
            total -= size
            n += 1
        except OSError:
            pass
    return n


def fmt_bytes(b):
    for unit in ('bytes', 'KB', 'MB', 'GB'):
        if b < 1024 or unit == 'GB':
            return ('%.0f %s' if unit in ('bytes', 'KB') else '%.1f %s') % (b, unit)
        b /= 1024
