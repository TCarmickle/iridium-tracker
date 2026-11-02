"""Activity lights: small oval LEDs that flash when a burst comes in.
  green = a message was decoded, red = a damaged burst (heard, but couldn't be decoded)
"""
import time

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

GREEN = QtGui.QColor(60, 230, 110)
RED = QtGui.QColor(255, 70, 70)
OFF = QtGui.QColor(48, 54, 64)
FLASH_S = 0.25      # how long a flash takes to fade out


def led_colour(on, flashed_at, now):
    """the LED's colour: full brightness right after a flash, fading back to dark"""
    k = max(0.0, 1.0 - (now - flashed_at) / FLASH_S) if flashed_at else 0.0
    return QtGui.QColor(int(OFF.red() + (on.red() - OFF.red()) * k), int(OFF.green() + (on.green() - OFF.green()) * k),
                        int(OFF.blue() + (on.blue() - OFF.blue()) * k)), k


def paint_led(p, rect, colour, glow):
    p.save()
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    if glow > 0:            # soft halo while lit
        halo = QtGui.QColor(colour)
        halo.setAlpha(int(110 * glow))
        p.setPen(Qt.NoPen)
        p.setBrush(halo)
        p.drawEllipse(rect.adjusted(-3, -2, 3, 2))
    p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 160), 1))
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

    def flash(self, ok):
        self.last['ok' if ok else 'bad'] = time.time()
        if not self.timer.isActive():
            self.timer.start(30)
        self.update()

    def _tick(self):
        self.update()
        if time.time() - max(self.last.values()) > FLASH_S:
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
            self.timer.start(30)
        self.changed.emit()

    def _tick(self):
        self.changed.emit()
        if not self.last or time.time() - max(self.last.values()) > FLASH_S:
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
        c, k = led_colour(GREEN, self.lights.last.get(index.data()), time.time())
        paint_led(p, led, c, k)

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        return QtCore.QSize(s.width() + 24, s.height())
