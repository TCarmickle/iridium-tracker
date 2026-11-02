"""Where is the antenna? Typed in, pasted from a map, or detected by Windows; height looked up online."""
import math
import subprocess
import time

from PyQt5 import QtCore, QtWidgets

# Windows Location (the same service the Maps and Weather apps use); prints "lat lon accuracy_m" or an error word
DETECT_PS = r'''
Add-Type -AssemblyName System.Device
$w = New-Object System.Device.Location.GeoCoordinateWatcher([System.Device.Location.GeoPositionAccuracy]::High)
$w.Start()
$t = 0
while (($w.Status -ne 'Ready') -and ($w.Permission -ne 'Denied') -and ($t -lt 150)) { Start-Sleep -Milliseconds 100; $t++ }
$c = $w.Position.Location
if ($w.Permission -eq 'Denied') { 'DENIED' }
elseif ($c.IsUnknown) { 'UNKNOWN' }
else { '{0} {1} {2}' -f $c.Latitude.ToString([cultureinfo]::InvariantCulture), $c.Longitude.ToString([cultureinfo]::InvariantCulture), [math]::Round($c.HorizontalAccuracy) }
$w.Stop()
'''
ELEVATION_URL = 'https://api.open-meteo.com/v1/elevation?latitude=%.5f&longitude=%.5f'


def ground_height_m(lat, lon):
    """ground height above sea level (m) from Open-Meteo's terrain model (about 90 m grid)"""
    import requests
    for attempt in range(3):        # one hiccup on a first connection shouldn't lose the height
        try:
            r = requests.get(ELEVATION_URL % (lat, lon), timeout=15, headers={'User-Agent': 'IridiumTracker/1.0'})
            r.raise_for_status()
            return float(r.json()['elevation'][0])
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.5)


def distance_km(a, b):
    la1, lo1, la2, lo2 = (math.radians(v) for v in (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(min(1.0, h)))


class _Job(QtCore.QThread):
    """runs fn() off the GUI thread; done(result or Exception)"""
    done = QtCore.pyqtSignal(object)

    def __init__(self, fn, parent):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as e:      # reported in the dialog
            self.done.emit(e)


def detect_windows():
    """(lat, lon, accuracy m) from Windows Location"""
    r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', DETECT_PS],
                       capture_output=True, text=True, timeout=40, creationflags=0x08000000)
    out = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ''
    if out == 'DENIED':
        raise RuntimeError('Windows Location is turned off for desktop apps. Turn it on in Settings > Privacy & '
                           'security > Location ("Let desktop apps access your location"), or type your location in.')
    parts = out.split()
    if len(parts) != 3:
        raise RuntimeError('Windows could not tell where this PC is. Type your location in instead.')
    return float(parts[0]), float(parts[1]), float(parts[2])


