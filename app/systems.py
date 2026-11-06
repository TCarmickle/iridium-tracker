"""The satellite systems the tracker knows about.

Everything that differs between systems - which band to receive, what the satellites are called,
the carrier their Doppler shift is worked out for, what their orbits look like - lives here, so the
rest of the app can show all systems together and filter them.
"""
import math

IRIDIUM = {
    'key': 'iridium',
    'label': 'Iridium',
    'prefix': 'IRIDIUM ',            # satellite names start with this
    'short': 'IR ',                  # ...shortened to this on the map
    'colour': (76, 154, 255),
    'band_hz': (1616.0e6, 1626.5e6),
    'carrier_hz': 1626.1e6,          # Doppler is worked out for this frequency
    'inc_hint': math.radians(86.4),  # design inclination: helps a one-fix orbit fit
    'tle_group': 'iridium-NEXT',     # CelesTrak group, for satellite names only
    'capture_prefix': 'output-',
    'keep_kinds': ('IRA:', 'ITL:'),  # frames kept long-term in a capture's .frames.gz
    'antenna': 'about 4.6 cm (a quarter wave at 1.62 GHz), or a patch antenna facing up',
    'listen_hz': 1626.2708e6,        # tuned sound starts on the ring-alert channel
    'channel_hz': 36e3,
    'demod': 'am',                   # bursts: hear their envelope
    'bands': [(1616.0, 1626.0, 'duplex channels (phones, data)', (76, 154, 255, 28)),
              (1626.0, 1626.5, 'ring alerts & messaging', (76, 208, 125, 40))],
}

ORBCOMM = {
    'key': 'orbcomm',
    'label': 'Orbcomm',
    'prefix': 'ORBCOMM ',
    'short': 'OC ',
    'colour': (255, 159, 67),
    'band_hz': (137.0e6, 138.0e6),
    'carrier_hz': 137.5e6,
    'inc_hint': None,                # several orbit families (45, 47, 52 deg): no single design value
    'tle_group': 'orbcomm',
    'capture_prefix': 'orbcomm-',
    'keep_kinds': ('OEP:', 'OSY:'),
    'antenna': 'about 52 cm (a quarter wave at 137 MHz), or a V-dipole of 2 x 53 cm at 120 degrees',
    'listen_hz': 137.46e6,
    'channel_hz': 10e3,              # a 4800-baud channel plus its Doppler shift
    'demod': 'fm',                   # constant-envelope PSK: FM gives the familiar data warble
    'bands': [(137.2, 137.82, 'Orbcomm downlink channels', (255, 159, 67, 34))],
}

