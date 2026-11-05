"""Pop-out windows: any part of the window (a view, the waveform, the waterfall, the timeline) can be taken out into
a window of its own and put back - closing that window returns it to where it was. Window sizes and places are
remembered (settings.json 'ui' 'popouts').

ViewDeck is the main view: one of several views (the map, the globe, the sky, the signal) fills it, picked with
the buttons above it; any of them can be popped out, and the main view then says where it went.
"""
from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import Qt

import theme

POP = '⧉'          # the pop-out symbol on the buttons


class PopWindow(QtWidgets.QWidget):
    """a window holding one popped-out part; closing it hands the part back (the closed signal)"""
    closed = QtCore.pyqtSignal()

    def __init__(self, key, title, content, size=(1000, 650)):
        super().__init__(None)
        self.setWindowFlags(Qt.Window | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
        self.setWindowTitle(title)
        self.key = key
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(content)
        content.show()
        self.resize(*size)
        geo = theme.load_ui().get('popouts', {}).get(key)
        if geo:
            try:
                self.restoreGeometry(QtCore.QByteArray.fromBase64(geo.encode()))
            except (TypeError, ValueError):
                pass

    def closeEvent(self, e):
        geo = bytes(self.saveGeometry().toBase64()).decode()
        theme.save_ui(popouts=dict(theme.load_ui().get('popouts', {}), **{self.key: geo}))
        super().closeEvent(e)
        self.closed.emit()


def placeholder(text, back):
    """what is left where a part was popped out: a note and a button to bring it back"""
    box = QtWidgets.QFrame()
    box.setObjectName('popped')
    theme.style(box, 'QFrame#popped { background: %(PANEL)s; border: 1px dashed %(BORDER)s; border-radius: 8px; }'
                     ' QFrame#popped QLabel { background: transparent; color: %(MUTED)s; }')
    lay = QtWidgets.QHBoxLayout(box)
    lay.addStretch(1)
    lay.addWidget(QtWidgets.QLabel(text))
    b = QtWidgets.QPushButton('Bring it back here')
    b.clicked.connect(back)
    lay.addWidget(b)
    lay.addStretch(1)
    return box


class ViewDeck(QtWidgets.QWidget):
    """the main view. views: [(key, button text, title, make())] - make() builds the view the first time it is
    needed. shown(key) is called whenever a view becomes visible (here or in its window), to bring it up to date."""
    changed = QtCore.pyqtSignal(str)

    def __init__(self, views, shown=None):
        super().__init__()
        self.views = {k: (text, title, make) for k, text, title, make in views}
        self.order = [k for k, *_ in views]
        self.shown = shown or (lambda key: None)
        self.content = {}           # key -> its widget (once made)
        self.pages = {}             # key -> its page in the stack
        self.windows = {}           # key -> PopWindow while popped out
        self.notes = {}             # key -> the placeholder on its page while popped out
        self.current = None
        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 2)
        self.buttons = {}
        for k in self.order:
            b = QtWidgets.QPushButton(self.views[k][0])
            b.setCheckable(True)
            b.setToolTip('Show the %s here, in the main view' % self.views[k][1].lower())
            theme.style(b, 'QPushButton { padding: 3px 12px; } QPushButton:checked { background: %(SEL_BG)s; '
                           'border: 1px solid %(ACCENT)s; color: %(TEXT)s; font-weight: 600; }')
            b.clicked.connect(lambda _, k=k: self.show_view(k))
            self.buttons[k] = b
            bar.addWidget(b)
        bar.addStretch(1)
        self.pop_btn = QtWidgets.QPushButton(POP + '  Pop out')
        self.pop_btn.setToolTip('Open the view shown here in a window of its own (close that window to bring it '
                                'back)')
        self.pop_btn.clicked.connect(lambda: self.current and self.pop_out(self.current))
        bar.addWidget(self.pop_btn)
        self.stack = QtWidgets.QStackedWidget()
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addLayout(bar)
        lay.addWidget(self.stack, 1)

    def widget(self, key):
        """the view's widget, made (and placed on its page) the first time"""
        if key not in self.content:
            w = self.views[key][2]()
            self.content[key] = w
            page = QtWidgets.QWidget()
            pl = QtWidgets.QVBoxLayout(page)
            pl.setContentsMargins(0, 0, 0, 0)
            pl.addWidget(w)
            self.pages[key] = page
            self.stack.addWidget(page)
        return self.content[key]

    def show_view(self, key):
        """make key the main view"""
        self.widget(key)
        self.current = key
        self.stack.setCurrentWidget(self.pages[key])
        for k, b in self.buttons.items():
            b.setChecked(k == key)
        self.pop_btn.setEnabled(key not in self.windows)
        theme.save_ui(main_view=key)
        self.shown(key)
        self.changed.emit(key)

    def pop_out(self, key):
        """put the view in a window of its own (or bring that window to the front)"""
        if key in self.windows:
            self.windows[key].showNormal()
            self.windows[key].raise_()
            self.windows[key].activateWindow()
            return
        w = self.widget(key)
        page = self.pages[key]
        note = placeholder('The %s is in its own window.' % self.views[key][1].lower(), lambda: self.dock(key))
        page.layout().removeWidget(w)
        page.layout().addWidget(note)
        self.notes[key] = note
        win = PopWindow('view_' + key, self.views[key][1], w)
        win.closed.connect(lambda: self.dock(key, from_window=True))
        self.windows[key] = win
        win.show()
        if key == self.current:
            self.pop_btn.setEnabled(False)
        self.shown(key)

    def dock(self, key, from_window=False):
        """bring a popped-out view back to its place in the main view"""
        win = self.windows.pop(key, None)
        if win is None:
            return
        w = self.content[key]
        page = self.pages[key]
        note = self.notes.pop(key, None)
        if note is not None:
            note.hide()
            note.setParent(None)
            note.deleteLater()
        page.layout().addWidget(w)
        w.show()
        if not from_window:
            win.close()             # (its closed signal finds the view docked already)
        win.deleteLater()
        if key == self.current:
            self.pop_btn.setEnabled(True)
        self.shown(key)

    def is_visible(self, key):
        """is the view on screen (here as the main view, or in its window)?"""
        w = self.content.get(key)
        return w is not None and w.isVisible()

    def close_windows(self):
        for key in list(self.windows):
            self.windows[key].close()
