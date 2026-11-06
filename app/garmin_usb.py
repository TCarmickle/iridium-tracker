"""Live data from a Garmin handheld GPS over USB (Garmin's own USB protocol, through Garmin's USB driver).

With the handheld's Interface set to Garmin Serial or Garmin Spanner, and "No" to mass storage, Windows shows it as
"Garmin USB GPS". This talks to it the way Garmin's protocol document (Garmin Device Interface Specification) and
GPSBabel do, and asks for its position once a second:
  PVT (packet 51, D800)   position, height (above the WGS84 ellipsoid and above sea level), Garmin's own error
                          estimates (EPE / EPH / EPV), fix type (2D / 3D, 'diff' = with WAAS corrections), GPS time
  satellites (packet 114) each channel's satellite, signal level, elevation, azimuth and status (has ephemeris,
                          WAAS-corrected, used in the fix) - undocumented, as GPSBabel reads it

  python garmin_usb.py [seconds]        prints what it receives
  python garmin_usb.py log FILE         records it to FILE until stopped, reconnecting as it comes and goes
  GarminUSB().open(); .product(); .start_pvt(); .read() -> ('pvt', dict) / ('sats', list) / (other, None)
"""
import ctypes
import math
import struct
import sys
import time
from ctypes import wintypes

GUID_GARMIN = (0x2C9C45C2, 0x8E7D, 0x4C08, (0xA1, 0x2D, 0x81, 0x6B, 0xBA, 0xE7, 0x22, 0xC0))
IOCTL_ASYNC_IN = 0x00222140
IOCTL_USB_PACKET_SIZE = 0x00222144
IOCTL_API_VERSION = 0x00222000
INTERRUPT_SIZE = 64
FIX_TYPES = {0: 'none', 1: 'invalid', 2: '2D', 3: '3D', 4: '2D+WAAS', 5: '3D+WAAS'}
GARMIN_EPOCH = 631065600            # 1989-12-31 00:00 UTC (Garmin's day count starts here)

# packet types / ids
T_USB, T_APP = 0, 20
P_DATA_AVAILABLE, P_START_SESSION, P_SESSION_STARTED = 2, 5, 6
P_COMMAND, P_PROTOCOLS, P_PRODUCT_RQST, P_PRODUCT_DATA = 10, 253, 254, 255
P_PVT, P_SATS = 51, 114
CMD_START_PVT, CMD_STOP_PVT = 49, 50


class GUID(ctypes.Structure):
    _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD), ('Data3', wintypes.WORD),
                ('Data4', ctypes.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('InterfaceClassGuid', GUID), ('Flags', wintypes.DWORD),
                ('Reserved', ctypes.c_void_p)]


def _guid():
    g = GUID()
    g.Data1, g.Data2, g.Data3 = GUID_GARMIN[0], GUID_GARMIN[1], GUID_GARMIN[2]
    for i, b in enumerate(GUID_GARMIN[3]):
        g.Data4[i] = b
    return g


def device_paths():
    """the Garmin USB devices present (their Windows device paths)"""
    sa = ctypes.WinDLL('setupapi', use_last_error=True)
    sa.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    sa.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), ctypes.c_wchar_p, ctypes.c_void_p, wintypes.DWORD]
    sa.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD,
                                               ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
    sa.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                                    ctypes.c_void_p]
    sa.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    g = _guid()
    h = sa.SetupDiGetClassDevsW(ctypes.byref(g), None, None, 0x12)      # DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    if not h or h == ctypes.c_void_p(-1).value:
        return []
    out = []
    try:
        i = 0
        while True:
            d = SP_DEVICE_INTERFACE_DATA()
            d.cbSize = ctypes.sizeof(d)
            if not sa.SetupDiEnumDeviceInterfaces(h, None, ctypes.byref(g), i, ctypes.byref(d)):
                break
            need = wintypes.DWORD()
            sa.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(d), None, 0, ctypes.byref(need), None)
            buf = ctypes.create_string_buffer(need.value)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W: cbSize (8 on 64-bit Windows) then the path
            struct.pack_into('I', buf, 0, 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6)
            if sa.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(d), buf, need, None, None):
                out.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
            i += 1
    finally:
        sa.SetupDiDestroyDeviceInfoList(h)
    return out