# ISS and amateur (ham) satellites: one system shown on the map and in the list, received on two bands.
# Their names don't share a prefix ("ISS (ZARYA)", "FUNCUBE-1 (AO-73)"), so names are registered as heard.
HAM = {
    'key': 'ham',
    'label': 'ISS & ham sats',
    'prefix': None,
    'short': '',
    'colour': (110, 220, 120),
    'carrier_hz': None,              # each pass's own frequency (2 m or 70 cm)
    'inc_hint': None,
    'tle_groups': ('stations', 'amateur'),   # CelesTrak groups, for satellite names only
}
_HAM_BAND = {
    'system': 'ham',
    'prefix': None,
    'short': '',
    'colour': HAM['colour'],
    'carrier_hz': None,
    'inc_hint': None,
    'tle_group': None,
    'tle_groups': HAM['tle_groups'],
    'keep_kinds': ('HFR:',),         # decoded frames (their payloads are never stored)
    'demod': 'nfm',
}
HAM2M = dict(_HAM_BAND, **{
    'key': 'ham2m',
    'label': 'ISS & ham 2 m',
    'band_hz': (144.0e6, 146.0e6),
    'rate': 2048000, 'center_hz': 145550000,    # 144.53-146.57 MHz: all the 2 m satellite downlinks
    'capture_prefix': 'ham2m-',
    'antenna': 'about 49 cm / 19 1/4 in (a quarter wave at 145.8 MHz), or a 2 m / 70 cm dual-band whip',
    'listen_hz': 145.825e6,          # the ISS's APRS digipeater
    'channel_hz': 12.5e3,
    'bands': [(145.8, 146.0, 'satellite subband', (110, 220, 120, 34))],
})
HAM70 = dict(_HAM_BAND, **{
    'key': 'ham70',
    'label': 'Ham 70 cm',
    'band_hz': (435.0e6, 438.0e6),
    'rate': 2400000, 'center_hz': 436725000,    # 435.5-437.9 MHz: most CubeSat downlinks
    'capture_prefix': 'ham70-',
    'antenna': 'about 16.5 cm / 6 1/2 in (a quarter wave at 436 MHz), or a 2 m / 70 cm dual-band whip',
    'listen_hz': 436.5e6,
    'channel_hz': 15e3,
    'bands': [(435.0, 438.0, 'satellite subband', (110, 220, 120, 34))],
})
# GNSS: GPS and Galileo both broadcast at 1575.42 MHz (GPS L1 C/A, Galileo E1B), below the
# noise; PocketSDR tracks them and decodes each satellite's own broadcast orbit (ephemeris)
GNSS = {
    'key': 'gnss',
    'label': 'GNSS (GPS / Galileo / GLONASS / BeiDou)',
    'prefix': None,
    'short': '',
    'colour': (190, 130, 255),
    'carrier_hz': 1575.42e6,
    'inc_hint': None,
    'tle_groups': ('gps-ops', 'galileo', 'beidou', 'sbas', 'glo-ops'),   # CelesTrak groups, for names only
}
GNSS_BAND = {
    'key': 'gnss1',
    'system': 'gnss',
    'label': 'GNSS L1 (GPS)',
    'prefix': None,
    'short': '',
    'colour': GNSS['colour'],
    'carrier_hz': GNSS['carrier_hz'],
    'inc_hint': None,
    'tle_group': None,
    'tle_groups': GNSS['tle_groups'],
    'band_hz': (1574.4e6, 1576.44e6),
    'rate': 2048000, 'center_hz': 1575420000,
    'capture_prefix': 'gnss-',
    'keep_kinds': ('GCH:', 'GEPH:', 'GPOS:', 'GSPP:'),
    'antenna': 'the active GPS antenna, plugged straight into the SDR (it is powered through the coax - no DC '
               'blocker), with open sky: outdoors or in a window',
    'listen_hz': 1575.42e6,
    'channel_hz': 36e3,
    'demod': 'am',                   # there is nothing to hear: the signals are under the noise
    'bands': [(1574.4, 1576.44, 'GPS L1 / Galileo E1 - under the noise', (190, 130, 255, 30))],
}
# GLONASS (Russia): L1 at 1602 MHz + 0.5625 MHz x its frequency channel (-7..+6). 27 MHz from GPS, so it is
# its own band; 2.4 MS/s around 1602 MHz covers about channels -2..+2 (the ones in the middle), and the
# satellites on them - each channel is shared by two satellites on opposite sides of the Earth.
GLONASS_BAND = dict(GNSS_BAND, **{
    'key': 'glo1',
    'label': 'GNSS L1 (GLONASS)',
    'band_hz': (1600.8e6, 1603.2e6),
    'rate': 2400000, 'center_hz': 1602000000,
    'capture_prefix': 'glo-',
    'antenna': 'the active GPS antenna (if it covers GLONASS too, as most do), plugged straight into the SDR, '
               'with open sky: outdoors or in a window',
    'listen_hz': 1602.0e6,
    'bands': [(1600.8, 1603.2, 'GLONASS L1 channels -2..+2 - under the noise', (190, 130, 255, 30))],
})
# BeiDou (China): B1I at 1561.098 MHz, 14 MHz below GPS, so its own band. Every BeiDou satellite (BDS-2 and BDS-3)
# sends B1I, with its broadcast orbit; pocket_trk decodes both. (BDS-3's newer B1C shares GPS's 1575.42 MHz,
# but pocket_trk decodes no orbit from it - tried first.)
BEIDOU_BAND = dict(GNSS_BAND, **{
    'key': 'bds1',
    'label': 'GNSS B1I (BeiDou)',
    'band_hz': (1560.07e6, 1562.12e6),
    'rate': 2048000, 'center_hz': 1561098000,
    'capture_prefix': 'bds-',
    'antenna': 'the active GPS antenna (most cover 1559-1610 MHz, BeiDou B1I included), plugged straight into the '
               'SDR, with open sky: outdoors or in a window',
    'listen_hz': 1561.098e6,
    'bands': [(1560.07, 1562.12, 'BeiDou B1I - under the noise', (190, 130, 255, 30))],
})
# Meteor-M (Russia's weather satellites): pictures of the Earth (LRPT, 137.9 MHz, backup 137.1 MHz).
# The receiver records the pass (meteor_rx.py) and SatDump turns it into pictures afterwards (images.py);
# they are shown in the Images section, not on the map. The SDR is tuned 200 kHz below 137.9 MHz so its centre
# spike (strong on E4000 tuners) stays off the signal.
METEOR = {
    'key': 'meteor',
    'system': 'meteor',
    'label': 'Meteor-M weather pictures',
    'prefix': 'METEOR',
    'short': '',
    'colour': (80, 200, 230),
    'carrier_hz': 137.9e6,
    'inc_hint': None,
    'tle_group': None,
    'band_hz': (137.85e6, 137.95e6),
    'rate': 1024000, 'center_hz': 137700000,
    'capture_prefix': 'meteor-',
    'keep_kinds': ('MSIG:',),
    'antenna': 'a whip about 21 in / 53 cm long (a quarter wave at 137.9 MHz - e.g. a telescopic one pulled out '
               'that far), upright on something metal, outdoors with open sky',
    'listen_hz': 137.9e6,
    'channel_hz': 120e3,
    'demod': 'fm',                   # the 72k-symbol signal: a steady hiss-like warble
    'bands': [(137.846, 137.954, 'Meteor-M LRPT pictures (137.9 MHz)', (80, 200, 230, 40))],
}
# each system's signal measure and its scale for the strength colours and bars: (weak - barely decodes,
# strong, unit). Iridium / Orbcomm / ham: the SNR of each burst, packet or frame (dB over the noise in its
# channel); GNSS: C/N0 every second (dB-Hz: the signal against the noise in 1 Hz - 35 is enough for the orbit)
IRIDIUM['signal'] = (12.0, 30.0, 'dB')
ORBCOMM['signal'] = (8.0, 25.0, 'dB')
HAM['signal'] = (6.0, 24.0, 'dB')
GNSS['signal'] = (28.0, 45.0, 'dB-Hz')
IRIDIUM['system'] = 'iridium'
ORBCOMM['system'] = 'orbcomm'
ORBCOMM.update(rate=1228800, center_hz=137500000)     # the decoder needs a multiple of 9600 S/s

