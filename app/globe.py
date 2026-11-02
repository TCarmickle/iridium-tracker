"""3D globe: the satellites, their orbital planes and your position fixes around a spinning Earth.

Drawn with NumPy (no 3D engine needed): an orthographic view of the Earth textured with the same
dark map as the main window, the night side shaded from the Sun's real position, and everything
from the tracker's scene - satellites at orbital height, plane rings, the selected satellite's
track and the measured fixes - in true 3D (things behind the Earth are hidden or faint).
Drag to spin, mouse wheel to zoom, double-click to return to your location.
"""
import math
import os
import threading
import time

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import QPointF, Qt

import paths

R_EARTH = 6371.0
TEX_W, TEX_H = 2048, 1024       # equirectangular Earth texture
TILE_Z = 3                      # map tiles used for it (8 x 8 tiles = 2048 px)


def to_xyz(lat, lon, alt_km=0.0):
    """Earth-fixed position in Earth radii"""
    la, lo = np.radians(lat), np.radians(lon)
    r = (R_EARTH + np.asarray(alt_km, float)) / R_EARTH
    return np.stack([r * np.cos(la) * np.cos(lo), r * np.cos(la) * np.sin(lo), r * np.sin(la)], -1)


def build_texture(load_tile):
    """an equirectangular Earth image from the Web-Mercator map tiles; load_tile(z, x, y) -> RGB array"""
    n = 2 ** TILE_Z
    mosaic = np.zeros((256 * n, 256 * n, 3), np.uint8)
    for x in range(n):
        for y in range(n):
            t = load_tile(TILE_Z, x, y)
            if t is not None:
                mosaic[y * 256:(y + 1) * 256, x * 256:(x + 1) * 256] = t
    lat = 90 - (np.arange(TEX_H) + 0.5) / TEX_H * 180
    lat_c = np.clip(lat, -85.05, 85.05)
    s = np.sin(np.radians(lat_c))
    my = (0.5 - np.log((1 + s) / (1 - s)) / (4 * math.pi)) * mosaic.shape[0]
    mx = (np.arange(TEX_W) + 0.5) / TEX_W * mosaic.shape[1]
    tex = mosaic[np.clip(my.astype(int), 0, mosaic.shape[0] - 1)[:, None], np.clip(mx.astype(int), 0, mosaic.shape[1] - 1)[None, :]]
    polar = np.abs(lat) > 85.05             # beyond the map: plain dark polar caps
    tex[polar] = (22, 30, 40)
    return tex