class GarminUSB:
    def __init__(self, path=None):
        self.path = path
        self.h = None
        self.k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self.k32.CreateFileW.restype = wintypes.HANDLE
        self.k32.CreateFileW.argtypes = [ctypes.c_wchar_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                             ctypes.c_void_p]
        self.bulk = False
        self.packet_size = 64
        self.unit_id = None

    def open(self):
        if not self.path:
            paths = device_paths()
            if not paths:
                raise OSError('no Garmin USB GPS found (driver installed? interface Garmin Serial/Spanner, and "No" '
                              'to mass storage?)')
            self.path = paths[0]
        self.h = self.k32.CreateFileW(self.path, 0xC0000000, 0, None, 3, 0x80, None)
        if self.h == wintypes.HANDLE(-1).value:
            raise OSError('could not open the Garmin (%d) - is another program using it?' % ctypes.get_last_error())
        n = wintypes.DWORD()
        size = ctypes.c_uint32()
        if self.k32.DeviceIoControl(self.h, IOCTL_USB_PACKET_SIZE, None, 0, ctypes.byref(size), 4, ctypes.byref(n), None):
            self.packet_size = size.value or 64
        # start a session: the unit answers with its ID
        for _ in range(3):
            self.send(T_USB, P_START_SESSION)
            for _ in range(10):
                t, pid, data = self.recv()
                if t == T_USB and pid == P_SESSION_STARTED:
                    self.unit_id = struct.unpack('<I', data[:4])[0] if len(data) >= 4 else None
                    return self
        raise OSError('the Garmin did not answer the session start')

    def close(self):
        if self.h:
            try:
                self.send(T_APP, P_COMMAND, struct.pack('<H', CMD_STOP_PVT))
            except OSError:
                pass
            self.k32.CloseHandle(self.h)
            self.h = None

    def send(self, ptype, pid, data=b''):
        pkt = struct.pack('<B3xH2xI', ptype, pid, len(data)) + data
        n = wintypes.DWORD()
        if not self.k32.WriteFile(self.h, pkt, len(pkt), ctypes.byref(n), None):
            raise OSError('write to the Garmin failed (%d)' % ctypes.get_last_error())
        if len(pkt) % self.packet_size == 0:
            self.k32.WriteFile(self.h, b'', 0, ctypes.byref(n), None)

    def recv(self):
        """one packet -> (type, id, data); (None, None, b'') when nothing came"""
        buf = ctypes.create_string_buffer(4096)
        n = wintypes.DWORD()
        if self.bulk:
            ok = self.k32.ReadFile(self.h, buf, 4096, ctypes.byref(n), None)
            if not ok or n.value == 0:
                self.bulk = False
                return None, None, b''
        else:
            ok = self.k32.DeviceIoControl(self.h, IOCTL_ASYNC_IN, None, 0, buf, INTERRUPT_SIZE, ctypes.byref(n), None)
            if not ok or n.value == 0:
                return None, None, b''
        raw = buf.raw[:n.value]
        if len(raw) < 12:
            return None, None, b''
        ptype, pid, size = struct.unpack('<B3xH2xI', raw[:12])
        if ptype == T_USB and pid == P_DATA_AVAILABLE:
            self.bulk = True        # the rest comes on the bulk pipe
        return ptype, pid, raw[12:12 + size]

    def product(self):
        """(product id, software version, description)"""
        self.send(T_APP, P_PRODUCT_RQST)
        for _ in range(30):
            t, pid, d = self.recv()
            if pid == P_PRODUCT_DATA and len(d) >= 4:
                prod, sw = struct.unpack('<Hh', d[:4])
                return prod, sw / 100.0, d[4:].split(b'\0')[0].decode(errors='replace')
        return None

    def start_pvt(self):
        self.send(T_APP, P_COMMAND, struct.pack('<H', CMD_START_PVT))

    def read(self):
        t, pid, d = self.recv()
        if t == T_APP and pid == P_PVT and len(d) >= 64:
            return 'pvt', parse_pvt(d)
        if t == T_APP and pid == P_SATS:
            return 'sats', parse_sats(d)
        return pid, None


