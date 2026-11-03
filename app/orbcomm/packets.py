"""Orbcomm downlink packet table and packet parsing.

Packet table from fbieberly/ORBCOMM-receiver orbcomm_packet.py, Ephemeris decoding
from its realtime_decoder.py / file_decoder.py (MIT, see LICENSE.txt).
Changes: parsing returns a dict instead of printing; Ephemeris also decodes the
velocity (20-bit, +-7700 m/s full scale, as in file_decoder.py) and converts the
week + time of week to a unix time (see GPS_UTC_LEAP_SECONDS);
'Oribital' spelled 'Orbital'.
"""
from .helpers import fletcher_checksum, ecef_to_lla

packet_dict = {
    'Sync': {
        'header': '01100101',
        'hex_header': '65',
        'message_parts': [
            ('code', (0, 6)),  # Part name, (start idx, stop idx) in the hex string
            ('sat_id', (6, 8)),
        ],
    },
    'Message': {
        'header': '00011010',
        'hex_header': '1A',
        'message_parts': [
            ('msg_packet_num', (3, 4)),
            ('msg_total_length', (2, 3)),
            ('data', (4, 22)),
        ],
    },
    'Uplink_info': {
        'header': '00011011',
        'hex_header': '1B',
        'message_parts': [
            ('msg_packet_num', (3, 4)),
            ('msg_total_length', (2, 3)),
            ('data', (4, 22)),
        ],
    },
    'Downlink_info': {
        'header': '00011100',
        'hex_header': '1C',
        'message_parts': [
            ('msg_packet_num', (3, 4)),
            ('msg_total_length', (2, 3)),
            ('data', (4, 22)),
        ],
    },
    'Network': {
        'header': '00011101',
        'hex_header': '1D',
        'message_parts': [
            ('msg_packet_num', (3, 4)),
            ('msg_total_length', (2, 3)),
            ('data', (4, 22)),
        ],
    },
    'Fill': {
        'header': '00011110',
        'hex_header': '1E',
        'message_parts': [('data', (2, 22))],
    },
    'Ephemeris': {
        'header': '00011111',
        'hex_header': '1F',
        'message_parts': [
            ('sat_id', (2, 4)),
            ('data', (4, 48)),
        ],
    },
    'Orbital': {
        'header': '00100010',
        'hex_header': '22',
        'message_parts': [],
    },
}

GPS_EPOCH_UNIX = 315964800      # 1980-01-06 00:00:00 UTC
# GPS-UTC is 18 s (since 2017), but the Orbcomm week/time of week is already UTC:
# on fbieberly's 2019 recordings it matches the packet's own arrival time to 0.1 s
# with no correction, and is 17.6 s early with 18 s taken off.
GPS_UTC_LEAP_SECONDS = 0
MAX_R_SAT = 8378155.0           # m, full scale of the 20-bit position fields
MAX_V_SAT = 7700.0              # m/s, full scale of the 20-bit velocity fields
VAL_20_BITS = 1048576.0


def gps_to_unix(week, tow):
    return GPS_EPOCH_UNIX + week * 604800 + tow - GPS_UTC_LEAP_SECONDS


def _field20(nibbles, full_scale):
    # The payload's 5-nibble fields as the original decoder unpacks them
    temp = int(nibbles[:2][::-1], 16) + 256. * int(nibbles[2:4][::-1], 16) + 256 ** 2 * int(nibbles[4:], 16)
    return (2 * temp * full_scale) / VAL_20_BITS - full_scale


def decode_ephemeris(packet):
    """Ephemeris fields of a 24-byte Ephemeris packet (hex string)."""
    payload = ''.join([packet[xx:xx + 2] for xx in range(42, 2, -2)])
    week = int(payload[:4], 16)
    tow = int(payload[4:10], 16)

    zdot = payload[10:15][::-1]
    ydot = payload[15:20][::-1]
    xdot = payload[20:25][::-1]
    zpos = payload[25:30][::-1]
    ypos = payload[30:35][::-1]
    xpos = payload[35:40][::-1]

    x, y, z = (_field20(p, MAX_R_SAT) for p in (xpos, ypos, zpos))
    vx, vy, vz = (_field20(v, MAX_V_SAT) for v in (xdot, ydot, zdot))
    lat, lon, alt = ecef_to_lla(x, y, z)
    return {
        'pos_ecef_km': (x / 1000.0, y / 1000.0, z / 1000.0),
        'vel_ecef_kms': (vx / 1000.0, vy / 1000.0, vz / 1000.0),
        'gps_week': week,
        'gps_tow': tow,
        'unix_time': gps_to_unix(week, tow),
        'lat': lat,
        'lon': lon,
        'alt_km': alt / 1000.0,
    }


def parse_packet(packet):
    """Parse one packet (upper-case hex string). Returns None if the Fletcher
    checksum fails, else a dict with 'packet_type', 'hex' and the table's parts
    (hex strings); Ephemeris packets add the decode_ephemeris() fields."""
    if fletcher_checksum(packet) != '0000':
        return None
    out = {'packet_type': 'Unrecognized', 'hex': packet}
    for packet_type, info in packet_dict.items():
        if packet[:2] == info['hex_header']:
            out['packet_type'] = packet_type
            for part, (start, stop) in info['message_parts']:
                out[part] = packet[start:stop]
            if packet_type == 'Ephemeris':
                out.update(decode_ephemeris(packet))
            break
    else:
        out['data'] = packet[2:]
    return out