METEOR['signal'] = (2.0, 12.0, 'dB')       # the pass's signal over the noise beside it (meteor_rx.py)
# Radio: no satellites, no decoding - just listening anywhere the SDR can tune (the Radio section, like SDR++).
# The SDR sits RADIO_OFFSET_HZ above the frequency listened to, so its centre spike stays off it; it moves when
# the frequency leaves the slice. It is last in ORDER: frequencies outside every other band belong to it.
RADIO_OFFSET_HZ = 250e3
RADIO = {
    'key': 'radio',
    'system': 'radio',
    'label': 'Radio',
    'prefix': None,
    'short': '',
    'colour': (255, 210, 60),
    'carrier_hz': None,
    'inc_hint': None,
    'tle_group': None,
    'band_hz': (24e6, 2300e6),       # what tuners like the E4000 / R820T reach
    'rate': 2048000,
    'capture_prefix': 'radio-',
    'keep_kinds': (),
    'antenna': 'the one made for the frequency you tune to (a quarter wave: 7125 / MHz centimetres)',
    'listen_hz': 100.0e6,
    'channel_hz': 180e3,
    'demod': 'wfm',
    'bands': [],                     # (its band plan is RADIO_PLAN, drawn on its own row)
}
# what is where across the radio spectrum (North America; MHz): shown on the spectrum's second label row, the narrow
# ones that sit inside a wider one on a third. Below ~24 MHz (~52 MHz for E4000 tuners) the SDR can't receive.
PLAN_COL = (150, 160, 180, 18)
RADIO_PLAN = [  # (lo, hi, name, row)
    (0.53, 1.7, 'AM broadcast', 1), (26.965, 27.405, 'CB radio', 1), (28.0, 29.7, '10 m ham', 1),
    (50.0, 54.0, '6 m ham', 1), (54.0, 88.0, 'TV (VHF low)', 1), (88.0, 108.0, 'FM broadcast radio', 1),
    (108.0, 118.0, 'aircraft navigation (VOR / ILS)', 1), (118.0, 137.0, 'aircraft voice (AM)', 1),
    (121.45, 121.55, 'air emergency 121.5', 2), (137.0, 138.0, 'weather & satellite downlinks', 1),
    (144.0, 148.0, '2 m ham', 1), (146.5, 146.54, 'calling 146.52', 2), (150.8, 156.0, 'business / public safety', 1),
    (156.0, 162.025, 'marine VHF', 1), (156.775, 156.825, 'marine ch 16', 2),
    (162.4, 162.55, 'NOAA Weather Radio', 2), (174.0, 216.0, 'TV (VHF high)', 1),
    (225.0, 400.0, 'military aircraft (AM)', 1), (400.0, 406.0, 'weather balloons (radiosondes)', 1),
    (406.0, 406.1, 'emergency beacons', 2), (420.0, 450.0, '70 cm ham', 1), (433.05, 434.79, '433 MHz remotes / sensors', 2),
    (450.0, 470.0, 'business / public safety (UHF)', 1), (462.55, 467.725, 'FRS / GMRS walkie-talkies', 2),
    (470.0, 608.0, 'TV (UHF)', 1), (698.0, 806.0, 'cell phones (700 MHz)', 1), (806.0, 824.0, 'public safety 800', 1),
    (824.0, 894.0, 'cell phones (850 MHz)', 1), (902.0, 928.0, '915 MHz sensors / LoRa', 1),
    (960.0, 1215.0, 'aircraft (DME, transponders)', 1), (1089.0, 1091.0, 'ADS-B 1090', 2),
    (1164.0, 1215.0, 'GNSS L5', 2), (1215.0, 1300.0, 'GNSS L2 / 23 cm ham', 1), (1525.0, 1559.0, 'Inmarsat', 1),
    (1559.0, 1610.0, 'GNSS L1 (GPS, Galileo, GLONASS)', 1), (1610.0, 1626.5, 'Iridium / Globalstar', 1),
    (1670.0, 1710.0, 'weather satellites (HRPT, GOES)', 1), (1850.0, 1990.0, 'cell phones (PCS)', 1),
    (2400.0, 2483.5, 'Wi-Fi / Bluetooth', 1),
]
ORDER = [IRIDIUM, ORBCOMM, HAM2M, HAM70, GNSS_BAND, GLONASS_BAND, BEIDOU_BAND, METEOR, RADIO]   # what the receiver can listen to (one at a time)
DISPLAY = [IRIDIUM, ORBCOMM, HAM, GNSS]               # what the map, list and Systems filter group satellites by
SYSTEMS = {s['key']: s for s in ORDER + DISPLAY}
DEFAULT = 'iridium'
_names = {}                                   # satellite name -> display system, for names without a prefix


