"""Satellite pictures: Meteor-M passes turned into images by SatDump, kept in data\\images (one folder per pass).

  python images.py --decode meteor-<stamp>     make the pictures of a received pass (receiver.py runs this
                                               after Stop): SatDump's Meteor LRPT pipeline on the recording
  python images.py --import FOLDER             copy a SatDump output folder in (e.g. a pass received with
                                               SatDump itself)

SatDump (Aang23 and contributors, GPL-3.0, https://www.satdump.org) does the decoding: the installed copy is
used (C:\\Program Files\\SatDump), or one in app\\satdump, or the path in settings.json 'images' 'satdump'.
Each pass folder has a pass.json: {source received|imported, name, start_utc, satellite, frames, images,
note}. The recording (~1 GB) is deleted once its pictures are made; one that gave none is kept (only the
newest such) so it can be tried again.
"""
import argparse
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

import paths

IMAGES = os.path.join(paths.DATA, 'images')
PIPELINE = 'meteor_m2-x_lrpt'
BASEBAND_EXT = '.s16'
BASEBAND_RATE = 256000          # meteor_rx.py's recording
PICTURE_EXT = ('.png', '.jpg', '.jpeg')
NO_WINDOW = 0x08000000


def settings():
    try:
        return json.load(open(os.path.join(paths.DATA, 'settings.json'))).get('images', {})
    except (OSError, ValueError, AttributeError):
        return {}


def find_satdump():
    """satdump.exe (the command-line one), or None"""
    here = os.path.dirname(os.path.abspath(__file__))
    for p in [settings().get('satdump', ''), os.path.join(here, 'satdump', 'satdump.exe'),
              os.path.join(here, 'satdump', 'bin', 'satdump.exe'),
              os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'SatDump', 'bin', 'satdump.exe'),
              os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'SatDump', 'bin', 'satdump.exe')]:
        if p and os.path.isfile(p):
            return p
    return shutil.which('satdump')


def pictures(folder):
    """the picture files of a pass folder, the most useful first (colour composites, then single channels)"""
    out = [p for p in glob.glob(os.path.join(folder, '**', '*'), recursive=True) if p.lower().endswith(PICTURE_EXT)]

    def rank(p):
        n = os.path.basename(p).lower()
        return (0 if 'rgb' in n or 'color' in n or 'colour' in n else 1 if 'msu-mr-' not in n else 2,
                'projected' in n or 'map' in n, n)
    return sorted(out, key=rank)


def nice_name(path):
    """'rgb_MSU-MR_221_corrected.png' -> 'RGB MSU-MR 221 corrected'"""
    n = os.path.splitext(os.path.basename(path))[0]
    n = re.sub(r'[_]+', ' ', n).strip()
    return re.sub(r'^rgb\b', 'RGB', n, flags=re.I)


def _write_meta(folder, meta):
    meta['images'] = len(pictures(folder))
    with open(os.path.join(folder, 'pass.json'), 'w') as f:
        json.dump(meta, f, indent=1)
    return meta


def _satellite(folder):
    try:
        d = json.load(open(os.path.join(folder, 'dataset.json')))
        return d.get('satellite') or None, d.get('timestamp') or 0
    except (OSError, ValueError):
        return None, 0


def passes():
    """every pass folder, newest first: dicts with folder and the pass.json fields"""
    out = []
    for folder in glob.glob(os.path.join(IMAGES, '*')):
        if not os.path.isdir(folder):
            continue
        try:
            meta = json.load(open(os.path.join(folder, 'pass.json')))
        except (OSError, ValueError):
            meta = {'name': os.path.basename(folder), 'source': 'imported'}
        meta['folder'] = folder
        meta.setdefault('start_utc', datetime.datetime.fromtimestamp(os.path.getmtime(folder), datetime.timezone.utc)
                        .isoformat(timespec='seconds'))
        out.append(meta)
    return sorted(out, key=lambda m: m['start_utc'], reverse=True)


