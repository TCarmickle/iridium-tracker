# Iridium Tracker

A Windows app that maps the Iridium satellites **your own antenna** hears, using an RTL-SDR.

The receiver decodes the satellites' *ring alerts* (each carries the satellite's own position)
and measures their Doppler shift. The tracker then fits every satellite's orbit **from those
observations alone**: no downloaded orbit data is used for positions or tracks. Satellites appear
once they have sent you a position fix, and fade as the time since you last heard them grows.

## Features
- **Start / stop the receiver** from the app; captures are decoded into a running catalog automatically
- **Live map** (dark theme, OpenStreetMap) with measured fixes, predicted positions and tracks,
  coverage footprints, day/night shading, and the six orbital planes
- **Orbits from your own data** (`orbitfit.py`): circular orbit + J2 drift fitted to position
  fixes and Doppler curves, with an uncertainty that grows with time since the last observation
- **Timeline** to scrub, play and replay past captures
- **Per-satellite details**: every ring-alert position it sent you, how good its orbit is, next pass
- **Iridium Console** (`Iridium Console.bat`): a burst-by-burst log of every decoded frame, ring-alert
  positions and decoder status, alongside the tracker or receiving on its own
- **Click the map to set your location**; the terrain height is looked up automatically
- **Stays small**: raw recordings are cleaned up automatically (7 days / 1 GB by default), long-term
  storage is ~1-2% of a capture, the map cache is capped, and receiving stops before the disk fills
- **Self-contained**: one folder with its own Python, GNU Radio and decoder - nothing to install
  except the SDR's USB driver

## What you need
- Windows 10/11 (64-bit)
- An RTL-SDR that reaches 1626 MHz (tested: Nooelec SMArTee XTR, E4000 tuner)
- An L-band antenna (1.6 GHz) with a view of the sky - even a short whip outdoors decodes ring alerts
- The WinUSB driver for the SDR, installed once with [Zadig](https://zadig.akeo.ie)

## Getting started
1. Build the package (see [BUILDING.md](BUILDING.md)) or get `IridiumTracker.zip` from someone who did,
   and unzip it somewhere you can write to (not *Program Files*).
2. Double-click **`Iridium Tracker.bat`**.
3. Click your antenna's location on the map (zoom in first for a precise spot).
4. Press **Start receiver**. Give it 30-60 minutes outdoors; press **Stop receiver** when done.

The full user guide is [README.txt](README.txt).

## How it works
| Step | Software |
|---|---|
| Find Iridium bursts in the SDR signal | [gr-iridium](https://github.com/muccc/gr-iridium) `iridium-extractor` (GNU Radio) |
| Decode bursts into frames | [iridium-toolkit](https://github.com/muccc/iridium-toolkit) `iridium-parser` |
| Catalog satellites, planes and positions | `app/catalog.py` |
| Fit orbits from your fixes + Doppler | `app/orbitfit.py` |
| Map, timeline, receiver control | `app/iridium_tracker.py`, `app/receiver.py` |

Satellite **names** (e.g. "IRIDIUM 120") come from CelesTrak's public list and are used only as
labels; positions never do.

## Privacy and the law
The tracker only uses technical frames (ring alerts, time & location, Doppler). It does not decode
or play voice or messages: Iridium calls and messages are private communications, and in many
countries (including the US) intercepting or sharing their content is illegal.
Your location, captures and catalog stay in the `data` folder and are excluded from this repository.
Raw recordings (which contain every burst heard, including other people's voice and message bursts)
are deleted automatically after 7 days by default; only ring alerts and time & location frames are
kept long-term, with phone paging IDs removed.

## License
The tracker is free software under the [GNU GPL v3](LICENSE). It bundles gr-iridium and GNU Radio
(GPL-3.0), iridium-toolkit (BSD-2-Clause), PyQt5 (GPL-3.0) and other open-source packages; see
[LICENSES](LICENSES). Map data © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors.
Terrain heights from [Open-Meteo](https://open-meteo.com). Satellite names from [CelesTrak](https://celestrak.org).
