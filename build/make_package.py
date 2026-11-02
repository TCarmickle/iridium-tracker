"""Make the shareable zip: dist\\IridiumTracker.zip (everything needed to run, no personal data).

Usage:  python build\\make_package.py
Needs the python folder (build_runtime.py) and LICENSES\\third-party (collect_licenses.py).
"""
import fnmatch
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'dist', 'IridiumTracker.zip')
INCLUDE = ['Iridium Tracker.bat', 'Iridium Console.bat', 'README.txt', 'LICENSES', 'app', 'python']
# never shipped: your location, captures, catalog, map cache, logs
EXCLUDE = ['app/settings.json', 'app/output*', 'app/catalog-data*.json', 'app/catalog.txt',
           'app/iridium-names.tle', 'app/tiles/*', 'app/tracker-error.log', '*/__pycache__/*', '*.pyc']


def excluded(rel):
    rel = rel.replace(os.sep, '/')
    return any(fnmatch.fnmatch(rel, pat) for pat in EXCLUDE)


def main():
    for need in ('python', os.path.join('LICENSES', 'third-party')):
        if not os.path.isdir(os.path.join(ROOT, need)):
            sys.exit('missing %s - see BUILDING.md' % need)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    n = 0
    with zipfile.ZipFile(OUT, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for item in INCLUDE:
            path = os.path.join(ROOT, item)
            files = [path] if os.path.isfile(path) else \
                [os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs]
            for f in files:
                rel = os.path.relpath(f, ROOT)
                if not excluded(rel):
                    z.write(f, 'IridiumTracker/' + rel.replace(os.sep, '/'))
                    n += 1
    print('%s: %d files, %.0f MB' % (OUT, n, os.path.getsize(OUT) / 2 ** 20))


if __name__ == '__main__':
    main()
