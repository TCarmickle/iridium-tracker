r"""Where Satellite Tracker keeps your data: a data folder next to the app folder, so updating or
replacing the program never touches it.

  data\settings.json        antenna location and storage limits
  data\catalog-data.json    everything heard (the tracker's main data)
  data\catalog.txt          the same as a readable report
  data\iridium-names.tle    public satellite name list (refreshed automatically)
  data\captures\            one set of files per receiver run (see storage.py)
  data\tiles\               map picture cache
"""
import glob
import os
import shutil

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)
DATA = os.environ.get('IRIDIUM_TRACKER_DATA') or os.path.join(ROOT, 'data')
CAPTURES = os.path.join(DATA, 'captures')
TILES = os.path.join(DATA, 'tiles')
ERROR_LOG = os.path.join(DATA, 'tracker-error.log')

_OLD_FILES = ('settings.json', 'catalog-data.json', 'catalog-data.backup.json', 'catalog.txt',
              'iridium-names.tle', 'tracker-error.log')


def migrate():
    """move data that older versions kept inside the app folder into the data folder"""
    os.makedirs(CAPTURES, exist_ok=True)
    for name in _OLD_FILES:
        old, new = os.path.join(APP, name), os.path.join(DATA, name)
        if os.path.exists(old) and not os.path.exists(new):
            try:
                shutil.move(old, new)
            except OSError:
                pass
    for old in glob.glob(os.path.join(APP, 'output-*')):
        new = os.path.join(CAPTURES, os.path.basename(old))
        if not os.path.exists(new):
            try:
                shutil.move(old, new)
            except OSError:
                pass        # still being written by an older receiver
    old_tiles = os.path.join(APP, 'tiles')
    if os.path.isdir(old_tiles):
        for d, _, files in os.walk(old_tiles):
            for f in files:
                src = os.path.join(d, f)
                dst = os.path.join(TILES, os.path.relpath(src, old_tiles))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if not os.path.exists(dst):
                    shutil.move(src, dst)
        shutil.rmtree(old_tiles, ignore_errors=True)


migrate()
