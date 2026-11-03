"""Draw the app icon: app\\icon.png (256 px) and app\\icon.ico (16-256 px) for the window and taskbar.

Usage:  python build\\make_icon.py
A dark tile with the Earth, a tilted orbit around it and a satellite on the orbit sending signal arcs
down to the Earth - drawn with Qt, so it is crisp at every size.
"""
import io
import math
import os
import sys

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import QPointF, QRectF, Qt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PNG = os.path.join(ROOT, 'app', 'icon.png')
OUT_ICO = os.path.join(ROOT, 'app', 'icon.ico')
SIZES = [16, 24, 32, 48, 64, 128, 256]
RX, RY, TILT = 104, 40, 28      # the orbit ellipse (px) and its tilt (degrees)
SAT_AT = 40                     # where the satellite is on it (degrees round the ellipse)


def orbit(p, c, start):
    """half of the tilted orbit ellipse round c: start 0 = the back (upper) half, 180 = the front"""
    p.save()
    p.translate(c)
    p.rotate(-TILT)
    p.setBrush(Qt.NoBrush)
    p.setPen(QtGui.QPen(QtGui.QColor(190, 140, 255), 7, Qt.SolidLine, Qt.FlatCap))
    p.drawArc(QRectF(-RX, -RY, 2 * RX, 2 * RY), start * 16, 180 * 16)
    p.restore()


def draw(n):
    img = QtGui.QImage(n, n, QtGui.QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QtGui.QPainter(img)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.scale(n / 256.0, n / 256.0)          # draw on a 256 x 256 canvas

    # tile: deep navy, lighter at the top
    g = QtGui.QLinearGradient(0, 0, 0, 256)
    g.setColorAt(0, QtGui.QColor(28, 38, 66))
    g.setColorAt(1, QtGui.QColor(10, 14, 26))
    p.setPen(Qt.NoPen)
    p.setBrush(g)
    p.drawRoundedRect(QRectF(8, 8, 240, 240), 52, 52)

    # a few stars
    p.setBrush(QtGui.QColor(255, 255, 255, 150))
    for x, y, r in ((40, 52, 2.6), (196, 40, 2.2), (214, 112, 1.8), (64, 120, 1.6), (150, 30, 1.6)):
        p.drawEllipse(QPointF(x, y), r, r)

    c = QPointF(108, 152)
    orbit(p, c, 0)          # the back half of the orbit, behind the Earth

    # the Earth
    eg = QtGui.QRadialGradient(QPointF(88, 128), 90)
    eg.setColorAt(0, QtGui.QColor(70, 170, 230))
    eg.setColorAt(0.7, QtGui.QColor(28, 104, 176))
    eg.setColorAt(1, QtGui.QColor(16, 62, 120))
    p.setBrush(eg)
    p.drawEllipse(c, 62, 62)
    # land, clipped to the disc
    clip = QtGui.QPainterPath()
    clip.addEllipse(c, 62, 62)
    p.save()
    p.setClipPath(clip)
    p.setBrush(QtGui.QColor(70, 190, 120))
    land = QtGui.QPainterPath()
    land.moveTo(60, 120)
    land.cubicTo(80, 100, 112, 108, 118, 126)
    land.cubicTo(124, 146, 100, 150, 96, 170)
    land.cubicTo(92, 190, 70, 196, 58, 182)
    land.closeSubpath()
    land.moveTo(128, 150)
    land.cubicTo(146, 140, 170, 150, 168, 168)
    land.cubicTo(164, 192, 140, 206, 128, 196)
    land.cubicTo(118, 186, 130, 172, 128, 150)
    p.drawPath(land)
    p.restore()
    # thin atmosphere rim
    p.setBrush(Qt.NoBrush)
    p.setPen(QtGui.QPen(QtGui.QColor(140, 210, 255, 120), 3))
    p.drawEllipse(c, 63, 63)

    orbit(p, c, 180)        # the front half, over the Earth

    # the satellite sits on the back half of the orbit, up and to the right
    a = math.radians(TILT)
    lx, ly = RX * math.cos(math.radians(SAT_AT)), -RY * math.sin(math.radians(SAT_AT))
    sat = QPointF(c.x() + lx * math.cos(a) + ly * math.sin(a), c.y() - lx * math.sin(a) + ly * math.cos(a))
    # signal arcs from it towards the Earth
    down = math.degrees(math.atan2(-(c.y() - sat.y()), c.x() - sat.x()))
    for i, r in enumerate((36, 56)):
        p.setPen(QtGui.QPen(QtGui.QColor(255, 196, 60, 235 - 75 * i), 7, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(QRectF(sat.x() - r, sat.y() - r, 2 * r, 2 * r), int((down - 22) * 16), 44 * 16)

    # body and two solar panels, the panels square to the Earth
    p.save()
    p.translate(sat)
    p.rotate(-(down + 90))
    p.setPen(QtGui.QPen(QtGui.QColor(10, 14, 26), 3))
    p.setBrush(QtGui.QColor(90, 140, 220))
    p.drawRect(QRectF(-48, -11, 30, 22))      # panels
    p.drawRect(QRectF(18, -11, 30, 22))
    p.setPen(QtGui.QPen(QtGui.QColor(230, 236, 245), 4))
    p.drawLine(QPointF(-18, 0), QPointF(-12, 0))
    p.drawLine(QPointF(12, 0), QPointF(18, 0))
    p.setPen(QtGui.QPen(QtGui.QColor(10, 14, 26), 3))
    p.setBrush(QtGui.QColor(235, 238, 245))
    p.drawRoundedRect(QRectF(-13, -15, 26, 30), 5, 5)  # body
    p.restore()

    p.end()
    return img


def main():
    QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    draw(256).save(OUT_PNG)
    # .ico with every size drawn on its own (sharper than one image scaled down)
    from PIL import Image
    imgs = []
    for n in SIZES:
        buf = QtCore.QBuffer()
        buf.open(QtCore.QIODevice.WriteOnly)
        draw(n).save(buf, 'PNG')
        imgs.append(Image.open(io.BytesIO(bytes(buf.data()))).convert('RGBA'))
    imgs[-1].save(OUT_ICO, sizes=[(n, n) for n in SIZES], append_images=imgs[:-1])
    print(OUT_PNG, OUT_ICO)


if __name__ == '__main__':
    main()