def parse_pvt(d):
    """D800 position / velocity / time"""
    alt, epe, eph, epv, fix, tow, lat, lon, ve, vn, vu, msl, leap, wn_days = struct.unpack('<ffffhdddffffhi', d[:64])
    t = GARMIN_EPOCH + wn_days * 86400 + tow - leap
    return {'t': t, 'lat': math.degrees(lat), 'lon': math.degrees(lon), 'alt_ell': alt, 'msl': alt + msl,
            'geoid_sep': -msl, 'epe': epe, 'eph': eph, 'epv': epv, 'fix': FIX_TYPES.get(fix, str(fix)),
            'vel': (ve, vn, vu)}


def parse_sats(d):
    """the satellite records (7 bytes each, as GPSBabel reads them): prn, snr, elevation, azimuth, status bits
    (1 has ephemeris, 2 WAAS-corrected, 4 used in the fix, 8 rising)"""
    out = []
    for i in range(0, len(d) - 6, 7):
        svid, snr, el, az, st = struct.unpack('<BHBHB', d[i:i + 7])
        if svid == 255:
            continue
        out.append({'prn': svid, 'snr': snr, 'el': el, 'az': az, 'status': st,
                    'eph': bool(st & 1), 'diff': bool(st & 2), 'used': bool(st & 4)})
    return out


def log_forever(path):
    """record everything the Garmin sends to a file, reconnecting whenever it is unplugged or powered off:
       GSTATE: pc_time text              connected / lost, the product
       GPVT: pc_time gps_time lat lon h_ellipsoid h_msl epe eph epv fix
       GSATS: pc_time prn:snr:el:az:status ...
    pc_time is this PC's clock; gps_time the Garmin's own (both Unix seconds)"""
    def w(line):
        with open(path, 'a') as f:
            f.write(line + '\n')

    while True:
        try:
            g = GarminUSB().open()
        except OSError:
            time.sleep(1.0)
            continue
        try:
            prod = g.product()
            w('GSTATE: %.3f connected %s' % (time.time(), prod[2] if prod else '?'))
            g.start_pvt()
            geoid_done = False
            last = time.time()
            while time.time() - last < 5:       # nothing for 5 s: unplugged or switched off
                k, v = g.read()
                now = time.time()
                if k is None:
                    time.sleep(0.05)
                    continue
                last = now
                if k == 'pvt':
                    if v['fix'].startswith('3D') and not geoid_done:
                        geoid_done = True       # the local sea-level offset, for the tracker's heights
                        try:
                            import refpoints
                            refpoints.set_geoid_sep(v['alt_ell'] - v['msl'], prod[2] if prod else 'Garmin')
                        except Exception:
                            pass
                    w('GPVT: %.3f %.3f %.8f %.8f %.2f %.2f %.2f %.2f %.2f %s' % (
                        now, v['t'], v['lat'], v['lon'], v['alt_ell'], v['msl'], v['epe'], v['eph'], v['epv'], v['fix']))
                elif k == 'sats':
                    w('GSATS: %.3f %s' % (now, ' '.join('%d:%d:%d:%d:%d' % (s['prn'], s['snr'], s['el'], s['az'],
                                                                             s['status']) for s in v)))
        except OSError:
            pass
        w('GSTATE: %.3f lost' % time.time())
        try:
            g.close()
        except OSError:
            pass
        time.sleep(1.0)


def main():
    if len(sys.argv) > 2 and sys.argv[1] == 'log':
        log_forever(sys.argv[2])
        return
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 20
    print('Garmin USB devices:', device_paths())
    g = GarminUSB().open()
    print('session started, unit id', g.unit_id, '- packet size', g.packet_size)
    print('product:', g.product())
    g.start_pvt()
    end = time.time() + secs
    kinds = {}
    while time.time() < end:
        k, v = g.read()
        if k is None:
            time.sleep(0.05)
            continue
        kinds[k] = kinds.get(k, 0) + 1
        if k == 'pvt':
            print('PVT  %s  fix %-7s  %.6f %.6f  h(ell) %.1f  msl %.1f  EPE %.1f EPH %.1f EPV %.1f m' % (
                time.strftime('%H:%M:%S', time.gmtime(v['t'])), v['fix'], v['lat'], v['lon'], v['alt_ell'], v['msl'],
                v['epe'], v['eph'], v['epv']))
        elif k == 'sats':
            print('SATS ' + '  '.join('%d:%d/%d%s' % (s['prn'], s['snr'], s['el'], '*' if s['used'] else '')
                                      for s in v))
    print('packets:', kinds)
    g.close()


if __name__ == '__main__':
    main()
