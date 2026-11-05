"""Light and dark colours: one theme for the whole window (panels, lists, buttons) and one for the map
(and the globe's Earth), each switched with a button and remembered in settings.json.

The plots - spectrum and waterfall, sky plot, Doppler curves, the globe's space - stay dark in both,
like an instrument display. Widgets with their own colours are styled with style(widget, template):
the template's %(NAME)s colours are filled in from PAL now and again whenever the theme changes.
"""
import json
import os
import weakref

import paths

PALETTES = {
    'dark': dict(BG='#14171c', PANEL='#1b1f26', SURFACE='#232832', BORDER='#2f3541', TEXT='#e6e9ef',
                 MUTED='#8b93a1', ACCENT='#4c9aff', ALERT='#ff6b6b', GOOD='#4cd07d', ALT='#1f242c',
                 HOVER='#2c323d', HOVER_BORDER='#3d4452', PRESSED='#353c49', SCROLL='#3a4150',
                 SCROLL_HOVER='#4a5263', STATUS='#101317', IDLE='#4a5263', PICK='#16324f', UP='#1c5434',
                 ALERT_BG='#3a1d22', ROW_HEAD='#262b35', DISABLED_BG='#3a4150', DISABLED_TEXT='#9aa1ad',
                 WARN='#ffb454', SEL_BG='#13303c'),
    'light': dict(BG='#eef1f5', PANEL='#ffffff', SURFACE='#e4e8ee', BORDER='#c9d0da', TEXT='#1d232c',
                  MUTED='#5d6675', ACCENT='#2f7de1', ALERT='#d64550', GOOD='#1f9d55', ALT='#f4f6f9',
                  HOVER='#d9dfe7', HOVER_BORDER='#aeb7c4', PRESSED='#cdd4de', SCROLL='#bcc4cf',
                  SCROLL_HOVER='#a3adba', STATUS='#e2e6ec', IDLE='#b3bcc8', PICK='#dbe9fb', UP='#c5ebd3',
                  ALERT_BG='#fbe1e4', ROW_HEAD='#e8ecf1', DISABLED_BG='#d5dae1', DISABLED_TEXT='#8a929e',
                  WARN='#b86e00', SEL_BG='#e1f3fa'),
}
# the map's own label and key colours (it can be light while the window is dark, or the other way round)
MAP_INK = {
    'dark': dict(text='#e6e9ef', muted='#8b93a1', box=(24, 28, 35, 225), box_edge=(60, 66, 78)),
    'light': dict(text='#1d232c', muted='#4d5563', box=(255, 255, 255, 230), box_edge=(180, 188, 200)),
}

name = 'dark'                       # the window's theme now
PAL = dict(PALETTES[name])          # its colours (updated in place, so every module sees the change)
_styled = weakref.WeakKeyDictionary()


def style(widget, template):
    """set a widget's style sheet from a template with %(NAME)s colours, and redo it when the theme changes"""
    _styled[widget] = template
    widget.setStyleSheet(template % PAL)


def set_theme(theme):
    """switch the window's colours (PAL) and restyle the widgets styled with style()"""
    global name
    name = theme
    PAL.clear()
    PAL.update(PALETTES[theme])
    for widget, template in list(_styled.items()):
        try:
            widget.setStyleSheet(template % PAL)
        except RuntimeError:        # the Qt side of it is gone
            pass


def _path():
    return os.path.join(paths.DATA, 'settings.json')


def load_ui():
    """the window settings saved in settings.json ('ui': theme, map theme, chime...)"""
    try:
        ui = json.load(open(_path())).get('ui', {})
    except (OSError, ValueError, AttributeError):
        ui = {}
    return ui if isinstance(ui, dict) else {}


def save_ui(**values):
    try:
        allset = json.load(open(_path()))
    except (OSError, ValueError):
        allset = {}
    allset['ui'] = dict(allset.get('ui', {}), **values)
    os.makedirs(paths.DATA, exist_ok=True)
    json.dump(allset, open(_path(), 'w'), indent=1)


# ---- remembered sizes: where the user dragged each divider (settings.json 'ui' 'splitters')
_split_timers = {}


def restore_splitter(splitter, key, default):
    """set a splitter's sizes to the ones the user last chose (or default)"""
    sizes = load_ui().get('splitters', {}).get(key)
    if not (isinstance(sizes, list) and len(sizes) == splitter.count() and all(isinstance(v, int) and v >= 0
                                                                               for v in sizes) and sum(sizes)):
        sizes = default
    splitter.setSizes(list(sizes))


def remember_splitter(splitter, key, default):
    """restore a splitter's sizes now, and save them (a moment after) whenever its divider is dragged"""
    from PyQt5 import QtCore
    restore_splitter(splitter, key, default)
    timer = QtCore.QTimer(splitter)
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: save_splitter(splitter, key))
    _split_timers[key] = timer
    splitter.splitterMoved.connect(lambda *_: timer.start(400))


def save_splitter(splitter, key):
    sizes = splitter.sizes()
    if sum(sizes):
        save_ui(splitters=dict(load_ui().get('splitters', {}), **{key: [int(v) for v in sizes]}))


def load():
    """(window theme, map theme) saved last time - dark by default"""
    ui = load_ui()
    pick = lambda v: v if v in PALETTES else 'dark'     # noqa: E731
    return pick(ui.get('theme')), pick(ui.get('map_theme'))


def save(theme, map_theme):
    save_ui(theme=theme, map_theme=map_theme)
