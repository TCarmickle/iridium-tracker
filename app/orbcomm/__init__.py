"""Orbcomm downlink decoding (137.2-137.8 MHz, SDPSK 4800 bit/s), numpy + scipy only.

Adapted from fbieberly/ORBCOMM-receiver (MIT, LICENSE.txt; rrcosfilter from CommPy,
BSD-3, THIRD_PARTY_LICENSE.txt). Each module's header says what was changed.

  dec = OrbcommDecoder(sat_freq, center_freq, sample_rate)   one per channel
  packets = dec.decode_samples_to_packets(chunk, timestamp=t)  complex64 chunk ->
      list of dicts, checksum-good packets only. Every packet has
        packet_type   'Sync', 'Ephemeris', 'Message', 'Fill', ... or 'Unrecognized'
        hex           the packet bytes as upper-case hex
        carrier_offset_hz  measured carrier minus channel_hz (Doppler + SDR error)
        channel_hz    the nominal downlink frequency
        stream_time   seconds since the decoder's start/reset
        time          unix time of the packet if timestamps were given, else None
      Sync and Ephemeris packets add sat_id (hex string); Ephemeris adds
        pos_ecef_km (x, y, z), vel_ecef_kms (vx, vy, vz), gps_week, gps_tow,
        unix_time (UTC of the ephemeris), lat, lon (deg), alt_km
  dec.carrier_offset, dec.carrier_snr_db, dec.locked, dec.good_packets,
  dec.bad_packets, dec.resets; dec.clean() forgets everything for a new pass.
"""
from .channels import ORBCOMM_SATELLITES, all_frequencies, satellites_on
from .decoder import OrbcommDecoder, RealtimeDecoder
from .helpers import fletcher_checksum, ecef_to_lla
from .packets import packet_dict, parse_packet, decode_ephemeris, gps_to_unix

__all__ = ['OrbcommDecoder', 'RealtimeDecoder', 'ORBCOMM_SATELLITES', 'all_frequencies',
           'satellites_on', 'fletcher_checksum', 'ecef_to_lla', 'packet_dict',
           'parse_packet', 'decode_ephemeris', 'gps_to_unix']
