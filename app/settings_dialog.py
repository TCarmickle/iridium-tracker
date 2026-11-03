"""The Settings window (the cog in the header): your antenna location, and data and storage.

Each tab is one of the existing panels (location_dialog.LocationDialog, data_dialog.DataDialog) shown inside
the window instead of on its own.
"""
from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import Qt

import data_dialog
import location_dialog


class _LocationTab(location_dialog.LocationDialog):
    """the location panel as a tab: Save applies it straight away (the window stays open)"""
    saved = QtCore.pyqtSignal(tuple)

    def accept(self):                   # (LocationDialog.save() ends with accept())
        if self.result_home:
            self.saved.emit(self.result_home)

    def reject(self):
        self.window().reject()


class _StorageTab(data_dialog.DataDialog):
    """the data and storage panel as a tab"""

    def accept(self):                   # (its Close button)
        self.window().accept()


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, tracker, home, protect=(), page=0):
        super().__init__(tracker)
        self.tracker = tracker
        self.setWindowTitle('Settings')
        self.tabs = QtWidgets.QTabWidget()

        # ---- location
        loc_page = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(loc_page)
        self.loc = _LocationTab(home, first_run=home is None, parent=loc_page)
        self.loc.setWindowFlags(Qt.Widget)
        self.loc.saved.connect(self._location_saved)
        # Cancel closes the window; the Save button applies the location
        for b in self.loc.findChildren(QtWidgets.QDialogButtonBox):
            b.button(QtWidgets.QDialogButtonBox.Save).setText('Save location')
        lv.addWidget(self.loc)
        more = QtWidgets.QHBoxLayout()
        pick = QtWidgets.QPushButton('\U0001F4CD  Pick it on the map')
        pick.setToolTip('Close this window and click your antenna’s spot on the map')
        pick.clicked.connect(self._pick)
        more.addWidget(pick)
        fix = tracker.gnss_location()
        if fix:
            gps = QtWidgets.QPushButton('\U0001F6F0  Use GPS-tracked position')
            gps.setToolTip('%.5f, %.5f, %.0f m - the middle of your %d most recent GNSS position fixes\n'
                           '(worked out from the GPS / Galileo satellites your antenna tracked)'
                           % (fix[0], fix[1], fix[2] * 1000, fix[3]))
            gps.clicked.connect(lambda: self._fill(fix))
            more.addWidget(gps)
        more.addStretch(1)
        lv.addLayout(more)
        lv.addStretch(1)
        self.tabs.addTab(loc_page, '\U0001F4CD  Location')

        # ---- data and storage
        self.storage = _StorageTab(self, protect)
        self.storage.setWindowFlags(Qt.Widget)
        self.tabs.addTab(self.storage, '\U0001F4BE  Data and storage')

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(self.tabs)
        self.tabs.setCurrentIndex(page)
        self.resize(620, 520)

    def _fill(self, fix):
        """put the GPS position in the boxes (Save location applies it)"""
        self.loc.latlon.setText('%.5f, %.5f' % (fix[0], fix[1]))
        self.loc.alt.setValue(round(fix[2] * 1000))
        self.loc._say('From your %d most recent GNSS position fixes - press Save location to use it.' % fix[3])

    def _location_saved(self, home):
        self.tracker.apply_location(home)
        self.loc._say('Saved: %.5f, %.5f, %.0f m.' % (home[0], home[1], home[2] * 1000))

    def _pick(self):
        self.accept()
        self.tracker.start_pick()
