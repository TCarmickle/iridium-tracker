"""Reads the broadcast orbits (ephemerides) out of an RTCM 3 stream - what pocket_trk writes with -rtcm.

pocket_trk keeps its ephemeris log lines ($EPH) switched off and has no option to switch them on, but it sends
every ephemeris it decodes as RTCM 3 messages: 1019 (GPS) and 1045 / 1046 (Galileo F/NAV, I/NAV). This turns
them back into the fields of a $EPH line (gnss.FIELDS), so gnss.py reads them the same way.
Field layout: RTCM 10403.3, as written by RTKLIB's rtcm3e.c (which PocketSDR uses).
"""
import math
import time

SC2RAD = math.pi                 # semicircles -> radians
GPS_EPOCH = 315964800            # 1980-01-06 as Unix time (GPS week 0)


def _crc24q(data):
    crc = 0
    for b in data:
        crc ^= b << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def frames(buf):
    """the checksum-good message payloads in an RTCM 3 byte stream"""
    i = 0
    while i + 6 <= len(buf):
        if buf[i] != 0xD3:
            i += 1
            continue
        n = ((buf[i + 1] & 0x03) << 8) | buf[i + 2]
        if i + 6 + n > len(buf):
            break
        if _crc24q(buf[i:i + 3 + n]) == int.from_bytes(buf[i + 3 + n:i + 6 + n], 'big'):
            yield buf[i + 3:i + 3 + n]
            i += 6 + n
        else:
            i += 1


class _Bits:
    def __init__(self, data):
        self.v, self.n, self.p = int.from_bytes(data, 'big'), len(data) * 8, 0

    def u(self, k):
        x = (self.v >> (self.n - self.p - k)) & ((1 << k) - 1)
        self.p += k
        return x

    def s(self, k):
        x = self.u(k)
        return x - (1 << k) if x & (1 << (k - 1)) else x


