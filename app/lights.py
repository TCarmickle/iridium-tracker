"""Activity lights: small oval LEDs that flash when a burst comes in.
  green = a message was decoded, red = a damaged burst (heard, but couldn't be decoded)
"""
import time

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

GREEN = QtGui.QColor(80, 255, 120)
RED = QtGui.QColor(255, 50, 50)
OFF = QtGui.QColor(48, 54, 64)
FLASH_S = 0.3       # a flash: full brightness for this long, then straight off (no fade)


def led_colour(on, flashed_at, now):
    """(colour, lit): fully on for FLASH_S after a flash, otherwise off"""
    lit = bool(flashed_at) and now - flashed_at < FLASH_S
    return (on if lit else OFF), (1.0 if lit else 0.0)


def paint_led(p, rect, colour, glow):
    """a plain solid oval with crisp edges: no halo, highlight, outline or smoothing"""
    p.save()
    p.setRenderHint(QtGui.QPainter.Antialiasing, False)
    p.setPen(Qt.NoPen)
    p.setBrush(colour)
    p.drawEllipse(rect)
    p.restore()


class ActivityLights(QtWidgets.QWidget):
    """two ovals, one above the other: decoded (green, top) and damaged (red, bottom)"""

    def __init__(self):
        super().__init__()
        self.setFixedSize(84, 30)
        self.setToolTip('Flashes for every burst the receiver hears:\n'
                        'green (top) = message decoded\nred (bottom) = damaged burst, could not be decoded')
        self.last = {'ok': 0.0, 'bad': 0.0}
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.setTimerType(Qt.PreciseTimer)

    def flash(self, ok):
        self.last['ok' if ok else 'bad'] = time.time()
        if not self.timer.isActive():
            self.timer.start(15)
        self.repaint()      # show it now, not at the next convenient moment

    def _tick(self):
        self.update()
        if time.time() - max(self.last.values()) > FLASH_S + 0.03:
            self.timer.stop()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        now = time.time()
        font = p.font()
        font.setPointSize(7)
        p.setFont(font)
        for i, (key, colour, label) in enumerate((('ok', GREEN, 'decoded'), ('bad', RED, 'damaged'))):
            y = 2 + i * 14
            c, k = led_colour(colour, self.last[key], now)
            paint_led(p, QtCore.QRectF(4, y + 1, 18, 10), c, k)
            p.setPen(QtGui.QColor('#8b93a1'))
            p.drawText(QtCore.QRectF(27, y, 60, 12), Qt.AlignLeft | Qt.AlignVCenter, label)


class SatelliteLights(QtCore.QObject):
    """remembers when each satellite was last heard, for the lights in the satellite list"""
    changed = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.last = {}
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)

    def flash(self, name):
        self.last[name] = time.time()
        if not self.timer.isActive():
            self.timer.start(15)
        self.changed.emit()

    def _tick(self):
        self.changed.emit()
        if not self.last or time.time() - max(self.last.values()) > FLASH_S + 0.03:
            self.timer.stop()


class SatelliteLightDelegate(QtWidgets.QStyledItemDelegate):
    """the satellite-name column, with an oval light at its left that flashes when that satellite is heard"""

    def __init__(self, lights, parent=None):
        super().__init__(parent)
        self.lights = lights

    def paint(self, p, option, index):
        r = option.rect
        led = QtCore.QRectF(r.left() + 6, r.center().y() - 4, 14, 8)
        text_opt = QtWidgets.QStyleOptionViewItem(option)
        text_opt.rect = r.adjusted(24, 0, 0, 0)
        p.save()            # row background (selection / alternate colour) across the whole cell
        style = option.widget.style() if option.widget else QtWidgets.QApplication.style()
        bg = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(bg, index)
        bg.text = ''
        style.drawControl(QtWidgets.QStyle.CE_ItemViewItem, bg, p, option.widget)
        p.restore()
        super().paint(p, text_opt, index)
        c, k = led_colour(GREEN, self.lights.last.get(index.data(QtCore.Qt.UserRole) or index.data()), time.time())
        paint_led(p, led, c, k)

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        return QtCore.QSize(s.width() + 24, s.height())
