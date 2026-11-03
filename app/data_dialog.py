"""The "Data" panel: how much space Satellite Tracker uses, the limits, and clean-up."""
import json
import os

from PyQt5 import QtCore, QtGui, QtWidgets

import paths
import storage


def catalogued():
    """names of the captures already in the catalog"""
    try:
        return set(json.load(open(os.path.join(paths.DATA, 'catalog-data.json'))))
    except (OSError, ValueError):
        return set()


class DataDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, protect=()):
        super().__init__(parent)
        self.protect = set(protect)
        self.setWindowTitle('Data and storage')
        lay = QtWidgets.QVBoxLayout(self)
        self.table = QtWidgets.QLabel()
        self.table.setTextFormat(QtCore.Qt.RichText)
        lay.addWidget(self.table)

        box = QtWidgets.QGroupBox('Limits')
        form = QtWidgets.QFormLayout(box)
        s = storage.settings()
        self.keep_days = QtWidgets.QSpinBox()
        self.keep_days.setRange(0, 3650)
        self.keep_days.setSuffix(' days')
        self.keep_days.setValue(s['keep_raw_days'])
        self.keep_days.setToolTip('0 = delete the raw recording as soon as the capture is in the catalog')
        self.max_raw = QtWidgets.QSpinBox()
        self.max_raw.setRange(0, 1000000)
        self.max_raw.setSingleStep(256)
        self.max_raw.setSuffix(' MB')
        self.max_raw.setValue(s['max_raw_mb'])
        self.max_tiles = QtWidgets.QSpinBox()
        self.max_tiles.setRange(20, 100000)
        self.max_tiles.setSingleStep(50)
        self.max_tiles.setSuffix(' MB')
        self.max_tiles.setValue(s['max_tiles_mb'])
        self.min_free = QtWidgets.QDoubleSpinBox()
        self.min_free.setRange(0.2, 1000)
        self.min_free.setDecimals(1)
        self.min_free.setSuffix(' GB')
        self.min_free.setValue(s['min_free_gb'])
        form.addRow('Keep raw recordings for:', self.keep_days)
        form.addRow('...but no more than:', self.max_raw)
        form.addRow('Map picture cache up to:', self.max_tiles)
        form.addRow('Stop receiving below:', self.min_free)
        note = QtWidgets.QLabel(
            '<span style="color:#8b93a1">Raw recordings hold everything the receiver heard - for Iridium every '
            'burst, including other people\'s voice and message bursts (never decoded). Once a capture is in the '
            'catalog only a small file of what the tracker uses (positions, Doppler, orbits) is needed - it is '
            'always kept, so your catalog can be rebuilt. The catalog itself is never deleted.</span>')
        note.setWordWrap(True)
        form.addRow(note)
        lay.addWidget(box)

        self.result_lbl = QtWidgets.QLabel()
        lay.addWidget(self.result_lbl)
        row = QtWidgets.QHBoxLayout()
        clean = QtWidgets.QPushButton('Save and clean up now')
        clean.clicked.connect(self.clean)
        folder = QtWidgets.QPushButton('Open data folder')
        folder.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(paths.DATA)))
        close = QtWidgets.QPushButton('Close')
        close.clicked.connect(self.accept)
        row.addWidget(clean)
        row.addWidget(folder)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        self.show_usage()
        self.resize(560, 0)

    def show_usage(self):
        u = storage.usage()
        f = storage.fmt_bytes
        rows = [('Raw recordings', '%s (%d captures)' % (f(u['raw']), u['raw_captures'])),
                ('Kept lines (positions, Doppler, orbits)', f(u['reduced'])),
                ('Catalog and settings', f(u['catalog'])),
                ('Map picture cache', f(u['tiles'])),
                ('<b>Total</b>', '<b>%s</b>' % f(u['total'])),
                ('Free on this disk', f(u['free']))]
        self.table.setText('<b>%s</b><table cellpadding="3">%s</table>' % (
            paths.DATA, ''.join('<tr><td>%s</td><td align="right">%s</td></tr>' % r for r in rows)))

    def clean(self):
        s = {'keep_raw_days': self.keep_days.value(), 'max_raw_mb': self.max_raw.value(),
             'max_tiles_mb': self.max_tiles.value(), 'min_free_gb': self.min_free.value()}
        storage.save_settings(s)
        n, freed = storage.enforce(catalogued(), protect=self.protect, s=s)
        t = storage.prune_tiles(s['max_tiles_mb'])
        self.result_lbl.setText('Saved. Deleted %d raw capture files (%s)%s.' % (
            n, storage.fmt_bytes(freed), ' and %d map pictures' % t if t else ''))
        self.show_usage()
