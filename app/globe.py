"""3D globe: the satellites, their orbital planes and your position fixes around a spinning Earth.

Drawn with NumPy (no 3D engine needed): a perspective camera looking at the Earth, textured with the
same dark map as the main window, the night side shaded from the Sun's real position, and everything
from the tracker's scene - satellites at orbital height, plane rings, the selected satellite's
track and the measured fixes - in true 3D (things behind the Earth are hidden or faint).
Only the window's own pixels are rendered, so zooming in costs no more than zooming out.

The camera looks at a point on the Earth's surface (the centre of the view) from some distance away,
tilted from straight down (top view) towards the horizon (side view).
  drag: move around the Earth          right-drag: tilt up/down, turn left/right
  wheel: zoom                          double-click: back to your location
"""
import concurrent.futures
import math
import os
import threading
import time

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import QPointF, Qt

import systems

R_EARTH = 6371.0
TEX_W, TEX_H = 2048, 1024       # equirectangular Earth texture
TILE_Z = 3                      # map tiles used for it (8 x 8 tiles = 2048 px)
FOV_DEG = 40.0                  # camera field of view across the window's shorter side
DIST_MIN, DIST_MAX = 0.12, 30.0  # camera distance from the point it looks at, Earth radii (30: GPS shell small in view)
TILT_MAX = 85.0
ORBIT_R = (R_EARTH + 781.0) / R_EARTH
# dist/tilt/heading of the camera, and how high the point it looks at is (Earth radii from the centre)
TOP_VIEW = dict(dist=3.6, tilt=0.0, heading=0.0, target_r=1.0)
SIDE_VIEW = dict(dist=1.8, tilt=72.0, heading=0.0, target_r=ORBIT_R)    # across the Earth, satellites mid-screen
# standing at your location: an eye just above the ground, looking along `heading` and `pitch` degrees up
EYE_R = (R_EARTH + 0.5) / R_EARTH
GROUND_FOV = (90.0, 25.0, 130.0)     # field of view: start, narrowest, widest
GROUND_START = dict(heading=0.0, pitch=25.0)
COMPASS = [('N', 0), ('NE', 45), ('E', 90), ('SE', 135), ('S', 180), ('SW', 225), ('W', 270), ('NW', 315)]
SKY = {'dark': ((7, 9, 10), (25, 45, 80)),            # standing-here sky: overhead, at the horizon (RGB)
       'light': ((70, 130, 200), (190, 215, 240))}
