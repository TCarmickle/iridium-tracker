"""Orbcomm channel plan: satellite name -> NORAD id and its two downlink frequencies (Hz).

Data from fbieberly/ORBCOMM-receiver sat_db.py (MIT, see LICENSE.txt), as of 2019:
the satellites the author heard transmitting. Upstream also listed, with no signal
in several passes: FM01-FM26, FM28, FM30-FM32, FM35; and not enough info to know:
FM27, FM29, FM33, FM34, FM36, FM37, FM104-FM107, FM111, FM115, FM119.
"""

ORBCOMM_SATELLITES = {
    'orbcomm fm103': {'norad_id': 40091, 'frequencies': [137250000.0, 137312500.0]},
    'orbcomm fm107': {'norad_id': 40087, 'frequencies': [137250000.0, 137312500.0]},
    'orbcomm fm108': {'norad_id': 41187, 'frequencies': [137460000.0, 137712500.0]},
    'orbcomm fm109': {'norad_id': 40086, 'frequencies': [137250000.0, 137312500.0]},
    'orbcomm fm110': {'norad_id': 41182, 'frequencies': [137287500.0, 137737500.0]},
    'orbcomm fm112': {'norad_id': 41184, 'frequencies': [137662500.0, 137800000.0]},
    'orbcomm fm113': {'norad_id': 41185, 'frequencies': [137662500.0, 137800000.0]},
    'orbcomm fm114': {'norad_id': 41179, 'frequencies': [137287500.0, 137737500.0]},
    'orbcomm fm116': {'norad_id': 41189, 'frequencies': [137662500.0, 137800000.0]},
    'orbcomm fm117': {'norad_id': 41188, 'frequencies': [137460000.0, 137712500.0]},
    'orbcomm fm118': {'norad_id': 41183, 'frequencies': [137287500.0, 137737500.0]},
}


def all_frequencies():
    """Sorted list of every distinct downlink frequency (Hz) in the plan."""
    return sorted({f for s in ORBCOMM_SATELLITES.values() for f in s['frequencies']})


def satellites_on(freq_hz, tol_hz=1000.0):
    """Names of the satellites that use the downlink freq_hz."""
    return [name for name, s in ORBCOMM_SATELLITES.items()
            if any(abs(f - freq_hz) <= tol_hz for f in s['frequencies'])]