def _full_week(week, modulo, now):
    """a broadcast week number counted modulo `modulo` -> the full GPS week nearest to now"""
    cur = int((now - GPS_EPOCH) // 604800)
    return cur - ((cur - week) % modulo)


def _gpst(week, sow):
    return GPS_EPOCH + week * 604800 + sow          # RTKLIB-style GPS seconds (like Unix time, no leap seconds)


def decode(payload, now=None):
    """one RTCM 3 payload -> (sat, sig, {gnss.FIELDS: value}) for a GPS / Galileo ephemeris, else None"""
    now = now or time.time()
    b = _Bits(payload)
    msg = b.u(12)
    if msg == 1019:
        prn, week, sva, code = b.u(6), b.u(10), b.u(4), b.u(2)
        idot = b.s(14) * 2 ** -43 * SC2RAD
        iode, toc = b.u(8), b.u(16) * 16.0
        af2, af1, af0 = b.s(8) * 2 ** -55, b.s(16) * 2 ** -43, b.s(22) * 2 ** -31
        iodc = b.u(10)
        crs, deln = b.s(16) * 2 ** -5, b.s(16) * 2 ** -43 * SC2RAD
        M0 = b.s(32) * 2 ** -31 * SC2RAD
        cuc, e, cus = b.s(16) * 2 ** -29, b.u(32) * 2 ** -33, b.s(16) * 2 ** -29
        sqrtA, toes = b.u(32) * 2 ** -19, b.u(16) * 16.0
        cic, OMG0 = b.s(16) * 2 ** -29, b.s(32) * 2 ** -31 * SC2RAD
        cis, i0 = b.s(16) * 2 ** -29, b.s(32) * 2 ** -31 * SC2RAD
        crc, omg = b.s(16) * 2 ** -5, b.s(32) * 2 ** -31 * SC2RAD
        OMGd, tgd = b.s(24) * 2 ** -43 * SC2RAD, b.s(8) * 2 ** -31
        svh, flag, fit = b.u(6), b.u(1), b.u(1)
        sat, sig, week = 'G%02d' % prn, 'L1CA', _full_week(week, 1024, now)
        fit = 0.0 if fit == 0 else 6.0
    elif msg in (1045, 1046):
        prn, week, iode, sva = b.u(6), b.u(12), b.u(10), b.u(8)
        idot = b.s(14) * 2 ** -43 * SC2RAD
        toc = b.u(14) * 60.0
        af2, af1, af0 = b.s(6) * 2 ** -59, b.s(21) * 2 ** -46, b.s(31) * 2 ** -34
        crs, deln = b.s(16) * 2 ** -5, b.s(16) * 2 ** -43 * SC2RAD
        M0 = b.s(32) * 2 ** -31 * SC2RAD
        cuc, e, cus = b.s(16) * 2 ** -29, b.u(32) * 2 ** -33, b.s(16) * 2 ** -29
        sqrtA, toes = b.u(32) * 2 ** -19, b.u(14) * 60.0
        cic, OMG0 = b.s(16) * 2 ** -29, b.s(32) * 2 ** -31 * SC2RAD
        cis, i0 = b.s(16) * 2 ** -29, b.s(32) * 2 ** -31 * SC2RAD
        crc, omg = b.s(16) * 2 ** -5, b.s(32) * 2 ** -31 * SC2RAD
        OMGd = b.s(24) * 2 ** -43 * SC2RAD
        tgd = b.s(10) * 2 ** -32
        svh, flag, fit, iodc, code = 0, 0, 0.0, iode, 0
        if msg == 1046:
            b.s(10)                                  # second group delay
            svh = (b.u(2) << 4) | (b.u(1) << 3)       # E5b health / validity
            svh |= (b.u(2) << 1) | b.u(1)             # E1-B health / validity
        else:
            svh = (b.u(2) << 4) | (b.u(1) << 3)       # E5a health / validity
        sat, sig, week = 'E%02d' % prn, 'E1B' if msg == 1046 else 'E5A', week + 1024     # GST week -> GPS week
    else:
        return None
    toe_gpst = _gpst(week, toes)
    toc_gpst = _gpst(week, toc)
    if toc_gpst - toe_gpst > 302400:                 # toc in the neighbouring week
        toc_gpst -= 604800
    elif toe_gpst - toc_gpst > 302400:
        toc_gpst += 604800
    fields = {'iode': iode, 'iodc': iodc, 'sva': sva, 'svh': svh, 'toe_gpst': int(toe_gpst), 'toc_gpst': int(toc_gpst),
              'ttr_gpst': int(now + 18), 'A': sqrtA ** 2, 'e': e, 'i0': i0, 'OMG0': OMG0, 'omg': omg, 'M0': M0,
              'deln': deln, 'OMGd': OMGd, 'idot': idot, 'crc': crc, 'crs': crs, 'cuc': cuc, 'cus': cus, 'cic': cic,
              'cis': cis, 'toes': toes, 'fit': fit, 'af0': af0, 'af1': af1, 'af2': af2, 'tgd': tgd, 'code': code,
              'flag': flag}
    return sat, sig, fields


def ephemerides(buf, now=None):
    """every GPS / Galileo ephemeris in an RTCM 3 byte stream -> [(sat, sig, fields)]"""
    out = []
    for payload in frames(buf):
        got = decode(payload, now)
        if got:
            out.append(got)
    return out


def split(buf):
    """(checksum-good payloads, bytes left over) of a growing RTCM 3 stream: the leftover is a frame still
    being written - prepend it to the next bytes read"""
    out, i = [], 0
    while i + 6 <= len(buf):
        if buf[i] != 0xD3:
            i += 1
            continue
        n = ((buf[i + 1] & 0x03) << 8) | buf[i + 2]
        if i + 6 + n > len(buf):
            break
        if _crc24q(buf[i:i + 3 + n]) == int.from_bytes(buf[i + 3 + n:i + 6 + n], 'big'):
            out.append(buf[i + 3:i + 3 + n])
            i += 6 + n
        else:
            i += 1
    return out, buf[i:]