SETTLE_MS = 180                 # after the last zoom/drag step, re-render at full resolution


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

    def __init__(self, home, load_tile, map_theme='dark'):
        super().__init__()
        self.setMinimumSize(500, 500)
        self.setMouseTracking(True)
        self.home = home                    # (lat, lon)
        self.lat0, self.lon0 = home[0], home[1]     # the point the camera looks at
        self.dist, self.tilt, self.heading = TOP_VIEW['dist'], TOP_VIEW['tilt'], TOP_VIEW['heading']
        self.target_r = TOP_VIEW['target_r']
        self.ground = False                 # standing at your location instead of looking at the Earth
        self.pitch, self.fov = GROUND_START['pitch'], GROUND_FOV[0]
        self.scene = None
        self.tex = None
        self.tex32 = None                   # the texture packed as ARGB words, ready for the renderer
        self.theme = map_theme              # the Earth's map: light or dark, like the flat map
        self.threads = max(1, min(8, os.cpu_count() or 1))     # the Earth is rendered in bands, in parallel
        self.pool = concurrent.futures.ThreadPoolExecutor(self.threads)
        self.full_ms = 0.0                  # how long the last full-resolution Earth took
        self.earth = None                   # cached rendering of the Earth (QImage) and what it was for
        self.earth_key = None
        self.drag = None
        self.fast_until = 0.0
        self.hits = []
        self.settle = QtCore.QTimer(self)
        self.settle.setSingleShot(True)
        self.settle.timeout.connect(self.update)
        threading.Thread(target=self._load_texture, args=(load_tile,), daemon=True).start()

    def _load_texture(self, load_tile):
        tex = build_texture(load_tile)
        self.tex = tex
        lift = 1.35 if self.theme == 'dark' else 1.0     # the dark map is dim; lift it a little on the globe
        rgb = np.clip(tex.astype(np.float32) * lift, 0, 255).astype(np.uint32)
        self.tex32 = (np.uint32(0xFF000000) | (rgb[..., 0] << 16) | (rgb[..., 1] << 8) | rgb[..., 2]).ravel()
        self.earth_key = None
        QtCore.QMetaObject.invokeMethod(self, 'update', Qt.QueuedConnection)

    def set_theme(self, theme, load_tile):
        """light or dark: the map texture and the sky colours (the texture is rebuilt in the background)"""
        if theme == self.theme:
            return
        self.theme = theme
        threading.Thread(target=self._load_texture, args=(load_tile,), daemon=True).start()
        self.earth_key = None
        self.update()

    def set_scene(self, scene):
        first = self.scene is None
        self.scene = scene
        sats = (scene or {}).get('sats', [])
        if first and not self.ground and sats and min(s['alt'] for s in sats) > 2000:   # GPS only: whole shell
            self.show_preset(TOP_VIEW)
        self.update()

    def _top_alt(self):
        """height (km) of the highest satellite shown"""
        return max([s['alt'] for s in (self.scene or {}).get('sats', [])] + [781.0])

    def show_preset(self, preset):
        """Top / Side view, pulled back far enough for the highest satellite shown (Iridium 781 km, GPS 20,200 km)"""
        r = (R_EARTH + self._top_alt()) / R_EARTH
        if r <= ORBIT_R + 0.2:
            dist, target_r = preset['dist'], preset['target_r']
        elif preset is TOP_VIEW:
            dist, target_r = min(DIST_MAX, 1.15 * r / math.tan(math.radians(FOV_DEG / 2)) - 1), 1.0
        else:
            dist, target_r = min(DIST_MAX, preset['dist'] * r / ORBIT_R), r
        self.set_view(dist, preset['tilt'], self.heading, target_r)

    def set_view(self, dist, tilt, heading, target_r=1.0):
        self.ground = False
        self.dist, self.tilt, self.heading, self.target_r = dist, tilt, heading, target_r
        self._moved()

    def stand_here(self):
        """look at the sky as if standing at your location"""
        if not self.ground:
            self.ground = True
            self.heading, self.pitch, self.fov = GROUND_START['heading'], GROUND_START['pitch'], GROUND_FOV[0]
        self._moved()

    def _moved(self):
        """the camera moved: draw quickly now, sharply once it stops"""
        self.fast_until = time.time() + SETTLE_MS / 1000
        self.settle.start(SETTLE_MS + 20)
        self.update()

    # ---- the camera
    def _camera(self):
        """(position, right, up, forward, focal length in pixels) - worked out once per view and remembered"""
        key = (self.ground, self.lat0, self.lon0, self.home, self.heading, self.tilt, self.dist, self.target_r,
               self.pitch, self.fov, self.width(), self.height())
        if key != getattr(self, '_cam_key', None):
            self._cam_key, self._cam = key, self._camera_now()
        return self._cam

    def _camera_now(self):
        la, lo = math.radians(self.home[0] if self.ground else self.lat0), math.radians(self.home[1] if self.ground else self.lon0)
        U = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])   # up, at the target
        E = np.array([-math.sin(lo), math.cos(lo), 0.0])                                        # east
        N = np.cross(U, E)                                                                      # north
        h, t = math.radians(self.heading), math.radians(self.tilt)
        F = N * math.cos(h) + E * math.sin(h)           # the way we're facing, along the ground
        right = E * math.cos(h) - N * math.sin(h)
        if self.ground:
            pt = math.radians(self.pitch)
            fwd = F * math.cos(pt) + U * math.sin(pt)
            f = min(self.width(), self.height()) / 2 / math.tan(math.radians(self.fov / 2))
            return U * EYE_R, right, np.cross(right, fwd), fwd, f
        back = U * math.cos(t) - F * math.sin(t)        # from the target towards the camera
        pos = U * self.target_r + self.dist * back
        fwd = -back
        up = np.cross(right, fwd)
        f = min(self.width(), self.height()) / 2 / math.tan(math.radians(FOV_DEG / 2))
        return pos, right, up, fwd, f

    def _centre_y(self):
        """where the point we look at sits on screen: the middle when looking down, higher up as the camera
        tilts, so a side view isn't half empty sky above the horizon"""
        if self.ground:
            return self.height() / 2
        return self.height() * (0.5 - 0.2 * math.sin(math.radians(self.tilt)))

    def project(self, xyz):
        """(screen x, screen y, visible, in view) for Earth-fixed points (Earth radii).
        in view = in front of the camera; visible = in view and not hidden behind the Earth"""
        xyz = np.atleast_2d(np.asarray(xyz, float))
        pos, right, up, fwd, f = self._camera()
        v = xyz - pos
        zc = v @ fwd
        inview = zc > 1e-3
        zs = np.where(inview, zc, 1.0)
        sx = self.width() / 2 + f * (v @ right) / zs
        sy = self._centre_y() - f * (v @ up) / zs
        r = np.linalg.norm(xyz, axis=1)
        # on the ground: visible when that side faces the camera
        ground = (xyz / r[:, None]) @ pos > 1.0
        # above it: hidden if the line of sight passes through the Earth first
        a = np.einsum('ij,ij->i', v, v)
        b = 2 * (v @ pos)
        c = pos @ pos - 1.0
        disc = b * b - 4 * a * c
        t1 = (-b - np.sqrt(np.clip(disc, 0, None))) / (2 * a)
        above = ~((disc > 0) & (t1 > 0) & (t1 < 1))
        visible = inview & np.where(r < 1.01, ground, above)
        return sx, sy, visible, inview

    def _render_earth(self, fast):
        """the Earth (and its glow) as an image the size of the window - half resolution while moving.
        Only the pixels that hit the Earth get the full work: which ones do is worked out from 2D sums
        (the camera axes are orthonormal), and colours come from a packed 32-bit texture."""
        w, h = self.width(), self.height()
        # moving: full resolution if that is quick enough here, else half - sharp again once it stops
        k = 2 if fast and self.full_ms > 35 else 1
        W, H = max(1, w // k), max(1, h // k)
        sun = self.scene['sun'] if self.scene else (0.0, 0.0)
        night = bool(self.scene and self.scene.get('night'))
        fps = tuple((round(fp['lat'], 1), round(fp['lon'], 1), round(fp['radius'], 1), fp['selected'])
                    for fp in (self.scene or {}).get('footprints', []))
        key = (fps, round(self.lat0, 3), round(self.lon0, 3), round(self.dist, 4), round(self.tilt, 2), self.target_r,
               round(self.heading, 2), self.ground, round(self.pitch, 2), round(self.fov, 2), W, H, round(sun[0], 1),
               round(sun[1], 1), self.tex32 is not None, night, self.theme)
        if key == self.earth_key:
            return self.earth
        pos, right, up, fwd, f = self._camera()
        f = f / k
        f32 = np.float32
        px = ((np.arange(W, dtype=f32) + f32(0.5) - f32(W / 2)) / f32(f))[None, :]
        py = ((f32(self._centre_y() / k) - np.arange(H, dtype=f32) - f32(0.5)) / f32(f))[:, None]
        c = float(pos @ pos) - 1.0
        img = np.zeros((H, W), np.uint32)               # ARGB, one word per pixel
        tex = self.tex32 if self.tex32 is not None else self._grid32()

        def band(r0, r1):
            """rows r0..r1 of the image (the bands run in parallel: numpy lets go of the GIL)"""
            py_ = py[r0:r1]
            img_ = img[r0:r1]
            inv = 1 / np.sqrt(1 + px * px + py_ * py_)        # 1 / length of the un-normalised ray fwd + px right + py_ up
            b = (f32(fwd @ pos) + f32(right @ pos) * px + f32(up @ pos) * py_) * inv     # ray: pos + t d, b = d . pos
            disc = b * b - f32(c)
            root = np.sqrt(np.maximum(disc, 0))
            hit = (disc > 0) & (-b - root > 0)
            if self.ground:     # the sky: dark blue at the horizon fading to black overhead (light: pale blue)
                U = pos / np.linalg.norm(pos)
                el = np.clip((f32(fwd @ U) + f32(right @ U) * px + f32(up @ U) * py_) * inv, 0, 1)
                k_sky = (1 - el) ** 3
                lo, hi = SKY[self.theme]
                sky = np.uint32(0xFF000000)
                for sh, ch in ((16, 0), (8, 1), (0, 2)):
                    sky = sky | ((lo[ch] + (hi[ch] - lo[ch]) * k_sky).astype(np.uint32) << np.uint32(sh))
                img_[~hit] = sky[~hit]
            else:               # atmosphere: a faint blue glow just outside the edge of the Earth
                dmin = np.sqrt(np.maximum(f32(c + 1.0) - b * b, 0))
                glow = ~hit & (b < 0) & (dmin < 1.06)
                a = (np.clip((1.06 - dmin[glow]) / 0.06, 0, 1) * 80).astype(np.uint32)
                img_[glow] = (a << np.uint32(24)) | np.uint32(0x4C9AFF)
            iy, ix = np.nonzero(hit)
            if len(iy):
                pxh, pyh, invh = px[0, ix], py_[iy, 0], inv[iy, ix]
                t = -b[iy, ix] - root[iy, ix]
                dx = (f32(fwd[0]) + pxh * f32(right[0]) + pyh * f32(up[0])) * invh
                dy = (f32(fwd[1]) + pxh * f32(right[1]) + pyh * f32(up[1])) * invh
                dz = (f32(fwd[2]) + pxh * f32(right[2]) + pyh * f32(up[2])) * invh
                X, Y, Z = f32(pos[0]) + t * dx, f32(pos[1]) + t * dy, f32(pos[2]) + t * dz
                v = ((0.5 - np.arcsin(np.clip(Z, -1, 1)) / f32(math.pi)) * TEX_H).astype(np.int32)
                u = ((np.arctan2(Y, X) / f32(2 * math.pi) + 0.5) * TEX_W).astype(np.int32)
                np.clip(v, 0, TEX_H - 1, out=v)
                np.clip(u, 0, TEX_W - 1, out=u)
                col = tex[v * TEX_W + u]
                rgb = [(col >> np.uint32(sh)) & np.uint32(255) for sh in (16, 8, 0)]
                # coverage footprints, like the map's: red for the selected satellite, orange for others
                for fla, flo, frad, fsel in fps:
                    la, lo = math.radians(fla), math.radians(flo)
                    inside = (X * f32(math.cos(la) * math.cos(lo)) + Y * f32(math.cos(la) * math.sin(lo))
                              + Z * f32(math.sin(la))) > f32(math.cos(math.radians(frad)))
                    tint, kk = ((255, 60, 60), 77) if fsel else ((255, 160, 0), 38)     # 0.3 / 0.15 of 256
                    for ch in range(3):
                        rgb[ch][inside] = (rgb[ch][inside] * np.uint32(256 - kk) + np.uint32(tint[ch] * kk)) >> np.uint32(8)
                shade = 0.72 + 0.28 * np.clip(-(X * dx + Y * dy + Z * dz), 0, 1)    # darker towards the edge: round
                if night:
                    sl, so = math.radians(sun[0]), math.radians(sun[1])
                    day = np.clip((X * f32(math.cos(sl) * math.cos(so)) + Y * f32(math.cos(sl) * math.sin(so))
                                   + Z * f32(math.sin(sl))) * 8 + 0.5, 0, 1)          # soft terminator
                    shade = shade * (0.55 + 0.45 * day)
                s8 = (shade * 256).astype(np.uint32)
                out = np.uint32(0xFF000000)
                for ch, sh in zip(rgb, (16, 8, 0)):
                    out = out | (np.minimum((ch * s8) >> np.uint32(8), 255) << np.uint32(sh))
                img_[iy, ix] = out

        n = min(self.threads, H)
        cuts = [H * i // n for i in range(n + 1)]
        t0 = time.perf_counter()
        list(self.pool.map(lambda i: band(cuts[i], cuts[i + 1]), range(n)))
        if k == 1:
            self.full_ms = (time.perf_counter() - t0) * 1000
        self.earth = QtGui.QImage(img.data, W, H, W * 4, QtGui.QImage.Format_ARGB32).copy()
        self.earth_key = key
        return self.earth

    def _grid32(self):
        """while the map loads: a plain ocean colour with a light 30-degree grid, packed like tex32"""
        if getattr(self, '_grid', None) is None:
            lat = 90 - (np.arange(TEX_H) + 0.5) / TEX_H * 180
            lon = (np.arange(TEX_W) + 0.5) / TEX_W * 360 - 180
            grid = (np.abs((lat[:, None] + 90) % 30 - 15) > 14.2) | (np.abs((lon[None, :] + 180) % 30 - 15) > 14.2)
            self._grid = np.where(grid, np.uint32(0xFF32465A), np.uint32(0xFF182C3E)).ravel()
        return self._grid

    # ---- mouse
    def mousePressEvent(self, e):
        self.drag = (e.pos(), e.button(), self.lat0, self.lon0, self.pitch if self.ground else self.tilt,
                     self.heading, False)

    def mouseMoveEvent(self, e):
        if self.drag and e.buttons() & (Qt.LeftButton | Qt.RightButton):
            p0, button, la, lo, tilt, heading, _ = self.drag
            d = e.pos() - p0
            if abs(d.x()) + abs(d.y()) > 2:
                self.drag = (p0, button, la, lo, tilt, heading, True)
                if self.ground:                     # look around: drag the sky
                    deg = self.fov / min(self.width(), self.height())
                    self.heading = (heading - d.x() * deg + 180) % 360 - 180
                    self.pitch = max(-30.0, min(90.0, tilt + d.y() * deg))
                elif button == Qt.RightButton:      # tilt and turn
                    self.tilt = max(0.0, min(TILT_MAX, tilt - d.y() * 0.3))
                    self.heading = (heading - d.x() * 0.3 + 180) % 360 - 180
                else:                               # grab the Earth and move it
                    _, _, _, _, f = self._camera()
                    deg = math.degrees(min(self.dist, 3.0) / f)
                    fwd_deg, right_deg = d.y() * deg, -d.x() * deg
                    h = math.radians(heading)
                    north = fwd_deg * math.cos(h) - right_deg * math.sin(h)
                    east = fwd_deg * math.sin(h) + right_deg * math.cos(h)
                    self.lat0 = max(-89.0, min(89.0, la + north))
                    self.lon0 = (lo + east / max(0.2, math.cos(math.radians(la))) + 180) % 360 - 180
                self._moved()
            return
        hit = self._hit(e.pos())
        if hit:
            QtWidgets.QToolTip.showText(e.globalPos(), hit[1], self)
        else:
            QtWidgets.QToolTip.hideText()

    def mouseReleaseEvent(self, e):
        if self.drag and not self.drag[6] and self.drag[1] == Qt.LeftButton:
            hit = self._hit(e.pos())
            if hit and hit[0]:
                self.satelliteClicked.emit(hit[0])
        self.drag = None

    def center_home(self):
        """look at your location again, keeping the current zoom, tilt and direction
        (standing there: look back at the horizon to the north)"""
        self.lat0, self.lon0 = self.home
        if self.ground:
            self.heading, self.pitch = GROUND_START['heading'], GROUND_START['pitch']
        self._moved()

    def mouseDoubleClickEvent(self, e):
        self.center_home()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if getattr(self, 'corner', None) is not None:      # the map switches, top right
            self.corner.adjustSize()
            self.corner.move(self.width() - self.corner.width() - 10, 10)

    def wheelEvent(self, e):
        step = 1 / 1.15 if e.angleDelta().y() > 0 else 1.15
        if self.ground:
            self.fov = min(GROUND_FOV[2], max(GROUND_FOV[1], self.fov * step))
        else:
            self.dist = min(DIST_MAX, max(DIST_MIN, self.dist * step))
        self._moved()

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
        sx, sy, front, inview = self.project(np.asarray(xyz))
        for vis, alpha, wd in ((False, faint_alpha, 1.0), (True, colour.alpha(), width)):
            if alpha <= 0:
                continue
            c = QtGui.QColor(colour)
            c.setAlpha(alpha)
            p.setPen(QtGui.QPen(c, wd))
            p.setBrush(Qt.NoBrush)          # a line, not a filled shape
            # each stretch of segments wholly in front (or wholly behind) becomes one polyline
            seg = (front[1:] == vis) & (front[:-1] == vis) & inview[1:] & inview[:-1]
            if not seg.any():
                continue
            d = np.diff(np.concatenate([[0], seg.astype(np.int8), [0]]))
            for a, b in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)):
                p.drawPolyline(QtGui.QPolygonF([QPointF(x, y) for x, y in zip(sx[a:b + 1].tolist(), sy[a:b + 1].tolist())]))

    def _sprite(self, p, x, y, colour, scale, system):
        """a satellite icon, drawn once per look (colour, size, system) and then just stamped - dozens of
        satellites are drawn every frame"""
        import iridium_tracker as it
        key = (colour.rgba(), scale, system)
        cache = self.__dict__.setdefault('_sprites', {})
        if key not in cache:
            dpr = self.devicePixelRatioF()
            size = int(32 * scale) + 4
            pm = QtGui.QPixmap(int(size * dpr), int(size * dpr))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.transparent)
            q = QtGui.QPainter(pm)
            q.setRenderHint(QtGui.QPainter.Antialiasing)
            it.draw_satellite(q, size / 2, size / 2, colour, scale, system)
            q.end()
            cache[key] = (pm, size / 2)
        pm, half = cache[key]
        p.drawPixmap(QPointF(x - half, y - half), pm)

    def _sky_points(self, az, el):
        """points in the sky seen from your location (degrees), far enough out to project"""
        la, lo = math.radians(self.home[0]), math.radians(self.home[1])
        U = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
        E = np.array([-math.sin(lo), math.cos(lo), 0.0])
        N = np.cross(U, E)
        az, el = np.radians(np.atleast_1d(az))[:, None], np.radians(np.atleast_1d(el))[:, None]
        dirs = (N * np.cos(az) + E * np.sin(az)) * np.cos(el) + U * np.sin(el)
        return U * EYE_R + 0.5 * dirs

    def _draw_compass(self, p):
        """standing view: lines 30 and 60 degrees up, and the compass points along the horizon"""
        az = np.arange(0, 361, 3.0)
        for el in (30.0, 60.0):
            self._line(p, self._sky_points(az, np.full_like(az, el)), QtGui.QColor(139, 147, 161, 70), 1.0, faint_alpha=0)
            x, y, _, inview = self.project(self._sky_points([self.heading], [el]))
            if inview[0]:
                p.setPen(QtGui.QColor(139, 147, 161, 150))
                p.drawText(QPointF(x[0] + 4, y[0] - 3), '%d°' % el)
        font = p.font()
        bold = QtGui.QFont(font)
        bold.setBold(True)
        for name, a in COMPASS:
            x, y, _, inview = self.project(self._sky_points([a], [0.0]))
            if not inview[0]:
                continue
            p.setPen(QtGui.QPen(QtGui.QColor(139, 147, 161, 160), 1))
            p.drawLine(QPointF(x[0], y[0]), QPointF(x[0], y[0] - 8))
            p.setFont(bold if len(name) == 1 else font)
            p.setPen(QtGui.QColor('#e6e9ef') if len(name) == 1 else QtGui.QColor('#8b93a1'))
            p.drawText(QtCore.QRectF(x[0] - 15, y[0] - 26, 30, 16), Qt.AlignCenter, name)
        p.setFont(font)

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QtGui.QColor('#07090d'))
        moving = bool(self.drag and self.drag[6]) or time.time() < self.fast_until
        img = self._render_earth(fast=moving)
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
        p.drawImage(QtCore.QRectF(0, 0, w, h), img)
        self.hits = []
        if self.ground:
            self._draw_compass(p)
        sc = self.scene
        if sc:
            import iridium_tracker as it      # colours shared with the map
            for pl in sc.get('planes', []):
                col = QtGui.QColor(*it.PLANE_COLORS.get(pl['plane'], it.PLANE_COLORS[None]))
                col.setAlpha(230 if pl['selected'] else 160)
                ring = [(la, lo) for la, lo in pl['ring']]
                self._line(p, to_xyz(np.array([r[0] for r in ring]), np.array([r[1] for r in ring]), 781.0),
                           col, 2.5 if pl['selected'] else 1.6)
            for fp in sc.get('footprints', []):
                ring = np.array(it.circle_points(fp['lat'], fp['lon'], fp['radius']))
                self._line(p, to_xyz(ring[:, 0], ring[:, 1], 0.0),
                           QtGui.QColor(230, 60, 70) if fp['selected'] else QtGui.QColor(220, 130, 20), 1.5)
            for tr in sc.get('tracks', []):
                for pts, col in ((tr.get('space_past', tr['past']), it.C_PAST),
                                 (tr.get('space_future', tr['future']), it.C_FUTURE)):
                    for seg in _segments(pts):
                        self._line(p, to_xyz(seg[:, 0], seg[:, 1], tr.get('alt_km', 781.0)), QtGui.QColor(col),
                                   2.5 if tr['selected'] else 1.8)
            # measured positions (the satellite's own report), at their real height - all projected at once
            fixes = sc.get('fixes', [])
            if fixes:
                fxs, fys, ffront, _ = self.project(to_xyz(np.array([fx['lat'] for fx in fixes]),
                                                          np.array([fx['lon'] for fx in fixes]),
                                                          np.array([fx['alt_km'] for fx in fixes], float)))
                for fx, x, y, front in zip(fixes, fxs, fys, ffront):
                    if front:
                        c = QtGui.QColor(it.C_FIX)
                        c.setAlpha(int(255 * fx['alpha']))
                        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, int(200 * fx['alpha'])), 1))
                        p.setBrush(c)
                        p.drawEllipse(QPointF(x, y), 3.5, 3.5)
            # you
            hx, hy, hf, _ = self.project(to_xyz(self.home[0], self.home[1], 2.0))
            if hf[0] and not self.ground:
                p.setPen(QtGui.QPen(Qt.white, 1.5))
                p.setBrush(it.C_HOME)
                p.drawEllipse(QPointF(hx[0], hy[0]), 4, 4)
                p.setPen(QtGui.QColor(255, 150, 150))
                p.drawText(QPointF(hx[0] + 7, hy[0] + 4), 'You')
            # satellites
            font = p.font()
            font.setPointSize(8)
            p.setFont(font)
            sats = sorted(sc.get('sats', []), key=lambda st: st['selected'])
            if sats:        # all projected at once
                sxs, sys_, sfront, sview = self.project(to_xyz(np.array([st['lat'] for st in sats]),
                                                               np.array([st['lon'] for st in sats]),
                                                               np.array([st['alt'] for st in sats], float)))
            for k, st in enumerate(sats):
                x, y, front, inview = sxs[k], sys_[k], sfront[k], sview[k]
                if not inview:
                    continue
                col = QtGui.QColor(40, 190, 90) if st['el'] > 0 else QtGui.QColor(150, 156, 168)
                p.setOpacity(1.0 if front else 0.12)       # behind the Earth: just a hint
                if st.get('live') and front:               # heard by your receiver in the last minute
                    p.setPen(QtGui.QPen(QtGui.QColor(0, 200, 0), 2))
                    p.setBrush(Qt.NoBrush)
                    p.drawEllipse(QPointF(x, y), 14, 14)
                self._sprite(p, x, y, col, 1.2 if st['selected'] else 0.85, st.get('system', 'iridium'))
                if st['selected']:
                    it.draw_selection(p, x, y, 16.0)
                if front and (st['selected'] or st['el'] > 0):
                    p.setPen(QtGui.QColor('#e6e9ef'))      # (space is dark in both themes)
                    p.drawText(QPointF(x + 12, y - 8), systems.short_name(st['name']))
                if front:
                    self.hits.append((x, y, st['name'], '<b>%s</b><br>%s<br>altitude %.0f km<br>%s' % (
                        st['name'], st['ident'], st['alt'],
                        '%.0f&deg; above your horizon' % st['el'] if st['el'] > 0 else 'below your horizon')))
            p.setOpacity(1.0)
        p.setPen(QtGui.QColor('#8b93a1'))
        p.drawText(QPointF(10, h - 10), 'Drag: look around  ·  wheel: zoom  ·  double-click: face north' if self.ground else
                   'Drag: move  ·  right-drag: tilt / turn  ·  wheel: zoom  ·  double-click: back to you')
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
    def __init__(self, parent, home, load_tile, map_theme='dark'):
        super().__init__(None)      # its own window: taskbar button, minimize/maximize (closed with the main window)
        self.setWindowFlags(Qt.Window | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
        self.setWindowTitle('Globe - satellites and orbits from your own observations')
        self.view = GlobeView(home, load_tile, map_theme)
        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(8, 6, 8, 0)
        for label, tip, preset in (('Top view', 'Look straight down on the Earth', TOP_VIEW),
                                   ('Side view', 'Look across the Earth towards the horizon, with the satellites '
                                                 'passing above it', SIDE_VIEW)):
            b = QtWidgets.QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(lambda _, v=preset: self.view.show_preset(v))
            bar.addWidget(b)
        stand = QtWidgets.QPushButton('🧍  Stand here')
        stand.setToolTip('See the sky as if you were standing at your location: the satellites passing overhead,\n'
                         'with the compass along the horizon. Drag to look around, wheel to zoom.')
        stand.clicked.connect(self.view.stand_here)
        bar.addWidget(stand)
        me = QtWidgets.QPushButton('◎  Center on me')
        me.setToolTip('Bring your location back to the centre of the view (top or side view)')
        me.clicked.connect(self.view.center_home)
        bar.addWidget(me)
        bar.addWidget(QtWidgets.QLabel('<span style="color:#8b93a1">Top/side view: right-drag to tilt and turn</span>'))
        bar.addStretch(1)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(self.view, 1)
        self.resize(820, 860)