def get(key):
    return SYSTEMS.get(key or DEFAULT, IRIDIUM)


def register(name, key):
    """remember which system a satellite heard by name belongs to (ham satellites)"""
    if name:
        _names[name] = get(get(key).get('system', key))


def of_name(name):
    """the (display) system a satellite belongs to, from its name"""
    if name in _names:
        return _names[name]
    for s in (IRIDIUM, ORBCOMM):
        if name and name.upper().startswith(s['prefix']):
            return s
    return IRIDIUM


def of_capture(cap_name):
    """the receive band a capture was recorded on, from its file name ('system' says its display system)"""
    for s in ORDER:
        if cap_name.startswith(s['capture_prefix']):
            return s
    return IRIDIUM


def of_frequency(hz):
    """the system whose band a frequency is in (or next to); anywhere else: Radio"""
    for s in ORDER:
        if s is RADIO:
            continue
        lo, hi = s['band_hz']
        if lo - 5e6 <= hz <= hi + 5e6:
            return s
    return RADIO


def band_tag(s):
    """'1.6 GHz', '137 MHz' - or 'any frequency' for Radio (for band lists)"""
    if s is RADIO:
        return 'any frequency'
    lo = s['band_hz'][0]
    return '%.1f GHz' % (lo / 1e9) if lo > 1e9 else '%.0f MHz' % (lo / 1e6)


def short_name(name):
    """'IRIDIUM 152' -> 'IR 152', 'ORBCOMM FM114' -> 'OC FM114'"""
    s = of_name(name)
    if s['prefix'] and name.upper().startswith(s['prefix']):
        return s['short'] + name[len(s['prefix']):]
    if s is GNSS and '(' in name:       # 'GPS BIIF-2  (PRN 01)' -> 'GPS 01'
        return name.split()[0] + ' ' + name[name.rindex('(') + 1:].rstrip(')').replace('PRN ', '').strip()
    if s is HAM:            # 'FUNCUBE-1 (AO-73)' -> 'AO-73', 'ISS (ZARYA)' -> 'ISS'
        if name.startswith('ISS'):
            return 'ISS'
        if '(' in name and name.rstrip().endswith(')'):
            return name[name.rindex('(') + 1:-1]
    return name


def keep_kinds():
    return tuple(k for s in ORDER for k in s['keep_kinds'])


def capture_prefixes():
    return tuple(s['capture_prefix'] for s in ORDER)
