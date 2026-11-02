IRIDIUM TRACKER
===============

A map of the Iridium satellites YOUR antenna hears. The receiver decodes the satellites'
ring alerts (each one carries the satellite's own position) and their Doppler shifts, and
the tracker fits every orbit from those observations alone - no downloaded orbit data is
used for positions. Satellites appear once they have sent you a position fix.

Everything is in this folder: the decoder (gr-iridium + iridium-toolkit), Python, and the app.
Nothing is installed on the computer.


WHAT YOU NEED
-------------
- Windows 10 or 11 (64-bit)
- An RTL-SDR that tunes to 1626 MHz. Tested with the Nooelec SMArTee XTR (E4000 tuner);
  R820T/R828D "V3/V4" dongles also reach 1626 MHz.
- An antenna for L-band (1.6 GHz) with a clear view of the sky. Even a small patch or a
  short telescopic whip outdoors decodes ring alerts.


ONE-TIME SETUP: THE USB DRIVER (needs administrator rights)
-----------------------------------------------------------
Windows needs the WinUSB driver for the SDR. This cannot be bundled.
1. Plug in the SDR.
2. Download Zadig from https://zadig.akeo.ie and run it.
3. Options menu > List All Devices.
4. Pick "Bulk-In, Interface (Interface 0)" (or "RTL2838UHIDIR").
5. Make sure the driver on the right says WinUSB, then click "Replace Driver" (or "Install Driver").
You only do this once per computer.


STARTING
--------
1. Keep this whole folder somewhere you can write to (Desktop, Documents...),
   NOT in "Program Files". Captures and the catalog are saved inside it.
2. Double-click "Iridium Tracker.bat".
3. The first time, the map asks you to CLICK YOUR ANTENNA'S LOCATION. Zoom in with the mouse
   wheel (drag to move) and click your house; the height there is looked up automatically
   (the point is sent to open-meteo.com). "Type coordinates..." lets you enter it instead.
   To change it later, click the location button (pin) at the top right of the window.
   It is saved in app\settings.json.
4. Press the green "Start receiver" button at the top. Close SDR++, SatDump or any other
   program using the SDR first - only one program can use it at a time.
5. Press "Stop receiver" when you're done. The capture is then decoded into the catalog
   automatically, and the satellites you heard get their orbits.

Ring alerts with positions come in when a satellite passes over or near you; give it at
least 30-60 minutes outdoors. In the list on the right, click a satellite to see every
position it sent you and how good its orbit is.


FILES IT WRITES (all in the app folder)
---------------------------------------
output-<date-time>.bits    raw bursts from the decoder
output-<date-time>.live    decoded frames (written while receiving)
output-<date-time>.parsed  decoded frames (written after you stop)
output-<date-time>.start   the exact time the capture started
catalog-data.json          everything heard so far (satellites, planes, positions)
catalog.txt                the same as a readable report
iridium-names.tle          public list of Iridium satellites, refreshed from CelesTrak every
                           couple of days - used ONLY to put names on satellites
tiles\                     cached map pictures from OpenStreetMap


PRIVACY AND THE LAW
-------------------
The tracker only uses technical frames (ring alerts, time & location, Doppler). It does not
decode or play voice or messages. Iridium phone calls and messages are private
communications; in many countries (including the US) listening to their content or sharing
it is illegal. Don't.
If you share your capture files or catalog, remember they contain your location.


TROUBLESHOOTING
---------------
"the SDR could not be opened"   The SDR is unplugged, the Zadig driver isn't installed, or
                                another program (SDR++, SatDump...) is using it.
"decoder used over 2 GB"        Too many false bursts - the threshold is too low or there is
                                strong interference. It stops itself to protect the computer.
Nothing decoded                 Check the antenna is outside with sky view; Iridium is at
                                1616-1626.5 MHz.
Errors                          See app\tracker-error.log and the output-*.log files.


CREDITS AND LICENSES
--------------------
This package contains open-source software; see the LICENSES folder.
- gr-iridium (iridium-extractor)  GPL-3.0     https://github.com/muccc/gr-iridium
- iridium-toolkit (parser)        BSD-2-Clause https://github.com/muccc/iridium-toolkit
- GNU Radio, gr-osmosdr           GPL-3.0     https://www.gnuradio.org  https://osmocom.org
- librtlsdr                       GPL-2.0     https://osmocom.org/projects/rtl-sdr
- Python, NumPy, SciPy, requests, PyQt5 (GPL-3.0) and their dependencies - full list and
  license texts in LICENSES\third-party
- Map data (c) OpenStreetMap contributors, https://www.openstreetmap.org/copyright
- Satellite names: CelesTrak, https://celestrak.org
The tracker itself (the files in app\ other than iridium-toolkit) is free software under the
GNU General Public License v3 (LICENSES\GPL-3.0.txt). The source code is the .py files in app\.
