"""Test only: how close are our self-made orbits to the public orbit data (SatDump TLEs)?
The tracker never uses the public data; this just measures our accuracy.
Usage: python orbit_check.py
"""
import datetime
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))
import catalog    # noqa: E402
import iridium_tracker as it   # noqa: E402

UTC = datetime.timezone.utc
d = it.Data()
ref = {s.name: s for s in catalog.load_tles()}
print('%-12s %-26s %8s %8s   distance from public orbit (km) at time after last observation'
      % ('satellite', 'our data', 'fit rms', 'sigma'))
print('%-12s %-26s %8s %8s   %8s %8s %8s %8s %8s' % ('', '', 'km', 'now', 'at obs', '+1 h', '+6 h', '+24 h', '+3 d'))
for name in sorted(d.orbits, key=lambda n: int(''.join(c for c in n if c.isdigit()) or 0)):
    o = d.orbits[name]
    if name not in ref:
        continue
    i = o.info
    row = []
    for dt in (0, 3600, 6 * 3600, 86400, 3 * 86400):
        t = i['last_obs'] + dt
        ours = o.ecef(t)[0]
        X, Y, Z = it_ref = ref[name].ecef(datetime.datetime.fromtimestamp(t, UTC))
        row.append(np.linalg.norm(ours - np.array(it_ref)))
    print('%-12s %2d fixes %d passes %2d dopp  %8.1f %8.0f   %8.0f %8.0f %8.0f %8.0f %8.0f'
          % (name, i['fixes'], i['passes'], i['doppler_groups'], i['fix_rms_km'], o.sigma_km(__import__('time').time())[0], *row))
print()
for name, why in sorted(d.no_orbit.items()):
    print('no orbit: %-12s %s' % (name, why))
