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


CHOOSING YOUR SDR (Source button)
---------------------------------
The "Source" button next to Start receiver works like SDR++'s source menu:
- Device: the SDRs found on this PC (Refresh to look again), an RTL-SDR shared over the network
  (RTL-TCP: rtl_tcp=host:port), or any gr-osmosdr device string (hackrf=0, airspy=0, rtl=1,
  soapy=0,driver=...).
- Sample rate: RTL-SDRs 2.0-2.5 MS/s. Wider SDRs (HackRF, Airspy, bladeRF) can take up to
  10-20 MS/s and cover the whole Iridium band (1616-1626.5 MHz).
- Frequency: Automatic keeps the ring-alert channels (1626.0-1626.5 MHz) in view.
- Gain (or Auto), PPM correction, Bias-T (powers an active antenna or LNA through the coax),
  offset tuning and DC correction. IQ correction is off by default: gr-osmosdr's automatic IQ
  balance creates a mirror image of every burst, which can't be decoded.
- Burst threshold: how far over the noise a burst must be. 14 dB is the minimum - lower floods
  the decoder with false bursts and can run the PC out of memory.
Changes apply the next time you start the receiver.


ACTIVITY LIGHTS
---------------
Next to the receiver status, two small lights flash for every burst: green (top) = a message
was decoded, red (bottom) = a damaged burst that couldn't be decoded. In the satellite list, the
light at the left of each satellite flashes green when a frame from that satellite comes in
(identified by its network ID or plane/slot). Damaged bursts can't be traced to a satellite -
the part saying which satellite sent them is what couldn't be decoded.


SPECTRUM AND WATERFALL
----------------------
Under the map, while receiving: the live spectrum (top) and waterfall (bottom) of exactly what
the decoder hears - each Iridium burst shows up as a short dash in the waterfall. The ring-alert
and messaging channels are shaded green, the phone channels blue.
- Mouse wheel over the waterfall zooms in on a frequency (both views), drag moves along,
  double-click shows everything. Mouse wheel over the spectrum trace steps the tuned sound
  frequency 1 kHz per click (10 kHz with Shift).
- Hover to read the frequency and level (and how long ago, in the waterfall).
- Auto range keeps the noise dark and bursts bright; untick it to set Min / Max yourself.
- Peak hold draws the strongest level seen at each frequency - handy when aiming an antenna.
- The Spectrum button at the top hides or shows it; drag the divider to resize it.
- Sound (in the spectrum toolbar): hear the radio like SDR++ - static, with a click or "zzt"
  for each burst. "Whole band" plays every burst the receiver hears; "Tuned" plays one channel -
  click the spectrum to choose it (the yellow marker). Nothing is decoded into speech.
  "Audio" next to the volume: an optional low-pass (softer, less hiss) and high-pass (less
  rumble) filter, and a squelch that mutes the static and opens only for bursts louder than the
  noise by the amount you set. Your choices are remembered.


THE CONSOLE (more detail)
-------------------------
Double-click "Iridium Console.bat" for a burst-by-burst view, like a radio log:
every decoded frame with its type, signal strength and frequency, ring alerts with the
satellite's name and position, damaged bursts, and the decoder's own status every 10 s.
- It watches whatever the tracker's receiver is decoding, so you can run both together.
- Or press R in the console to receive from there without the tracker (close the
  tracker's receiver first). Press R again or Q to stop; the capture goes into the catalog.
- B turns beeps on/off (short = damaged, long = decoded, extra long = ring alert with a
  position). D shows/hides damaged bursts.
- To list a finished capture:  Iridium Console.bat --capture output-<date-time>


YOUR DATA (all in the data folder, next to app)
-----------------------------------------------
Updating or replacing the program never touches the data folder. Click the disk button at the
top right of the tracker to see how much space it uses, change the limits, or clean up.

settings.json              your antenna location and storage limits
catalog-data.json          everything heard so far (satellites, planes, positions) - never
                           deleted; this is what the tracker shows
catalog.txt                the same as a readable report
iridium-names.tle          public list of Iridium satellites, refreshed from CelesTrak every
                           couple of days - used ONLY to put names on satellites
captures\                  one set of files per receiver run:
  output-<date-time>.bits       raw bursts from the decoder
  output-<date-time>.live       every burst decoded
  output-<date-time>.log        the decoder's messages
  output-<date-time>.start      the exact time the capture started (kept)
  output-<date-time>.frames.gz  only the ring alerts and time & location frames (kept, ~1-2%
                                of the size) - enough to rebuild the catalog
tiles\                     cached map pictures from OpenStreetMap

Keeping it small: once a capture is in the catalog, its raw files (.bits .live .log) are
deleted after 7 days, or sooner if they add up to more than 1 GB (oldest first). The map
cache is kept under 200 MB, and receiving stops if the disk gets below 1 GB free. All four
limits can be changed in the disk panel. Raw files hold every burst the receiver heard,
including other people's voice and message bursts (never decoded) - set "Keep raw
recordings" to 0 days to delete them as soon as a capture is in the catalog.


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
