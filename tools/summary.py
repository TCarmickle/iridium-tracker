"""Readable summary of an iridium-parser .parsed file.

Usage: python summary.py output-YYYYMMDD-HHMMSS.parsed
"""
import collections
import datetime
import re
import sys

path = sys.argv[1]
# burst times count from when the decoder started reading samples, which is ~40 s after
# the file is created; the receiver measures that moment and saves it in a .start file.
# Without one, fall back to the file name time (printed times will run ~40 s early).
m = re.search(r'(\d{8}-\d{6})', path)
start = datetime.datetime.strptime(m.group(1), '%Y%m%d-%H%M%S') if m else None
start_file = re.sub(r'\.(parsed|bits)$', '', path) + '.start'
try:
    text = open(start_file).read().strip()
    start = datetime.datetime.strptime(text.rstrip('Z'), '%Y-%m-%d %H:%M:%S.%f')
    if text.endswith('Z'):   # UTC (newer runs) - converted to local time per burst, so a DST change mid-run is handled
        start = start.replace(tzinfo=datetime.timezone.utc)
except (OSError, ValueError):
    if start:
        print('note: no %s - times may be ~40 s early' % start_file)


def when(ms):
    if start is None:
        return '%7.1f s' % (ms / 1000)
    t = start + datetime.timedelta(milliseconds=ms)
    if t.tzinfo:
        t = t.astimezone()
    return t.strftime('%I:%M:%S %p').lstrip('0')


frames = []
for line in open(path, errors='replace'):
    f = line.split()
    if len(f) < 6 or not f[0].endswith(':') or f[0] == 'ERR:':
        continue
    try:
        ms, freq = float(f[2]), int(f[3])
        snr = float(f[5].split('|')[2]) if '|' in f[5] else 0.0
    except ValueError:
        continue
    frames.append((f[0][:-1], ms, freq, snr, line))

print()
print('FRAME TYPES  (RAW = heard but too damaged to read)')
for kind, n in collections.Counter(k for k, *_ in frames).most_common():
    print('  %-4s %5d' % (kind, n))

# ITL frames name the satellite: plane P1-P6, satellite S1-S11 on that plane
sats = collections.defaultdict(list)
for kind, ms, freq, snr, line in frames:
    if kind == 'ITL':
        m = re.search(r' (P\d) (S\d+) ', line)
        if m:
            sats['plane %s, satellite %s' % (m.group(1)[1:], m.group(2)[1:])].append((ms, freq, snr))

print()
print('SATELLITES HEARD  (from ITL time & location frames)')
if not sats:
    print('  none identified in this capture')
for name, hits in sats.items():
    hits.sort()
    print('  %s: %d frames, %s to %s, best signal %.1f dB'
          % (name, len(hits), when(hits[0][0]), when(hits[-1][0]), max(h[2] for h in hits)))
    print('      time          frequency       signal   (frequency drops as it passes = Doppler shift)')
    for ms, freq, snr in hits:
        print('      %-12s  %.4f MHz   %5.1f dB' % (when(ms), freq / 1e6, snr))

print()
print('RING ALERTS  (IRA - can carry the satellite position)')
shown = 0
for kind, ms, freq, snr, line in frames:
    if kind != 'IRA':
        continue
    shown += 1
    m = re.search(r'sat:(\d+) beam:(\d+).*?pos=\(([-+\d.]+)/([-+\d.]+)\) alt=(-?\d+)', line)
    if m and int(m.group(5)) > 100:
        print('  %-12s sat %s beam %s  lat %s lon %s  altitude %s km  (%.1f dB)'
              % (when(ms), m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), snr))
    else:
        why = 'cut short - only part of the burst was received' if 'TRUNCATED' in line else 'position fields empty'
        print('  %-12s decoded, but no position: %s  (%.1f dB)' % (when(ms), why, snr))
if not shown:
    print('  none in this capture')