class LocationDialog(QtWidgets.QDialog):
    def __init__(self, home=None, first_run=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Antenna location')
        self.result_home = None
        self.jobs = []
        lay = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            ('<b>Where is your antenna?</b><br><br>' if first_run else '<b>Antenna location</b><br><br>') +
            'The tracker uses it to work out which satellites are above you and to fit their orbits from '
            'their Doppler shift, so the closer the better (within ~100 m is plenty).<br>'
            'Exact spot: right-click your house in Google Maps and click the numbers at the top of the menu '
            'to copy them, then paste here.')
        intro.setWordWrap(True)
        lay.addWidget(intro)

        form = QtWidgets.QFormLayout()
        self.latlon = QtWidgets.QLineEdit()
        self.latlon.setPlaceholderText('e.g. 51.47791, -0.00148   (pasting from Google Maps works)')
        self.alt = QtWidgets.QDoubleSpinBox()
        self.alt.setRange(-400, 9000)
        self.alt.setDecimals(0)
        self.alt.setSuffix(' m above sea level')
        form.addRow('Latitude, longitude:', self.latlon)
        form.addRow('Height:', self.alt)
        lay.addLayout(form)

        row = QtWidgets.QHBoxLayout()
        self.detect_btn = QtWidgets.QPushButton('Detect my location')
        self.detect_btn.setToolTip('Asks Windows Location (Wi-Fi / network based on most PCs), then looks up the height.')
        self.detect_btn.clicked.connect(self.detect)
        self.height_btn = QtWidgets.QPushButton('Look up height')
        self.height_btn.setToolTip('Ground height for the coordinates above, from Open-Meteo\'s terrain model.')
        self.height_btn.clicked.connect(self.lookup_height)
        row.addWidget(self.detect_btn)
        row.addWidget(self.height_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.info = QtWidgets.QLabel()
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        note = QtWidgets.QLabel('<span style="color:#8b93a1">"Look up height" sends the coordinates to '
                                'open-meteo.com. Your location is saved only in settings.json in the app folder.</span>')
        note.setWordWrap(True)
        lay.addWidget(note)
        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self.save)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        if home:
            self.latlon.setText('%.5f, %.5f' % (home[0], home[1]))
            self.alt.setValue(home[2] * 1000)
        self.resize(560, 0)

    # ---- helpers
    def _say(self, text, error=False):
        self.info.setText('<span style="color:%s">%s</span>' % ('#ff6b6b' if error else '#4cd07d', text))

    def _coords(self):
        try:
            lat, lon = (float(v) for v in self.latlon.text().replace(';', ',').split(','))
        except ValueError:
            return None
        return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None

    def _run(self, fn, then):
        job = _Job(fn, self)
        job.done.connect(then)
        job.finished.connect(lambda: self.jobs.remove(job))
        self.jobs.append(job)
        self.detect_btn.setEnabled(False)
        self.height_btn.setEnabled(False)
        job.start()

    def _idle(self):
        self.detect_btn.setEnabled(True)
        self.height_btn.setEnabled(True)

    # ---- detect
    def detect(self):
        self._say('Asking Windows where this PC is...')
        self._run(detect_windows, self._detected)

    def _detected(self, res):
        self._idle()
        if isinstance(res, Exception):
            self._say(str(res), error=True)
            return
        lat, lon, acc = res
        self.latlon.setText('%.5f, %.5f' % (lat, lon))
        self.accuracy = acc
        rough = acc > 300
        self._say('Windows says %.5f, %.5f (within about %s).%s' % (
            lat, lon, '%.0f km' % (acc / 1000) if acc >= 1000 else '%.0f m' % acc,
            ' That is only roughly where this PC is - for the best orbits, paste your exact spot from a map.'
            if rough else ''))
        self.lookup_height(keep_message=True)

    # ---- height
    def lookup_height(self, keep_message=False):
        c = self._coords()
        if not c:
            self._say('Enter latitude, longitude first.', error=True)
            return
        self.keep = keep_message and self.info.text()
        self._run(lambda: ground_height_m(*c), self._height)

    def _height(self, res):
        self._idle()
        prefix = (self.keep + '<br>') if self.keep else ''
        if isinstance(res, Exception):
            self.info.setText(prefix + '<span style="color:#ff6b6b">Could not look up the height (offline?) - '
                              'type it in.</span>')
            return
        self.alt.setValue(round(res))
        self.info.setText(prefix + '<span style="color:#4cd07d">Ground height there: %.0f m.</span>' % res)

    # ---- save
    def save(self):
        c = self._coords()
        if not c:
            self._say('Enter latitude and longitude in degrees, separated by a comma (south and west are negative).',
                      error=True)
            return
        self.result_home = (c[0], c[1], self.alt.value() / 1000)
        self.accept()


def ask_location(home=None, first_run=False, parent=None):
    """shows the dialog; returns (lat, lon, alt km) or None"""
    d = LocationDialog(home, first_run, parent)
    return d.result_home if d.exec_() else None
