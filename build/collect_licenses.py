"""Copy the license texts of every package in the bundled runtime into LICENSES\\third-party
and write LICENSES\\THIRD-PARTY-PACKAGES.txt.

Usage:  python build\\collect_licenses.py [--pkgs PATH\\TO\\conda\\pkgs]
The license files come from conda's package cache (the pkgs folder of the conda that built python\\).
"""
import argparse
import glob
import json
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_pkgs():
    for guess in (os.environ.get('CONDA_PKGS_DIRS', '').split(os.pathsep)[0], r'%USERPROFILE%\radioconda\pkgs',
                  r'%USERPROFILE%\miniforge3\pkgs'):
        d = os.path.expandvars(guess) if guess else ''
        if d and os.path.isdir(d):
            return d
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pkgs', default=default_pkgs(), help="conda's package cache folder")
    a = ap.parse_args()
    out = os.path.join(ROOT, 'LICENSES', 'third-party')
    os.makedirs(out, exist_ok=True)
    rows, missing = [], []
    for meta in sorted(glob.glob(os.path.join(ROOT, 'python', 'conda-meta', '*.json'))):
        d = json.load(open(meta))
        rows.append('%-36s %-16s %s' % (d['name'], d['version'], d.get('license', '?')))
        src = os.path.join(a.pkgs or '', os.path.basename(meta)[:-5], 'info', 'licenses')
        if a.pkgs and os.path.isdir(src):
            shutil.copytree(src, os.path.join(out, d['name']), dirs_exist_ok=True)
        else:
            missing.append(d['name'])
    with open(os.path.join(ROOT, 'LICENSES', 'THIRD-PARTY-PACKAGES.txt'), 'w') as f:
        f.write('Open-source packages bundled in the python folder (from conda-forge, https://conda-forge.org).\n'
                'Source code for each: https://github.com/conda-forge/<name>-feedstock lists the upstream source.\n'
                'License texts are in the third-party folder.\n\n')
        f.write('%-36s %-16s %s\n%s\n' % ('package', 'version', 'license', '-' * 90))
        f.write('\n'.join(rows) + '\n')
    print('%d packages; license texts copied for %d (none found for: %s)'
          % (len(rows), len(rows) - len(missing), ', '.join(missing) or '-'))


if __name__ == '__main__':
    main()