class GlobeView(QtWidgets.QWidget):
    satelliteClicked = QtCore.pyqtSignal(str)

    def __init__(self, home, load_tile):
        super().__init__()
        self.setMinimumSize(500, 500)
        self.setMouseTracking(True)
        self.home = home                    # (lat, lon)
        self.lat0, self.lon0 = home[0], home[1]
        self.zoom = 0.82                    # Earth radius as a fraction of half the window
        self.scene = None
        self.tex = None
        self.earth = None                   # cached rendering of the Earth (QImage) and what it was for
        self.earth_key = None
        self.drag = None
        self.hits = []
        threading.Thread(target=self._load_texture, args=(load_tile,), daemon=True).start()

    def _load_texture(self, load_tile):
        tex = build_texture(load_tile)
        self.tex = tex
        QtCore.QMetaObject.invokeMethod(self, 'update', Qt.QueuedConnection)

    def set_scene(self, scene):
        self.scene = scene
        self.update()

    # ---- the view
    def _axes(self):
        la, lo = math.radians(self.lat0), math.radians(self.lon0)
        c = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])   # towards you
        e = np.array([-math.sin(lo), math.cos(lo), 0.0])                                        # right
        n = np.cross(c, e)                                                                      # up
        return e, n, c

    def _scale(self):
        return min(self.width(), self.height()) / 2 * self.zoom

    def project(self, xyz):
        """(screen x, screen y, in front) for Earth-fixed points (Earth radii)"""
        e, n, c = self._axes()
        x, y, z = xyz @ e, xyz @ n, xyz @ c
        s = self._scale()
        front = (z > 0) | (x * x + y * y > 1)        # not hidden behind the Earth
        return self.width() / 2 + s * x, self.height() / 2 - s * y, front

    def _render_earth(self, fast):
        """the Earth as an image for the current view (half resolution while dragging)"""
        s = self._scale()
        k = 2 if fast else 1
        size = int(2 * s / k) + 2
        sun = self.scene['sun'] if self.scene else (0.0, 0.0)
        key = (round(self.lat0, 3), round(self.lon0, 3), round(s, 1), k, round(sun[0], 1), round(sun[1], 1),
               self.tex is not None, bool(self.scene and self.scene.get('night')))
        if key == self.earth_key:
            return self.earth
        g = (np.arange(size) + 0.5) / size * 2 - 1
        xs, ys = np.meshgrid(g, -g)
        rr = xs * xs + ys * ys
        m = rr < 1
        z = np.sqrt(np.clip(1 - rr, 0, 1))
        e, n, c = self._axes()
        x, y, zz = xs[m], ys[m], z[m]
        p = np.outer(x, e) + np.outer(y, n) + np.outer(zz, c)
        lat = np.degrees(np.arcsin(np.clip(p[:, 2], -1, 1)))
        lon = np.degrees(np.arctan2(p[:, 1], p[:, 0]))
        if self.tex is not None:
            u = np.clip(((lon + 180) / 360 * TEX_W).astype(int), 0, TEX_W - 1)
            v = np.clip(((90 - lat) / 180 * TEX_H).astype(int), 0, TEX_H - 1)
            rgb = self.tex[v, u].astype(np.float32)
        else:                               # map still loading: a plain ocean colour with a light grid
            rgb = np.tile(np.array([24, 44, 62], np.float32), (len(lat), 1))
            grid = (np.abs((lat + 90) % 30 - 15) > 14.2) | (np.abs((lon + 180) % 30 - 15) > 14.2)
            rgb[grid] = (50, 70, 90)
        rgb = rgb * 1.35                    # the map is dark-themed; lift it a little on the globe
        shade = 0.72 + 0.28 * zz            # a little darker towards the edge: looks round
        if self.scene and self.scene.get('night'):
            sl, so = np.radians(sun[0]), np.radians(sun[1])
            sun_v = np.array([math.cos(sl) * math.cos(so), math.cos(sl) * math.sin(so), math.sin(sl)])
            day = np.clip((p @ sun_v) * 8 + 0.5, 0, 1)        # soft terminator
            shade = shade * (0.55 + 0.45 * day)
        rgb = np.clip(rgb * shade[:, None], 0, 255).astype(np.uint8)
        img = np.zeros((size, size, 4), np.uint8)
        img[..., 3] = 0
        img[m, 0], img[m, 1], img[m, 2], img[m, 3] = rgb[:, 2], rgb[:, 1], rgb[:, 0], 255     # BGRA
        self.earth = (QtGui.QImage(img.tobytes(), size, size, size * 4, QtGui.QImage.Format_ARGB32).copy(), s)
        self.earth_key = key
        return self.earth

    # ---- mouse
    def mousePressEvent(self, e):
        self.drag = (e.pos(), self.lat0, self.lon0, False)

    def mouseMoveEvent(self, e):
        if self.drag and e.buttons() & Qt.LeftButton:
            p0, la, lo, _ = self.drag
            d = e.pos() - p0
            if abs(d.x()) + abs(d.y()) > 2:
                self.drag = (p0, la, lo, True)
                deg = math.degrees(1 / self._scale())
                self.lon0 = (lo - d.x() * deg / max(0.2, math.cos(math.radians(la))) + 180) % 360 - 180
                self.lat0 = max(-89.0, min(89.0, la + d.y() * deg))
                self.update()
            return
        hit = self._hit(e.pos())
        if hit:
            QtWidgets.QToolTip.showText(e.globalPos(), hit[1], self)
        else:
            QtWidgets.QToolTip.hideText()

    def mouseReleaseEvent(self, e):
        if self.drag and not self.drag[3]:
            hit = self._hit(e.pos())
            if hit and hit[0]:
                self.satelliteClicked.emit(hit[0])
        dragged = self.drag and self.drag[3]
        self.drag = None
        if dragged:
            self.update()               # sharp again

    def mouseDoubleClickEvent(self, e):
        self.lat0, self.lon0 = self.home
        self.update()

    def wheelEvent(self, e):
        self.zoom = min(6.0, max(0.3, self.zoom * (1.15 if e.angleDelta().y() > 0 else 1 / 1.15)))
        self.update()

    def _hit(self, pos):
        best = None
        for x, y, name, tip in self.hits:
            d = math.hypot(x - pos.x(), y - pos.y())
            if d < 12 and (best is None or d < best[0]):
                best = (d, name, tip)
        return best[1:] if best else None

    # ---- drawing
    def _line(self, p, xyz, colour, width, faint_alpha=35):
        """a 3D polyline: solid where it's in front of the Earth, faint where behind"""
        if len(xyz) < 2:
            return
        sx, sy, front = self.project(np.asarray(xyz))
        for vis, alpha, wd in ((False, faint_alpha, 1.0), (True, colour.alpha(), width)):
            c = QtGui.QColor(colour)
            c.setAlpha(alpha)
            p.setPen(QtGui.QPen(c, wd))
            p.setBrush(Qt.NoBrush)          # a line, not a filled shape
            path = QtGui.QPainterPath()
            last = -2
            for i in range(1, len(sx)):
                if front[i] == vis and front[i - 1] == vis:     # a piece wholly in front (or wholly behind)
                    if last != i - 1:
                        path.moveTo(sx[i - 1], sy[i - 1])
                    path.lineTo(sx[i], sy[i])
                    last = i
            p.drawPath(path)

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QtGui.QColor('#07090d'))
        cx, cy = w / 2, h / 2
        s = self._scale()
        # a faint atmosphere glow
        glow = QtGui.QRadialGradient(cx, cy, s * 1.08)
        glow.setColorAt(0.9, QtGui.QColor(76, 154, 255, 70))
        glow.setColorAt(1.0, QtGui.QColor(76, 154, 255, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QPointF(cx, cy), s * 1.08, s * 1.08)
        img, _s = self._render_earth(fast=bool(self.drag and self.drag[3]))
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
        p.drawImage(QtCore.QRectF(cx - s, cy - s, 2 * s, 2 * s), img)
        self.hits = []
        sc = self.scene
        if sc:
            import iridium_tracker as it      # colours shared with the map
            for pl in sc.get('planes', []):
                col = QtGui.QColor(*it.PLANE_COLORS.get(pl['plane'], it.PLANE_COLORS[None]))
                col.setAlpha(230 if pl['selected'] else 160)
                ring = [(la, lo) for la, lo in pl['ring']]
                self._line(p, to_xyz(np.array([r[0] for r in ring]), np.array([r[1] for r in ring]), 781.0),
                           col, 2.5 if pl['selected'] else 1.6)
            for tr in sc.get('tracks', []):
                for pts, col in ((tr['past'], it.C_PAST), (tr['future'], it.C_FUTURE)):
                    for seg in _segments(pts):
                        self._line(p, to_xyz(seg[:, 0], seg[:, 1], 781.0), QtGui.QColor(col), 2.5 if tr['selected'] else 1.8)
            # measured positions (the satellite's own report), at their real height
            for fx in sc.get('fixes', []):
                x, y, front = self.project(to_xyz(fx['lat'], fx['lon'], fx['alt_km'])[None, :])
                if front[0]:
                    c = QtGui.QColor(it.C_FIX)
                    c.setAlpha(int(255 * fx['alpha']))
                    p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, int(200 * fx['alpha'])), 1))
                    p.setBrush(c)
                    p.drawEllipse(QPointF(x[0], y[0]), 3.5, 3.5)
            # you
            hx, hy, hf = self.project(to_xyz(self.home[0], self.home[1], 2.0)[None, :])
            if hf[0]:
                p.setPen(QtGui.QPen(Qt.white, 1.5))
                p.setBrush(it.C_HOME)
                p.drawEllipse(QPointF(hx[0], hy[0]), 4, 4)
                p.setPen(QtGui.QColor(255, 150, 150))
                p.drawText(QPointF(hx[0] + 7, hy[0] + 4), 'You')
            # satellites
            font = p.font()
            font.setPointSize(8)
            p.setFont(font)
            for st in sorted(sc.get('sats', []), key=lambda st: st['selected']):
                x, y, front = self.project(to_xyz(st['lat'], st['lon'], st['alt'])[None, :])
                col = QtGui.QColor(230, 50, 60) if st['selected'] else (QtGui.QColor(40, 190, 90) if st['el'] > 0
                                                                       else QtGui.QColor(150, 156, 168))
                p.setOpacity(1.0 if front[0] else 0.12)       # behind the Earth: just a hint
                it.draw_satellite(p, x[0], y[0], col, 1.2 if st['selected'] else 0.85)
                if front[0] and (st['selected'] or st['el'] > 0):
                    p.setPen(QtGui.QColor(it.TEXT))
                    p.drawText(QPointF(x[0] + 12, y[0] - 8), st['name'].replace('IRIDIUM ', 'IR '))
                if front[0]:
                    self.hits.append((x[0], y[0], st['name'], '<b>%s</b><br>%s<br>altitude %.0f km<br>%s' % (
                        st['name'], st['ident'], st['alt'],
                        '%.0f&deg; above your horizon' % st['el'] if st['el'] > 0 else 'below your horizon')))
            p.setOpacity(1.0)
        p.setPen(QtGui.QColor('#8b93a1'))
        p.drawText(QPointF(10, h - 10), 'Drag to spin  ·  wheel to zoom  ·  double-click: back to you')
        p.drawText(QPointF(w - 200, h - 10), '© OpenStreetMap contributors')


def _segments(pts):
    """split a track (lat, lon or None) into continuous arrays"""
    out, cur = [], []
    for pt in pts:
        if pt is None:
            if len(cur) > 1:
                out.append(np.array(cur))
            cur = []
        else:
            cur.append(pt)
    if len(cur) > 1:
        out.append(np.array(cur))
    return out


class GlobeWindow(QtWidgets.QDialog):
    def __init__(self, parent, home, load_tile):
        super().__init__(parent)
        self.setWindowTitle('Globe - satellites and orbits from your own observations')
        self.view = GlobeView(home, load_tile)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.view)
        self.resize(820, 820)
