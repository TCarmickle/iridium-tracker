"""DSP and packet helpers for the Orbcomm decoder.

Vendored from fbieberly/ORBCOMM-receiver helpers.py (MIT, see LICENSE.txt);
rrcosfilter is from CommPy (BSD-3, see THIRD_PARTY_LICENSE.txt).
Changes: complex_mix keeps a real phase wrapped to 2*pi (the original carried a
growing imaginary phase); TLE/plot/LPF helpers the decoder does not use were dropped.
"""
import numpy as np


def complex_mix(arr, freq_shift, sample_rate, phase=0.0):
    """Shift arr by freq_shift Hz. Returns (shifted, phase after the last sample),
    so consecutive chunks stay phase coherent."""
    n = np.arange(len(arr) + 1, dtype=np.float64)
    ph = 2 * np.pi * freq_shift / sample_rate * n + phase
    shifted = arr * np.exp(1j * ph[:-1]).astype(np.complex64)
    return shifted, float(np.mod(ph[-1], 2 * np.pi))


# From: https://github.com/veeresht/CommPy/blob/master/commpy/filters.py
def rrcosfilter(N, alpha, Ts, Fs):
    """Root raised cosine FIR of N taps, roll-off alpha, symbol period Ts, rate Fs.
    Returns (time_idx, h_rrc)."""
    N = int(N)
    T_delta = 1 / float(Fs)
    time_idx = ((np.arange(N) - N / 2)) * T_delta
    sample_num = np.arange(N)
    h_rrc = np.zeros(N, dtype=float)

    for x in sample_num:
        t = (x - N / 2) * T_delta
        if t == 0.0:
            h_rrc[x] = 1.0 - alpha + (4 * alpha / np.pi)
        elif alpha != 0 and t == Ts / (4 * alpha):
            h_rrc[x] = (alpha / np.sqrt(2)) * (((1 + 2 / np.pi) *
                        (np.sin(np.pi / (4 * alpha)))) + ((1 - 2 / np.pi) * (np.cos(np.pi / (4 * alpha)))))
        elif alpha != 0 and t == -Ts / (4 * alpha):
            h_rrc[x] = (alpha / np.sqrt(2)) * (((1 + 2 / np.pi) *
                        (np.sin(np.pi / (4 * alpha)))) + ((1 - 2 / np.pi) * (np.cos(np.pi / (4 * alpha)))))
        else:
            h_rrc[x] = (np.sin(np.pi * t * (1 - alpha) / Ts) +
                        4 * alpha * (t / Ts) * np.cos(np.pi * t * (1 + alpha) / Ts)) / \
                       (np.pi * t * (1 - (4 * alpha * t / Ts) * (4 * alpha * t / Ts)) / Ts)

    return time_idx, h_rrc


# Derived from wikipedia: https://en.wikipedia.org/wiki/Fletcher%27s_checksum
def fletcher_checksum(hex_data_str):
    """Fletcher-16 over a hex string; '0000' means the packet is good."""
    sum1 = 0
    sum2 = 0
    if len(hex_data_str) % 2 == 1:
        hex_data_str += '0'
    for xx in range(0, len(hex_data_str) - 1, 2):
        val = int(hex_data_str[xx:xx + 2], 16)
        sum1 = (sum1 + val) % 256
        sum2 = (sum1 + sum2) % 256
    return '{:02X}{:02X}'.format(sum2, sum1)


def ecef_to_lla(x_ecef, y_ecef, z_ecef):
    """WGS84 ECEF (m) -> (lat deg, lon deg, height m).
    From: EPSG guidance note 7-2, page 97."""
    f = 1.0 / 298.257223563
    wgs84_a = 6378137.0  # m
    wgs84_b = wgs84_a * (1. - f)
    wgs84_e_sqrd = 1. - (wgs84_b ** 2) / (wgs84_a ** 2)

    wgs84_eps = wgs84_e_sqrd / (1 - wgs84_e_sqrd)
    wgs84_p = np.sqrt(x_ecef ** 2 + y_ecef ** 2)
    wgs84_q = np.arctan2((z_ecef * wgs84_a), (wgs84_p * wgs84_b))

    phi = np.arctan2((z_ecef + wgs84_eps * wgs84_b * np.sin(wgs84_q) ** 3),
                     (wgs84_p - wgs84_e_sqrd * wgs84_a * np.cos(wgs84_q) ** 3))
    lamd = np.arctan2(y_ecef, x_ecef)

    wgs84_v = (wgs84_a / np.sqrt(1 - wgs84_e_sqrd * np.sin(phi) ** 2))
    h = (wgs84_p / np.cos(phi)) - wgs84_v

    return float(np.degrees(phi)), float(np.degrees(lamd)), float(h)
