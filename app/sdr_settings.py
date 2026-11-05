"""SDR source settings (the device the receiver uses and how it is tuned), saved in settings.json.

The decoders open the device through gr-osmosdr, so any device gr-osmosdr supports works: RTL-SDR,
RTL-TCP over the network, HackRF, Airspy, bladeRF, SoapySDR... One device receives one system at a
time: 'system' says which (Iridium at 1.6 GHz, or Orbcomm at 137 MHz - each needs its own antenna).
"""
import json
import os
import subprocess
import sys

import paths
import systems

DEFAULTS = {
    'system': 'iridium',        # which satellite system the receiver listens to (systems.py)
    'device': 'rtl=0',          # gr-osmosdr device arguments
    'label': 'RTL-SDR #0',
    'sample_rate': 2000000,
    'center_mhz': 0.0,          # 0 = automatic (covers the top of the Iridium band, where ring alerts are)
    'gain': 40.0,
    'auto_gain': False,
    'ppm': 0.0,
    'bias_t': False,
    'offset_tune': False,       # E4000 tuners: avoids the centre spike (SDR++: "Offset Tuning")
    'iq_correction': False,     # gr-osmosdr's automatic IQ balance mirrors every burst (~6 dB below it) - off
    'dc_correction': True,
    'threshold_db': 16.0,       # burst detection, dB over the noise
    'extra_args': '',           # anything else for the device string, e.g. "offset_tune=1"
    'orbcomm_gain': 35.0,       # each band has its own gain: a different antenna and signals
    'orbcomm_auto_gain': False,
    'ham2m_gain': 30.0,
    'ham2m_auto_gain': False,
    'ham70_gain': 35.0,
    'ham70_auto_gain': False,
    'gnss1_gain': 40.0,         # GNSS: an active antenna's amplifier already lifts the signal
    'gnss1_auto_gain': False,
    'glo1_gain': 40.0,
    'glo1_auto_gain': False,
    'meteor_gain': 40.0,        # Meteor-M pictures (a whip: its signal is weak, low in the noise)
    'meteor_auto_gain': False,
}
# the decoder needs sample_rate / decimation to be a multiple of 250 kHz
RATES = [2000000, 2250000, 2500000, 3000000, 4000000, 5000000, 6000000, 8000000, 10000000, 12500000, 20000000]
MIN_THRESHOLD = 14.0            # lower floods the decoder with false bursts and can run the PC out of memory
BAND_LO, BAND_HI = 1616.0e6, 1626.5e6       # Iridium downlink
# Orbcomm: the decoder needs a rate that is a whole multiple of 9600 S/s (2 samples per 4800-baud
# symbol); 1.2288 MS/s centred on 137.5 MHz covers all the downlink channels (137.2-137.8 MHz)
ORBCOMM_RATE, ORBCOMM_CENTER = 1228800, 137500000
FFT_SIZE = 2048


def load():
    try:
        s = json.load(open(os.path.join(paths.DATA, 'settings.json'))).get('sdr', {})
    except (OSError, ValueError, AttributeError):
        s = {}
    out = dict(DEFAULTS)
    for k, v in s.items():
        if k in DEFAULTS:
            try:
                out[k] = type(DEFAULTS[k])(v)
            except (TypeError, ValueError):
                pass
    out['threshold_db'] = max(MIN_THRESHOLD, out['threshold_db'])
    return out


def save(s):
    path = os.path.join(paths.DATA, 'settings.json')
    try:
        allset = json.load(open(path))
    except (OSError, ValueError):
        allset = {}
    allset['sdr'] = {k: s[k] for k in DEFAULTS}
    os.makedirs(paths.DATA, exist_ok=True)
    json.dump(allset, open(path, 'w'), indent=1)


def is_orbcomm(s):
    return s.get('system') == 'orbcomm'


def fixed_tuning(s):
    """every band but Iridium has its rate and centre set by its decoders (systems.py)"""
    return s.get('system', 'iridium') != 'iridium'


def sample_rate(s):
    return systems.get(s['system'])['rate'] if fixed_tuning(s) else s['sample_rate']


def gain(s):
    """(gain dB, automatic?) for the band being received"""
    key = s.get('system', 'iridium')
    return (s['gain'], s['auto_gain']) if key == 'iridium' else (s[key + '_gain'], s[key + '_auto_gain'])


def center_hz(s):
    """the tuning frequency. Iridium: as set, or automatic - the widest slice of the band the rate
    allows, keeping the top (the ring-alert and messaging channels at 1626.0-1626.5 MHz) inside.
    Other bands: fixed (Orbcomm 137.5 MHz, the middle of its channels; the ham bands, see systems.py)."""
    if fixed_tuning(s):
        return systems.get(s['system'])['center_hz']
    if s['center_mhz'] > 0:
        return int(round(s['center_mhz'] * 1e6))
    half = s['sample_rate'] / 2
    return int(max(BAND_HI - half, (BAND_LO + BAND_HI) / 2))


def decimation(rate):
    """smallest decimation that leaves a multiple of 250 kHz (1 for all RATES)"""
    for d in range(1, 17):
        if (rate / d) % 250000 == 0:
            return d
    raise ValueError('%d S/s cannot be used: it must divide into a multiple of 250 kHz' % rate)


def device_args(s):
    parts = [s['device'].strip()]
    if s['bias_t'] and 'bias=' not in s['device']:
        parts.append('bias=1')
    if s['offset_tune'] and 'offset_tune=' not in s['device']:
        parts.append('offset_tune=1')
    if s['extra_args'].strip():
        parts.append(s['extra_args'].strip().strip(','))
    return ','.join(p for p in parts if p)


def write_conf(s, path):
    """the config file iridium-extractor reads"""
    lines = ['[osmosdr-source]',
             'device_args=%s' % device_args(s),
             'sample_rate=%d' % s['sample_rate'],
             'center_freq=%d' % center_hz(s)]
    if not s['auto_gain']:
        lines.append('gain=%d' % round(s['gain']))
    lines += ['', '[demodulator]', 'decimation=%d' % decimation(s['sample_rate'])]
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    return path


# ---- finding devices (in a separate process: loading the SDR drivers is slow and noisy)
_FIND = r'''
import osmosdr
for d in osmosdr.device.find():
    print('DEV ' + str(d.to_string()))
'''
_EXAMPLES = ('/path/to/your/file', 'redpitaya=192.168.1.100', 'rtl_tcp=localhost:1234')


def _parse(dev):
    """gr-osmosdr device string -> (label, args without the label)"""
    label, parts = None, []
    for p in dev.split(','):
        if p.startswith('label='):
            label = p[6:].strip("'")
        else:
            parts.append(p)
    return label, ','.join(parts)


def discover(python=None, timeout=30):
    """[(label, device args)] of the devices plugged in now"""
    python = python or os.path.join(os.path.dirname(sys.executable), 'python.exe')
    try:
        r = subprocess.run([python, '-c', _FIND], capture_output=True, text=True, timeout=timeout,
                           creationflags=0x08000000)
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = []
    for line in r.stdout.splitlines():
        if not line.startswith('DEV '):
            continue
        label, args = _parse(line[4:])
        if any(x in line for x in _EXAMPLES):
            continue        # gr-osmosdr's examples of network/file sources, not real devices
        found.append((label or args, args))
    return found
