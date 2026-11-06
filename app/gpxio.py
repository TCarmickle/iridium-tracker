"""GPX 1.1 (the format handhelds, BaseCamp, Google Earth and QGIS use): writing the tracker's position fixes as tracks
and its reference points as waypoints, and reading tracks / waypoints from a GPX file (a Garmin's, for example).

Heights: GPX <ele> is height above sea level. The tracker's fixes are heights above the WGS84 ellipsoid (GPS's own
model of the Earth); with the local geoid separation known (geoid.py) they are written as sea-level heights,
otherwise as they are (the file's description says which)."""
import datetime
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

NS = 'http://www.topografix.com/GPX/1/1'


def _iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def write(path, tracks=(), waypoints=(), geoid_sep=None, creator='Satellite Tracker'):
    """tracks: [(name, [{'t','lat','lon','height_m','nsat'?,'hdop'?}])]; waypoints: [{'name','lat','lon',
    'height_m'?,'t'?,'desc'?}]; geoid_sep: ellipsoid minus sea level (m), to write sea-level heights"""
    def ele(h):
        if h is None:
            return ''
        return '<ele>%.2f</ele>' % (h - geoid_sep if geoid_sep is not None else h)

    note = ('heights above sea level (geoid separation %+.1f m)' % geoid_sep if geoid_sep is not None
            else 'heights above the WGS84 ellipsoid (GPS model height, not sea level)')
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx version="1.1" creator="%s" xmlns="%s">' % (escape(creator), NS),
           '<metadata><desc>%s</desc><time>%s</time></metadata>' % (escape(note),
                                                                    _iso(datetime.datetime.now().timestamp()))]
    for w in waypoints:
        out.append('<wpt lat="%.8f" lon="%.8f">%s%s<name>%s</name>%s</wpt>' % (
            w['lat'], w['lon'], ele(w.get('height_m')), '<time>%s</time>' % _iso(w['t']) if w.get('t') else '',
            escape(w.get('name', '')), '<desc>%s</desc>' % escape(w['desc']) if w.get('desc') else ''))
    for name, fixes in tracks:
        out.append('<trk><name>%s</name><trkseg>' % escape(name))
        for f in sorted(fixes, key=lambda f: f['t']):
            out.append('<trkpt lat="%.8f" lon="%.8f">%s<time>%s</time>%s%s</trkpt>' % (
                f['lat'], f['lon'], ele(f.get('height_m')), _iso(f['t']),
                '<sat>%d</sat>' % f['nsat'] if f.get('nsat') is not None else '',
                '<hdop>%.1f</hdop>' % f['hdop'] if f.get('hdop') is not None else ''))
        out.append('</trkseg></trk>')
    out.append('</gpx>')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


def read(path):
    """-> (tracks [(name, [(t, lat, lon, ele)])], waypoints [{'name','lat','lon','ele','t'}]); ele is what the
    file says (a Garmin's: sea level, barometer-corrected)"""
    root = ET.parse(path).getroot()
    ns = root.tag.split('}')[0] + '}' if root.tag.startswith('{') else ''

    def t_of(el):
        x = el.find(ns + 'time')
        if x is None or not x.text:
            return None
        return datetime.datetime.fromisoformat(x.text.strip().replace('Z', '+00:00')).timestamp()

    def ele_of(el):
        x = el.find(ns + 'ele')
        try:
            return float(x.text) if x is not None else None
        except (TypeError, ValueError):
            return None

    tracks = []
    for trk in root.iter(ns + 'trk'):
        nm = trk.find(ns + 'name')
        pts = []
        for p in trk.iter(ns + 'trkpt'):
            t = t_of(p)
            if t is not None:
                pts.append((t, float(p.get('lat')), float(p.get('lon')), ele_of(p)))
        tracks.append((nm.text if nm is not None else '', sorted(pts)))
    wpts = []
    for w in root.iter(ns + 'wpt'):
        nm = w.find(ns + 'name')
        wpts.append({'name': nm.text if nm is not None and nm.text else '', 'lat': float(w.get('lat')),
                     'lon': float(w.get('lon')), 'ele': ele_of(w), 't': t_of(w)})
    return tracks, wpts
