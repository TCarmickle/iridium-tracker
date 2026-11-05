"""The Images section: weather pictures from Meteor-M passes (made by SatDump, see images.py) - a list of passes,
the pictures of the chosen one, and a viewer to zoom, pan and save them. Receiving a pass uses the tracker's own
receiver on the Meteor band; the pictures appear here a minute or two after Stop.
"""
import datetime
import os
import shutil
import subprocess
import sys

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

import images
import paths
import systems
import theme

THUMB = 150


class Viewer(QtWidgets.QGraphicsView):
    """one picture: wheel to zoom, drag to pan, double-click to fit"""

    def __init__(self):
        super().__init__()
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.item = None
        self.path = None
        self.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self.setRenderHints(QtGui.QPainter.SmoothPixmapTransform)
        self.setBackgroundBrush(QtGui.QColor('#0e1014'))
        self.fit_mode = True
        self.empty = QtWidgets.QLabel('Pick a pass on the left, then a picture', self)
        self.empty.setStyleSheet('color: #8b93a1; background: transparent;')

    def show_path(self, path):
        self.scene().clear()
        self.item, self.path = None, path
        self.empty.setVisible(not path)
        if not path:
            return
        pm = QtGui.QPixmap(path)
        if pm.isNull():
            self.empty.setText('Could not open %s' % os.path.basename(path))
            self.empty.show()
            return
        self.item = self.scene().addPixmap(pm)
        self.scene().setSceneRect(QtCore.QRectF(pm.rect()))
        self.fit()

    def fit(self):
        self.fit_mode = True
        if self.item:
            self.fitInView(self.item, Qt.KeepAspectRatio)

    def actual(self):
        self.fit_mode = False
        self.resetTransform()

    def wheelEvent(self, e):
        if not self.item:
            return
        self.fit_mode = False
        f = 1.25 if e.angleDelta().y() > 0 else 0.8
        self.scale(f, f)

    def mouseDoubleClickEvent(self, e):
        self.fit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.empty.adjustSize()
        self.empty.move((self.width() - self.empty.width()) // 2, (self.height() - self.empty.height()) // 2)
        if self.fit_mode:
            self.fit()


def _local(iso):
    try:
        t = datetime.datetime.fromisoformat(iso).astimezone()
        return t.strftime('%a %b %d, %I:%M %p').replace(' 0', ' ')
    except (TypeError, ValueError):
        return iso or '?'


class ImagesView(QtWidgets.QWidget):
    def __init__(self, tracker):
        super().__init__()
        self.tracker = tracker
        self.sig = None
        self.proc = None                # a "make the pictures again" run

        # ---- top: what this is, receiving, SatDump
        intro = QtWidgets.QLabel()
        intro.setWordWrap(True)
        theme.style(intro, 'color: %(MUTED)s;')
        intro.setText('Weather pictures from the <b>Meteor-M</b> satellites (137.9 MHz). During a pass, press '
                      '<b>Receive a Meteor pass</b> (or choose <i>Meteor-M weather pictures</i> as the band and '
                      'Start receiver); press Stop when it has set, and SatDump makes the pictures - they appear '
                      'here a minute or two later. Antenna: %s.' % systems.METEOR['antenna'])
        self.rx_btn = QtWidgets.QPushButton()
        self.rx_btn.clicked.connect(self._receive)
        self.import_btn = QtWidgets.QPushButton('Import a SatDump folder...')
        self.import_btn.setToolTip('Copy in the pictures of a pass you received with SatDump itself\n'
                                   '(its output folder, e.g. Documents\\live_output\\<date>_meteor...)')
        self.import_btn.clicked.connect(self._import)
        self.folder_btn = QtWidgets.QPushButton('Open the images folder')
        self.folder_btn.clicked.connect(lambda: self._open(images.IMAGES))
        self.satdump_lbl = QtWidgets.QLabel()
        self.satdump_lbl.setOpenExternalLinks(True)
        self.satdump_lbl.setWordWrap(True)
        top_btns = QtWidgets.QHBoxLayout()
        top_btns.addWidget(self.rx_btn)
        top_btns.addWidget(self.import_btn)
        top_btns.addWidget(self.folder_btn)
        top_btns.addStretch(1)

        # ---- left: passes
        self.passes = QtWidgets.QListWidget()
        self.passes.setMinimumWidth(270)
        self.passes.currentRowChanged.connect(self._pass_chosen)
        self.pass_note = QtWidgets.QLabel()
        self.pass_note.setWordWrap(True)
        theme.style(self.pass_note, 'color: %(MUTED)s;')
        self.redo_btn = QtWidgets.QPushButton('Make pictures again')
        self.redo_btn.setToolTip('Run SatDump on this pass\'s recording again (kept when it gave no pictures)')
        self.redo_btn.clicked.connect(self._redo)
        self.delete_btn = QtWidgets.QPushButton('Delete pass...')
        self.delete_btn.clicked.connect(self._delete)
        pass_btns = QtWidgets.QHBoxLayout()
        pass_btns.addWidget(self.redo_btn)
        pass_btns.addWidget(self.delete_btn)
        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(self._heading('PASSES'))
        lv.addWidget(self.passes, 1)
        lv.addWidget(self.pass_note)
        lv.addLayout(pass_btns)

        # ---- middle: the pass's pictures; right: the viewer
        self.thumbs = QtWidgets.QListWidget()
        self.thumbs.setViewMode(QtWidgets.QListView.IconMode)
        self.thumbs.setIconSize(QtCore.QSize(THUMB, THUMB))
        self.thumbs.setGridSize(QtCore.QSize(THUMB + 24, THUMB + 44))
        self.thumbs.setResizeMode(QtWidgets.QListView.Adjust)
        self.thumbs.setMovement(QtWidgets.QListView.Static)
        self.thumbs.setWordWrap(True)
        self.thumbs.setMinimumWidth(THUMB + 50)
        self.thumbs.currentItemChanged.connect(lambda it, _: self.viewer.show_path(it.data(Qt.UserRole) if it else None)
                                               or self._viewer_buttons())
        mid = QtWidgets.QWidget()
        mv = QtWidgets.QVBoxLayout(mid)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.addWidget(self._heading('PICTURES'))
        mv.addWidget(self.thumbs, 1)

        self.viewer = Viewer()
        self.title = QtWidgets.QLabel()
        theme.style(self.title, 'font-weight: 600; color: %(TEXT)s;')
        self.fit_btn = QtWidgets.QPushButton('Fit')
        self.fit_btn.clicked.connect(self.viewer.fit)
        self.one_btn = QtWidgets.QPushButton('100%')
        self.one_btn.clicked.connect(self.viewer.actual)
        self.save_btn = QtWidgets.QPushButton('Save as...')
        self.save_btn.clicked.connect(self._save)
        self.open_btn = QtWidgets.QPushButton('Open')
        self.open_btn.setToolTip('Open the picture in your usual picture viewer')
        self.open_btn.clicked.connect(lambda: self.viewer.path and self._open(self.viewer.path))
        self.show_btn = QtWidgets.QPushButton('Show in folder')
        self.show_btn.clicked.connect(self._show_in_folder)
        vb = QtWidgets.QHBoxLayout()
        vb.addWidget(self.title, 1)
        for b in (self.fit_btn, self.one_btn, self.save_btn, self.open_btn, self.show_btn):
            vb.addWidget(b)
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addLayout(vb)
        rv.addWidget(self.viewer, 1)
        hint = QtWidgets.QLabel('Scroll to zoom, drag to move, double-click to fit')
        theme.style(hint, 'color: %(MUTED)s;')
        rv.addWidget(hint)

        split = QtWidgets.QSplitter()
        split.addWidget(left)
        split.addWidget(mid)
        split.addWidget(right)
        split.setStretchFactor(2, 1)
        self.split = split
        self.sized = False              # (sizes are set the first time the section is shown: its width is known)

        # under the pictures: the tracker's live spectrum, waterfall and sound (the same panel, moved here while
        # this section is shown - see Tracker.show_section)
        self.vsplit = QtWidgets.QSplitter(Qt.Vertical)
        self.vsplit.addWidget(split)
        self.vsplit.setStretchFactor(0, 3)
        self.vsave = QtCore.QTimer(self)        # the pictures / spectrum divider: saved a moment after a drag
        self.vsave.setSingleShot(True)
        self.vsave.timeout.connect(lambda: self.vsplit.count() == 2 and
                                   theme.save_splitter(self.vsplit, 'images_spectrum'))
        self.vsplit.splitterMoved.connect(lambda *_: self.vsave.start(400))

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(intro)
        lay.addLayout(top_btns)
        lay.addWidget(self.satdump_lbl)
        lay.addWidget(self.vsplit, 1)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(3000)
        tracker.receiver.running.connect(lambda _: self._rx_text())
        tracker.receiver.decoded.connect(self.refresh)
        self.refresh()
        self._rx_text()
        self._viewer_buttons()

    def take_spectrum(self, panel):
        """show the tracker's spectrum / waterfall panel under the pictures"""
        self.vsplit.insertWidget(1, panel)
        self.vsplit.setStretchFactor(1, 2)
        h = max(500, self.vsplit.height())
        theme.restore_splitter(self.vsplit, 'images_spectrum', [h - 300, 300])    # (where you last dragged it)

    def showEvent(self, e):
        super().showEvent(e)
        if not self.sized:
            self.sized = True
            w = max(900, self.width())
            theme.remember_splitter(self.split, 'images_panels', [290, 2 * THUMB + 70, w - 290 - 2 * THUMB - 70])

    @staticmethod
    def _heading(text):
        lbl = QtWidgets.QLabel(text)
        theme.style(lbl, 'color: %(MUTED)s; font-weight: 600;')
        return lbl

    # ---- receiving
    def _rx_text(self):
        r = self.tracker.receiver
        meteor = getattr(r, 'system', None) is systems.METEOR
        if r.state == 'running' and meteor:
            self.rx_btn.setText('■  Stop and make the pictures')
        elif r.state == 'running':
            self.rx_btn.setText('Receiving %s - stop it first' % r.system['label'])
        elif r.busy:
            self.rx_btn.setText('Making the pictures...' if meteor else 'Receiver finishing...')
        else:
            self.rx_btn.setText('▶  Receive a Meteor pass')
        self.rx_btn.setEnabled(not r.busy or (r.state == 'running' and meteor))
        sd = images.find_satdump()
        if sd:
            self.satdump_lbl.setText('<span style="color:%s">Pictures are made with SatDump (GPL-3.0, '
                                     'satdump.org): %s</span>' % (theme.PAL['MUTED'], sd))
        else:
            self.satdump_lbl.setText('<span style="color:%s">SatDump is not installed - it makes the pictures. Get it '
                                     'free from <a href="https://www.satdump.org">satdump.org</a> (Windows installer); '
                                     'passes received before that keep their recording until then.</span>'
                                     % theme.PAL['WARN'])

    def _receive(self):
        tr, r = self.tracker, self.tracker.receiver
        if r.state == 'running':
            if r.system is systems.METEOR:
                r.stop()
            return
        i = tr.rx_system.findData(systems.METEOR['key'])
        if tr.rx_system.currentIndex() != i:
            tr.rx_system.setCurrentIndex(i)         # (saved, like choosing it in the box above)
        tr.toggle_receiver()
        self._rx_text()

    # ---- passes
    def refresh(self):
        self._rx_text()
        found = images.passes()
        sig = [(p['folder'], p.get('images'), p.get('note'), os.path.getmtime(os.path.join(p['folder'], 'pass.json'))
                if os.path.exists(os.path.join(p['folder'], 'pass.json')) else 0) for p in found]
        if sig == self.sig:
            return
        self.sig = sig
        cur = self.passes.currentItem().data(Qt.UserRole)['folder'] if self.passes.currentItem() else None
        self.passes.blockSignals(True)
        self.passes.clear()
        for p in found:
            n = p.get('images') or 0
            sat = p.get('satellite') or ('Meteor-M' if p.get('source') == 'received' else p.get('name', ''))
            it = QtWidgets.QListWidgetItem('%s\n%s  -  %s%s' % (
                _local(p.get('start_utc')), sat, '%d pictures' % n if n else 'no pictures',
                '  (imported)' if p.get('source') == 'imported' else ''))
            it.setData(Qt.UserRole, p)
            it.setToolTip(p.get('note') or '')
            if not n:
                it.setForeground(QtGui.QColor(theme.PAL['MUTED']))
            self.passes.addItem(it)
        self.passes.blockSignals(False)
        if not found:
            self.passes.addItem(QtWidgets.QListWidgetItem('No passes yet'))
            self.passes.item(0).setFlags(Qt.NoItemFlags)
            self._pass_chosen(-1)
            return
        rows = [i for i, p in enumerate(found) if p['folder'] == cur]
        self.passes.setCurrentRow(rows[0] if rows else 0)
        self._pass_chosen(self.passes.currentRow())

    def _current(self):
        it = self.passes.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _pass_chosen(self, _row):
        p = self._current()
        self.thumbs.clear()
        self.viewer.show_path(None)
        self.redo_btn.setEnabled(bool(p and p.get('source') == 'received' and os.path.exists(
            os.path.join(paths.CAPTURES, p.get('capture', '') + images.BASEBAND_EXT)) and not self.proc))
        self.delete_btn.setEnabled(bool(p))
        if not p:
            self.pass_note.setText('')
            self._viewer_buttons()
            return
        self.pass_note.setText(p.get('note') or '')
        for path in images.pictures(p['folder']):
            reader = QtGui.QImageReader(path)
            size = reader.size()
            if size.isValid():
                reader.setScaledSize(size.scaled(THUMB, THUMB, Qt.KeepAspectRatio))
            img = reader.read()
            it = QtWidgets.QListWidgetItem(QtGui.QIcon(QtGui.QPixmap.fromImage(img)), images.nice_name(path))
            it.setData(Qt.UserRole, path)
            it.setToolTip('%s\n%d x %d' % (os.path.relpath(path, p['folder']), size.width(), size.height()))
            self.thumbs.addItem(it)
        if self.thumbs.count():
            self.thumbs.setCurrentRow(0)
        self._viewer_buttons()

    def _viewer_buttons(self):
        on = bool(self.viewer.path)
        for b in (self.fit_btn, self.one_btn, self.save_btn, self.open_btn, self.show_btn):
            b.setEnabled(on)
        self.title.setText(images.nice_name(self.viewer.path) if on else '')

    # ---- actions
    def _import(self):
        start = os.path.join(os.path.expanduser('~'), 'OneDrive', 'Documents', 'live_output')
        if not os.path.isdir(start):
            start = os.path.join(os.path.expanduser('~'), 'Documents')
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, 'A SatDump output folder (one pass)', start)
        if not folder:
            return
        try:
            meta = images.import_folder(folder)
        except (ValueError, OSError) as e:
            QtWidgets.QMessageBox.warning(self, 'Import', str(e))
            return
        self.sig = None
        self.refresh()
        for i in range(self.passes.count()):
            if (self.passes.item(i).data(Qt.UserRole) or {}).get('name') == meta['name']:
                self.passes.setCurrentRow(i)

    def _redo(self):
        p = self._current()
        if not p or self.proc:
            return
        self.proc = QtCore.QProcess(self)
        self.proc.setProcessEnvironment(self.tracker.receiver.env)
        self.proc.finished.connect(self._redo_done)
        self.pass_note.setText('Making the pictures with SatDump...')
        self.redo_btn.setEnabled(False)
        self.proc.start(sys.executable.replace('pythonw.exe', 'python.exe'),
                        [os.path.join(os.path.dirname(os.path.abspath(__file__)), 'images.py'), '--decode', p['capture']])

    def _redo_done(self, *_):
        self.proc = None
        self.sig = None
        self.refresh()

    def _delete(self):
        p = self._current()
        if not p:
            return
        if QtWidgets.QMessageBox.question(
                self, 'Delete pass', 'Delete the pass of %s and its %d pictures from the images folder?\n'
                '(This can\'t be undone. Pictures you saved elsewhere are not touched.)'
                % (_local(p.get('start_utc')), p.get('images') or 0)) != QtWidgets.QMessageBox.Yes:
            return
        shutil.rmtree(p['folder'], ignore_errors=True)
        if p.get('capture'):
            try:
                os.remove(os.path.join(paths.CAPTURES, p['capture'] + images.BASEBAND_EXT))
            except OSError:
                pass
        self.sig = None
        self.refresh()

    def _save(self):
        src = self.viewer.path
        if not src:
            return
        p = self._current() or {}
        stamp = ''
        try:
            stamp = datetime.datetime.fromisoformat(p.get('start_utc')).astimezone().strftime('%Y-%m-%d_%H%M_')
        except (TypeError, ValueError):
            pass
        name = stamp + os.path.basename(src)
        dst, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, 'Save the picture', os.path.join(os.path.expanduser('~'), 'Pictures', name),
            'Pictures (*%s)' % os.path.splitext(src)[1])
        if dst:
            shutil.copyfile(src, dst)

    def _show_in_folder(self):
        if self.viewer.path:
            subprocess.Popen(['explorer', '/select,', os.path.normpath(self.viewer.path)])

    @staticmethod
    def _open(path):
        os.makedirs(path, exist_ok=True) if path == images.IMAGES else None
        try:
            os.startfile(path)
        except OSError:
            pass
