"""Reads the broadcast orbits (ephemerides) out of an RTCM 3 stream - what pocket_trk writes with -rtcm.

pocket_trk keeps its ephemeris log lines ($EPH) switched off and has no option to switch them on, but it sends
every ephemeris it decodes as RTCM 3 messages: 1019 (GPS), 1045 / 1046 (Galileo F/NAV, I/NAV), 1042 (BeiDou)
and 1020 (GLONASS). This turns
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
    elif msg == 1042:                                # BeiDou (RTKLIB decode_type1042)
        prn, week, sva = b.u(6), b.u(13), b.u(4)
        idot = b.s(14) * 2 ** -43 * SC2RAD
        iode, toc = b.u(5), b.u(17) * 8.0
        af2, af1, af0 = b.s(11) * 2 ** -66, b.s(22) * 2 ** -50, b.s(24) * 2 ** -33
        iodc = b.u(5)
        crs, deln = b.s(18) * 2 ** -6, b.s(16) * 2 ** -43 * SC2RAD
        M0 = b.s(32) * 2 ** -31 * SC2RAD
        cuc, e, cus = b.s(18) * 2 ** -31, b.u(32) * 2 ** -33, b.s(18) * 2 ** -31
        sqrtA, toes = b.u(32) * 2 ** -19, b.u(17) * 8.0
        cic, OMG0 = b.s(18) * 2 ** -31, b.s(32) * 2 ** -31 * SC2RAD
        cis, i0 = b.s(18) * 2 ** -31, b.s(32) * 2 ** -31 * SC2RAD
        crc, omg = b.s(18) * 2 ** -6, b.s(32) * 2 ** -31 * SC2RAD
        OMGd = b.s(24) * 2 ** -43 * SC2RAD
        tgd = b.s(10) * 1e-10
        b.s(10)                                      # second group delay
        svh, flag, fit, code = b.u(1), 0, 0.0, 0
        # BDT week (13 bits) -> GPS week; BeiDou time runs 14 s behind GPST
        cur_bdt = int((now - GPS_EPOCH) // 604800) - 1356
        week = cur_bdt - ((cur_bdt - week) % 8192) + 1356
        sat, sig = 'C%02d' % prn, 'B1I'
        toe_gpst, toc_gpst = _gpst(week, toes) + 14, _gpst(week, toc) + 14
        return sat, sig, _fields(locals())
    elif msg == 1020:
        return decode_glonass(payload, now)
    else:
        return None
    toe_gpst = _gpst(week, toes)
    toc_gpst = _gpst(week, toc)
    if toc_gpst - toe_gpst > 302400:                 # toc in the neighbouring week
        toc_gpst -= 604800
    elif toe_gpst - toc_gpst > 302400:
        toc_gpst += 604800
    return sat, sig, _fields(locals())


def _fields(v):
    """the decoded values -> {gnss.FIELDS: value}"""
    return {'iode': v['iode'], 'iodc': v['iodc'], 'sva': v['sva'], 'svh': v['svh'], 'toe_gpst': int(v['toe_gpst']),
            'toc_gpst': int(v['toc_gpst']), 'ttr_gpst': int(v['now'] + 18), 'A': v['sqrtA'] ** 2, 'e': v['e'],
            'i0': v['i0'], 'OMG0': v['OMG0'], 'omg': v['omg'], 'M0': v['M0'], 'deln': v['deln'], 'OMGd': v['OMGd'],
            'idot': v['idot'], 'crc': v['crc'], 'crs': v['crs'], 'cuc': v['cuc'], 'cus': v['cus'], 'cic': v['cic'],
            'cis': v['cis'], 'toes': v['toes'], 'fit': v['fit'], 'af0': v['af0'], 'af1': v['af1'], 'af2': v['af2'],
            'tgd': v['tgd'], 'code': v['code'], 'flag': v['flag']}


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


def _sm(b, n):
    """a sign-magnitude field (GLONASS)"""
    x = b.u(n)
    mag = x & ((1 << (n - 1)) - 1)
    return -mag if x >> (n - 1) else mag


def decode_glonass(payload, now=None):
    """RTCM 1020 (GLONASS ephemeris; RTCM 10403.3, as RTKLIB's rtcm3.c decodes it) -> ('R16', 'G1CA', fields)
    with gnss.GLO_FIELDS (m, m/s, m/s2; toe = unix time of tb), or None"""
    now = now or time.time()
    b = _Bits(payload)
    if b.u(12) != 1020:
        return None
    slot = b.u(6)
    fcn = b.u(5) - 7
    b.u(2 + 2)                              # almanac health (+ its availability), P1
    b.u(5 + 6 + 1)                          # tk (h, min, 30 s)
    bn = b.u(1)
    b.u(1)                                  # P2
    tb = b.u(7) * 900.0                     # minutes of the Moscow day (UTC + 3 h), 15-min steps
    vx, x, ax = _sm(b, 24) * 2 ** -20 * 1e3, _sm(b, 27) * 2 ** -11 * 1e3, _sm(b, 5) * 2 ** -30 * 1e3
    vy, y, ay = _sm(b, 24) * 2 ** -20 * 1e3, _sm(b, 27) * 2 ** -11 * 1e3, _sm(b, 5) * 2 ** -30 * 1e3
    vz, z, az = _sm(b, 24) * 2 ** -20 * 1e3, _sm(b, 27) * 2 ** -11 * 1e3, _sm(b, 5) * 2 ** -30 * 1e3
    b.u(1)                                  # P3
    gamn = _sm(b, 11) * 2 ** -40
    b.u(2 + 1)                              # P, ln
    taun = _sm(b, 22) * 2 ** -30
    # tb is a time of the Moscow day: the one nearest `now`
    msk = now + 3 * 3600.0
    day = math.floor(msk / 86400) * 86400
    toe = day + tb - 3 * 3600.0
    toe += 86400 * round((now - toe) / 86400)
    import gnss
    toe = gnss.glo_check_toe(toe, now)         # (now: when it was received)
    if toe is None:
        return None
    fields = {'toe': toe, 'x': x, 'y': y, 'z': z, 'vx': vx, 'vy': vy, 'vz': vz, 'ax': ax, 'ay': ay, 'az': az,
              'taun': taun, 'gamn': gamn, 'fcn': fcn, 'slot': slot, 'glo': True, 'health': bn}
    return 'R%02d' % slot, 'G1CA', fields
