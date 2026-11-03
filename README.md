# Iridium Tracker

A Windows app that maps the Iridium, Orbcomm, GPS and Galileo satellites **your own antenna** hears - and
logs the ISS and ham CubeSats it hears - using an RTL-SDR.

The receiver decodes the satellites' *ring alerts* (each carries the satellite's own position)
and measures their Doppler shift. The tracker then fits every satellite's orbit **from those
observations alone**: no downloaded orbit data is used for positions or tracks. Satellites appear
once they have sent you a position fix, and fade as the time since you last heard them grows.

Orbcomm satellites (137 MHz) work the same way: each one broadcasts its own position (ephemeris
packets), so their orbits come from your receptions too. So do GPS and Galileo: with a GPS antenna,
each satellite's own broadcast orbit is decoded from its signal. One SDR receives one system at a time;
everything received is shown together, with a **Systems** filter.

## Features
- **Several systems: Iridium (1.6 GHz), Orbcomm (137 MHz), ISS & ham satellites (2 m / 70 cm)** - pick which
  band to receive next to Start (and swap the antenna: ~4.6 cm / ~52 cm / ~49 cm / ~16.5 cm); the map, list,
  globe and sky plot show them all, filterable
- **ISS and CubeSats**: the whole 2 m / 70 cm slice is watched; each signal's Doppler shift is measured and the
  [gr-satellites](https://github.com/daniestevez/gr-satellites) decoders of nearby satellites are tried; only
  unambiguous frames name a satellite (ISS: packets it digipeated). Frame contents are never stored
- **GNSS: GPS + Galileo** (1575.42 MHz, active GPS antenna straight into the SDR): [PocketSDR](https://github.com/tomojitakasu/PocketSDR)
  tracks the satellites under the noise; their Doppler curves are listed, each satellite's own broadcast orbit
  (ephemeris) puts it on the map and globe, and with four of them the app works out your own position as a
  check of the whole chain
- **Sound modes**: NFM, WFM, AM, USB, LSB, CW with a bandwidth, defaulting per band
- **Start / stop the receiver** from the app; captures are decoded into a running catalog automatically
- **Live map** (dark theme, OpenStreetMap) with measured fixes, predicted positions and tracks,
  coverage footprints, day/night shading, and the six orbital planes
- **Orbits from your own data** (`orbitfit.py`): circular orbit + J2 drift fitted to position
  fixes and Doppler curves, with an uncertainty that grows with time since the last observation
- **Timeline** to scrub, play and replay past captures
- **Per-satellite details**: every ring-alert position it sent you, how good its orbit is, next pass
- **Spectrum and waterfall** like SDR++: the live spectrum of exactly what the decoder hears, with
  zoom, hover readout, auto range and peak hold - and **sound**: hear the raw radio (static and burst
  clicks) for the whole band or one channel you click, like SDR++
- **Satellite info**: click any satellite for its launch and hardware facts, its Doppler curves (plotted against the prediction they were matched to), every pass you heard, and our orbit vs the public catalog
- **3D globe**: satellites, orbital-plane rings, tracks and your fixes in 3D around the Earth (top view, side view, or standing at your location looking up), following the timeline
- **Sky plot**: where in your sky your antenna hears satellites (position fixes coloured by signal),
  to find blind spots and compare antenna placements
- **Activity lights**: flash green for every decoded message and red for every damaged burst, and
  by each satellite in the list when that satellite is heard
- **Choose your SDR** like SDR++'s source menu: any gr-osmosdr device (RTL-SDR, RTL-TCP, HackRF,
  Airspy, bladeRF, SoapySDR...), sample rate, frequency, gain, PPM, bias-T, IQ/DC correction
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
- Optional: a 137 MHz whip or V-dipole (Orbcomm), a 2 m / 70 cm whip (ISS, CubeSats), an active GPS
  antenna and an SDR with a bias-T (GPS / Galileo)
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
| Receive and decode ISS / ham satellites (2 m, 70 cm) | `app/ham_rx.py` (GNU Radio) + [gr-satellites](https://github.com/daniestevez/gr-satellites) |
| Receive and decode Orbcomm (all 8 channels at once) | `app/orbcomm_rx.py` (GNU Radio) + `app/orbcomm/`, adapted from [ORBCOMM-receiver](https://github.com/fbieberly/ORBCOMM-receiver) |
| Track GPS / Galileo and decode their broadcast orbits | `app/gnss_rx.py` (GNU Radio, 3-minute IQ pieces) + [PocketSDR](https://github.com/tomojitakasu/PocketSDR) `pocket_trk`; orbits read from its RTCM 3 output (`app/rtcm3.py`) and computed in `app/gnss.py` |
| Catalog satellites, planes and positions | `app/catalog.py` |
| Fit orbits from your fixes + Doppler | `app/orbitfit.py` |
| Map, timeline, receiver control | `app/iridium_tracker.py`, `app/receiver.py` |

Satellite **names** (e.g. "IRIDIUM 120") come from CelesTrak's public list and are used only as
labels; positions never do.

## Privacy and the law
The tracker only uses technical frames (ring alerts, time & location, Orbcomm sync and ephemeris,
Doppler). It does not decode or play voice or messages (Orbcomm message packets are only counted,
never stored): Iridium calls and messages are private communications, and in many
countries (including the US) intercepting or sharing their content is illegal.
Your location, captures and catalog stay in the `data` folder and are excluded from this repository.
Raw recordings (which contain every burst heard, including other people's voice and message bursts)
are deleted automatically after 7 days by default; only ring alerts and time & location frames are
kept long-term, with phone paging IDs removed.

## License
The tracker is free software under the [GNU GPL v3](LICENSE). It bundles gr-iridium and GNU Radio
(GPL-3.0), iridium-toolkit (BSD-2-Clause), gr-satellites (GPL-3.0), [PocketSDR](https://github.com/tomojitakasu/PocketSDR)
(BSD-2-Clause, T. Takasu; GNSS tracking, `app/pocketsdr`), an Orbcomm decoder adapted from ORBCOMM-receiver (MIT, with
CommPy's BSD-3-Clause RRC filter; notices in `app/orbcomm`), PyQt5 (GPL-3.0) and other open-source packages; see
[LICENSES](LICENSES). Map data © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors.
Terrain heights from [Open-Meteo](https://open-meteo.com). Satellite names from [CelesTrak](https://celestrak.org).