def decode(capname, satdump=None, log=print):
    """SatDump's Meteor LRPT pipeline on a received pass -> data\\images\\<capname>; returns its pass.json"""
    stem = os.path.join(paths.CAPTURES, capname)
    baseband = stem + BASEBAND_EXT
    out = os.path.join(IMAGES, capname)
    os.makedirs(out, exist_ok=True)
    start = None
    try:
        start = datetime.datetime.strptime(open(stem + '.start').read().strip()[:19], '%Y-%m-%d %H:%M:%S') \
            .replace(tzinfo=datetime.timezone.utc)
    except (OSError, ValueError):
        pass
    meta = {'source': 'received', 'name': capname, 'capture': capname,
            'start_utc': (start or datetime.datetime.now(datetime.timezone.utc)).isoformat(timespec='seconds'),
            'satellite': None, 'frames': 0, 'note': ''}
    satdump = satdump or find_satdump()
    if not satdump:
        meta['note'] = ('SatDump is not installed - get it from https://www.satdump.org, then press "Make pictures '
                        'again" in Images (the recording is kept)')
        return _write_meta(out, meta)
    if not os.path.exists(baseband) or os.path.getsize(baseband) < BASEBAND_RATE * 4:
        meta['note'] = 'nothing was recorded'
        return _write_meta(out, meta)
    secs = os.path.getsize(baseband) / (BASEBAND_RATE * 4)
    log('Making pictures from %s (%.0f s recorded) with SatDump...' % (capname, secs))
    args = [satdump, PIPELINE, 'baseband', baseband, out, '--samplerate', str(BASEBAND_RATE),
            '--baseband_format', 's16']
    if start:
        args += ['--start_timestamp', '%.0f' % start.timestamp()]
    with open(os.path.join(out, 'satdump.log'), 'w', errors='replace') as lf:
        r = subprocess.run(args, stdout=lf, stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
                           cwd=os.path.dirname(satdump))
    cadu = os.path.join(out, PIPELINE + '.cadu')
    meta['frames'] = os.path.getsize(cadu) // 1024 if os.path.exists(cadu) else 0
    meta['satellite'] = _satellite(out)[0]
    if meta['satellite'] == 'Unknown Meteor':
        meta['satellite'] = None
    n = len(pictures(out))
    if r.returncode != 0:
        meta['note'] = 'SatDump stopped with an error (code %d) - see satdump.log in the pass folder' % r.returncode
    elif n:
        meta['note'] = '%d frames decoded' % meta['frames']
    elif meta['frames']:
        meta['note'] = '%d frames decoded, but too few for a picture' % meta['frames']
    else:
        meta['note'] = ('no signal decoded - check the antenna (about 21 in, upright, outdoors) and that a Meteor '
                        'satellite was passing')
    meta = _write_meta(out, meta)
    # the recording: gone once it gave pictures; one that gave none is kept for another try (only the newest)
    if n:
        _remove(baseband)
    else:
        for other in glob.glob(os.path.join(paths.CAPTURES, 'meteor-*' + BASEBAND_EXT)):
            if other != baseband:
                _remove(other)
    log('%s: %s' % (capname, meta['note']))
    return meta


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def import_folder(src):
    """copy a SatDump output folder (or a folder of pictures) in; returns its pass.json"""
    src = os.path.abspath(src)
    if not pictures(src):
        raise ValueError('no pictures (.png / .jpg) in %s' % src)
    base = re.sub(r'[^\w.-]+', '_', os.path.basename(src.rstrip('\\/')))[:60] or 'pass'
    dst = os.path.join(IMAGES, 'imported-' + base)
    k = 2
    while os.path.exists(dst):
        dst = os.path.join(IMAGES, 'imported-%s-%d' % (base, k))
        k += 1
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns('*.cadu', '*.s16', '*.s8', '*.f32', '*.cs16', '*.wav'))
    sat, ts = _satellite(dst)
    when = datetime.datetime.fromtimestamp(ts or os.path.getmtime(src), datetime.timezone.utc)
    return _write_meta(dst, {'source': 'imported', 'name': os.path.basename(src), 'from': src,
                             'start_utc': when.isoformat(timespec='seconds'),
                             'satellite': sat if sat != 'Unknown Meteor' else None, 'frames': None,
                             'note': 'imported from %s' % src})


def main():
    ap = argparse.ArgumentParser(description='Satellite pictures (Meteor-M, with SatDump)')
    ap.add_argument('--decode', help='a received pass: meteor-<date-time>')
    ap.add_argument('--import', dest='imp', help='a SatDump output folder to copy in')
    a = ap.parse_args()
    os.makedirs(IMAGES, exist_ok=True)
    if a.decode:
        decode(a.decode)        # (its result, good or not, is in the pass's pass.json)
    if a.imp:
        print(import_folder(a.imp))


if __name__ == '__main__':
    main()
