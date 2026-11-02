"""Build the bundled Python runtime (the python folder) from environment.yml.

Usage:  python build\\build_runtime.py [--conda PATH\\TO\\mamba.exe] [--force]

Needs conda or mamba (radioconda, Miniforge...). Creates <repo>\\python, then removes what the app
never uses at runtime (debug symbols, C headers, static libraries, Qt Quick, a compiler library),
taking it from about 1.1 GB to about 550 MB.
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFIX = os.path.join(ROOT, 'python')
ENV_FILE = os.path.join(ROOT, 'build', 'environment.yml')

# relative to the runtime folder; everything here is for building software, not running it
TRIM = ['include', 'Library/include', 'Library/lib/cmake', 'Library/lib/pkgconfig', 'Library/share/doc',
        'Library/share/man', 'Library/share/info', 'Library/share/gir-1.0', 'Library/qml',
        'Library/bin/libclang-13.dll']


def find_conda():
    for name in ('mamba', 'conda'):
        exe = shutil.which(name)
        if exe:
            return exe
    for guess in (r'%USERPROFILE%\radioconda\Scripts\mamba.exe', r'%USERPROFILE%\miniforge3\Scripts\mamba.exe',
                  r'%USERPROFILE%\miniforge3\Scripts\conda.exe'):
        exe = os.path.expandvars(guess)
        if os.path.exists(exe):
            return exe
    sys.exit('conda/mamba not found - pass --conda PATH')


def trim(prefix):
    for rel in TRIM:
        path = os.path.join(prefix, *rel.split('/'))
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)
    for pattern in ('**/*.pdb', 'Library/lib/*.lib'):
        for f in glob.glob(os.path.join(prefix, pattern), recursive=True):
            os.remove(f)
    # scipy's test suites aren't needed (numpy's are: numpy 2.2 imports one of them at runtime)
    for d in glob.glob(os.path.join(prefix, 'Lib', 'site-packages', 'scipy', '**', 'tests'), recursive=True):
        shutil.rmtree(d, ignore_errors=True)
    for d in glob.glob(os.path.join(prefix, '**', '__pycache__'), recursive=True):
        shutil.rmtree(d, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--conda', help='path to mamba.exe or conda.exe')
    ap.add_argument('--force', action='store_true', help='replace an existing python folder')
    ap.add_argument('--prefix', default=PREFIX, help='where to build (default: <repo>\\python)')
    a = ap.parse_args()
    if os.path.exists(a.prefix):
        if not a.force:
            sys.exit('%s already exists - use --force to rebuild it' % a.prefix)
        shutil.rmtree(a.prefix)
    conda = a.conda or find_conda()
    print('creating the runtime with', conda)
    subprocess.run([conda, 'env', 'create', '-y', '-p', a.prefix, '-f', ENV_FILE], check=True)
    print('trimming developer files...')
    trim(a.prefix)
    print('done:', a.prefix)


if __name__ == '__main__':
    main()
