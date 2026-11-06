"""The Settings window (the cog in the header): your antenna location, data and storage, and the PC clock
(set from GPS time).

Each tab is one of the existing panels (location_dialog.LocationDialog, data_dialog.DataDialog) shown inside
the window instead of on its own.
"""
import ctypes
import time

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

        # ---- PC clock
        self.clock = ClockTab(tracker)
        self.tabs.addTab(self.clock, '\U0001F552  PC clock')

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


# ---------------------------------------------------------------- PC clock from GPS time
FRESH_S = 60            # a measurement older than this isn't used
MAX_SPREAD_S = 0.05     # the fixes behind it must agree this well


def clock_text(offset):
    """'0.412 s behind GPS time' / 'right on GPS time'"""
    a = abs(offset)
    if a < 0.0015:
        return 'right on GPS time (within a millisecond or so)'
    amount = '%.0f ms' % (a * 1000) if a < 1 else '%.3f s' % a if a < 120 else '%.1f minutes' % (a / 60)
    return '%s %s GPS time' % (amount, 'behind' if offset > 0 else 'ahead of')


def set_pc_clock(offset):
    """move the PC clock by offset seconds (GPS minus PC): an elevated PowerShell runs Set-Date -Adjust, so the
    time it takes to say yes to Windows' prompt doesn't matter. True if it was started (the user may still say no)."""
    ticks = int(round(offset * 1e7))
    cmd = '-NoProfile -WindowStyle Hidden -Command "Set-Date -Adjust ([TimeSpan]::FromTicks(%d)) | Out-Null"' % ticks
    r = ctypes.windll.shell32.ShellExecuteW(None, 'runas', 'powershell.exe', cmd, None, 0)
    return r > 32


class ClockTab(QtWidgets.QWidget):
    """how far the PC clock is from GPS time (measured by the GNSS receiver), and a button to set it"""

    def __init__(self, tracker):
        super().__init__()
        self.tracker = tracker
        v = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            'While the GNSS receiver tracks GPS / Galileo satellites and has a position fix, each fix also gives the '
            'exact time. The receiver compares it with this PC\'s clock at the moment the samples were taken.')
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.state = QtWidgets.QLabel()
        self.state.setWordWrap(True)
        self.state.setStyleSheet('font-size: 15px; padding: 10px 0;')
        v.addWidget(self.state)
        self.detail = QtWidgets.QLabel()
        self.detail.setWordWrap(True)
        v.addWidget(self.detail)
        row = QtWidgets.QHBoxLayout()
        self.set_btn = QtWidgets.QPushButton('\U0001F552  Set PC clock to GPS time')
        self.set_btn.setToolTip('Moves the Windows clock by the measured difference.\n'
                                'Windows asks for permission first (changing the clock needs administrator rights).')
        self.set_btn.clicked.connect(self._set)
        row.addWidget(self.set_btn)
        row.addStretch(1)
        v.addLayout(row)
        self.result = QtWidgets.QLabel()
        self.result.setWordWrap(True)
        v.addWidget(self.result)
        note = QtWidgets.QLabel(
            'Accuracy: a few milliseconds (limited by the SDR\'s USB timing). Windows may also set the clock itself '
            'from the internet (Settings > Time & language) - that is fine; this is for when it can\'t, or to check it.')
        note.setWordWrap(True)
        note.setStyleSheet('color: #8b93a7;')
        v.addStretch(1)
        v.addWidget(note)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._update)
        self.timer.start(1000)
        self._update()

    def measurement(self):
        rx = getattr(self.tracker, 'receiver', None)
        m = getattr(rx, 'gps_clock', None)
        if not m:
            return None, None
        age = time.time() - m['t']
        ok = age < FRESH_S and m['nsat'] >= 4 and m['spread'] <= MAX_SPREAD_S
        return m, ok

    def _update(self):
        m, ok = self.measurement()
        if m is None:
            self.state.setText('No GPS time yet')
            self.detail.setText('Start the receiver on <b>GNSS L1 (GPS)</b>; once it has a position fix (usually '
                                'within a few minutes) the clock difference shows here.')
        else:
            age = time.time() - m['t']
            self.state.setText('Your PC clock is <b>%s</b>' % clock_text(m['offset']))
            self.detail.setText('Measured %.0f s ago from a %d-satellite fix; the last fixes agreed to within %.0f ms.%s'
                                % (age, m['nsat'], m['spread'] * 1000,
                                   '' if ok else '<br><span style="color:#e0a040">Too old or too uncertain to set the '
                                   'clock from - keep the GNSS receiver running.</span>'))
        self.set_btn.setEnabled(bool(ok))

    def _set(self):
        m, ok = self.measurement()
        if not ok:
            return
        if set_pc_clock(m['offset']):
            self.result.setText('Asked Windows to move the clock by %+.3f s. Within about 10 s the next measurement '
                                'here should read close to zero.' % m['offset'])
        else:
            self.result.setText('The clock was not changed (Windows\' permission prompt was declined or failed).')
