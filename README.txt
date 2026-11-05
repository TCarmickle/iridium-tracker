SATELLITE TRACKER
=================

A map of the satellites YOUR antenna hears, with an RTL-SDR: Iridium (1.6 GHz), ORBCOMM
(137 MHz), the ISS and amateur (ham) CubeSats on 2 m and 70 cm, and the GPS and Galileo
navigation satellites with a GPS antenna (see "SYSTEMS" and "GNSS" below). Every position and
orbit comes from what you receive - no downloaded orbit data is used for positions.

Iridium satellites send ring alerts that carry their own position; with their Doppler shifts
the tracker fits every orbit from those observations alone. Orbcomm satellites send their
position too, and GPS / Galileo satellites broadcast their own orbit. The map, list, globe and
sky plot show every system together, with a Systems filter to show just one.

Everything is in this folder: the decoders (gr-iridium, iridium-toolkit, gr-satellites,
PocketSDR), Python, and the app.
Nothing is installed on the computer.


WHAT YOU NEED
-------------
- Windows 10 or 11 (64-bit)
- An RTL-SDR that tunes to 1626 MHz. Tested with the Nooelec SMArTee XTR (E4000 tuner);
  R820T/R828D "V3/V4" dongles also reach 1626 MHz.
- An antenna for L-band (1.6 GHz) with a clear view of the sky. Even a small patch or a
  short telescopic whip outdoors decodes ring alerts.
- For Orbcomm: an antenna for 137 MHz - a telescopic whip at about 52 cm, or a V-dipole with
  two arms of about 53 cm opened to 120 degrees (the classic 137 MHz satellite antenna).
- For the ISS / ham satellites: a whip of about 49 cm (19 1/4 in) for 2 m or 16.5 cm (6 1/2 in)
  for 70 cm, or a 2 m / 70 cm dual-band whip.
- For GPS / Galileo: an active GPS antenna (the usual puck with an SMA plug, 3-5 V) and an SDR
  with a bias-T to power it.


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
2. Double-click "Satellite Tracker.bat".
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


SATELLITE INFO (click a satellite)
----------------------------------
Clicking a satellite on the map, the globe or in the list opens a window about it, with tabs:
  About          its launch (date and mission), block, hardware, the public catalog's orbit, and
                 how your receiver identified it
  Doppler curve  each pass's measured frequency shift plotted against the predicted curve it was
                 matched to: when it was closest, how high it was, and how much better it matched
                 than the next-best satellite
  Heard          every pass you picked it up (double-click one to jump the timeline there)
  Orbit          our own orbit for it, compared with the public catalog's
Launch and catalog facts come from the public CelesTrak list; the map and globe still use only
your own observations.


GLOBE (Globe button)
--------------------
The same picture as the map, on a 3D globe: the satellites at their orbital height, the
orbital-plane rings, the selected satellite's past (orange) and future (yellow) path, your
position fixes (red) and you. The night side is shaded from the Sun's real position. Anything
behind the Earth is drawn faintly. Drag to move around the Earth, mouse wheel to zoom,
right-drag up/down to tilt and left/right to turn, "Center on me" (or double-click) to come
back to your location,
click a satellite to select it. "Top view" looks straight down; "Side view" looks across the
Earth towards the horizon, with the orbits arching over it. "Stand here" shows the sky as if you
were standing at your location - the satellites passing overhead, with the compass along the
horizon and lines 30 and 60 degrees up; drag to look around, wheel to zoom. It follows the timeline, so Play
and scrubbing move the satellites on the globe too.


SKY PLOT (Sky button)
---------------------
Where in your sky the satellites were when your antenna heard them: the centre is straight
overhead, the edge is the horizon, north is up and east is right. Big dots are position fixes
(coloured by signal strength), small dots are other frames from identified satellites. Empty
areas are directions or heights your antenna can't hear - a house or roof in the way, or the
antenna's blind spot (a vertical whip hears little straight up). Move the antenna and watch the
empty areas fill in; "Show" picks this run, the last hour, the last day or everything.


SYSTEMS: IRIDIUM, ORBCOMM, ISS AND HAM SATELLITES
--------------------------------------------------
One SDR listens to one band at a time, so pick what to receive - in the box next to Start
receiver, or in Source:
  Iridium        (1.6 GHz)  antenna about 4.6 cm, or a patch antenna facing up
  Orbcomm        (137 MHz)  antenna about 52 cm (whip), or a V-dipole of 2 x 53 cm at 120 degrees
  ISS & ham 2 m  (145 MHz)  antenna about 49 cm (19 1/4 in)
  Ham 70 cm      (436 MHz)  antenna about 16.5 cm (6 1/2 in)
  GNSS L1 (GPS)  (1575 MHz) an active GPS antenna, straight into the SDR (see GNSS below)
Change the antenna to match, then press Start. Each band keeps its own gain. Every band but
Iridium has a fixed sample rate and frequency, set for its decoders.

Orbcomm satellites send an "ephemeris" packet every few seconds with their own position and
velocity. The receiver decodes all eight downlink channels at once, names each satellite by
where it says it is, and fits its orbit from those positions (and the Doppler shift of every
packet), just like Iridium. On the map Orbcomm satellites have orange-edged solar panels and
"OC" labels; Iridium ones are "IR".

ISS and ham satellites: the receiver watches the whole 2 m or 70 cm slice. When a signal appears
it measures the signal's exact frequency (its Doppler shift) and tries the gr-satellites decoders
of the satellites that transmit near there (about 400 satellites are known). A frame that passes
its checksum names the satellite:
  - the ISS: only APRS packets the ISS itself repeated (RS0ISS / NA1SS in the path) - people on
    the ground use the same frequency, and those packets are ignored
  - a CubeSat: only if no other satellite uses the same frequency and signal; frames from shared
    frequencies are shown in the log as "could be ..." but not credited to any satellite
Frame contents are never saved - only that a frame was heard, when, at what frequency and from
which callsign. These satellites don't send their own position, so they appear in the list
(with their Doppler curves) but not on the map until their orbit can be worked out from Doppler
alone.

Everything you have received is always shown; the system buttons above the satellite list
(All / Iridium / Orbcomm / ISS & ham / GNSS, each with how many satellites it has) filter the
map, list, globe and sky plot, and the boxes under them sum up the system you picked.
Iridium's orbital-plane rings are Iridium only.

Sound: the Audio menu (next to the spectrum) has a Mode (NFM, WFM, AM, USB, LSB, CW) and a
Bandwidth for the tuned sound; each band starts on its own default (ham: NFM). To hear the ISS's
voice contacts or SSTV events, tune to 145.800 MHz on NFM.


GNSS: GPS AND GALILEO (1575.42 MHz)
-----------------------------------
GPS and Galileo satellites all transmit on 1575.42 MHz. Their signals are weaker than the radio
noise - nothing shows in the spectrum - but a GNSS receiver digs them out by correlating with
each satellite's code. The tracker uses PocketSDR (by T. Takasu, the author of RTKLIB) for that.

Antenna: an ACTIVE GPS antenna (it has an amplifier inside, powered through the coax). Plug it
straight into the SDR and turn on Bias-T in Source (some SDRs, like the SMArTee XTR, power the
antenna all the time). It needs open sky: outdoors, or at least in a window. A metal plate under
a puck antenna (10 cm or more across) helps - it blocks signals bouncing off the ground.

Pick "GNSS L1 (GPS)" next to Start receiver and press Start. The receiver streams the signal
straight into PocketSDR, which tracks the satellites live: they show up as they lock on (usually
within a minute or two) and stay locked, and their updates arrive every second or two. Leave it
running at least 10-15 minutes.
  - Satellites it tracks appear in the list ("GPS G06", "Galileo E18") with their Doppler
    curves, as "Doppler only, no orbit" at first.
  - Once a satellite has been tracked long enough (about 30 seconds of strong, unbroken signal)
    its broadcast orbit (ephemeris) is decoded: the satellite's own description of its orbit.
    It then goes on the map and globe at its real height (about 20,200 km) with a full orbit,
    and the summary box counts it under "with broadcast orbit".
  - With 4 or more broadcast orbits the receiver works out where YOU are: the log shows
    "GNSS FIX lat, lon, height (N satellites)". It should land near the location you set
    (within a kilometre or so) - a good check of the whole chain.
A broadcast orbit is accurate to a few metres for about 4 hours and slowly gets less certain
after that; the next run refreshes it (each satellite sends a fresh one every couple of hours).
BeiDou is not received (PocketSDR decodes no BeiDou orbit at this frequency, and searching its
codes would slow the GPS and Galileo search). A GPS orbit takes 12 hours, so its track
on the flat map sweeps from about 55 degrees south to 55 north and back across half the world;
on the globe it is a ring around the Earth.

WAAS (in the same band, at the same time): the FAA's GPS-correction satellites are geostationary
(35,786 km up, over the equator, south of you) and send a GPS-like signal on 1575.42 MHz. The
receiver tracks the North American ones too (WAAS 131, 133, 135) and decodes the position each
one broadcasts every couple of minutes, so they appear as "WAAS 131" and so on, standing still
in your southern sky.

GLONASS (its own band - pick "GNSS L1 (GLONASS)" next to Start): Russia's navigation satellites
send on about 1602 MHz, each satellite on its own frequency channel. The SDR's slice covers the
middle channels (about 5 of 14), so several GLONASS satellites at a time, not all. Each one's
broadcast orbit (position and velocity every 30 minutes) is integrated forward, as GLONASS
receivers do; it agrees with the public catalog to tens of km over hours. Your antenna has to
cover 1602 MHz - most GPS pucks do. GLONASS can't be received at the same time as GPS: one SDR
hears one band at a time.

Offset tuning (Source, on by default for the E4000 tuner) keeps the SDR's own spike at the
centre frequency off the signal. If nothing is tracked after 10 minutes: check the antenna has
sky, Bias-T is on, and try a higher gain in Source.


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


RADIO (the Radio section - listen anywhere, like SDR++)
--------------------------------------------------------
Click "Radio" at the top for a big spectrum and waterfall and a tuner for any frequency the SDR
reaches (about 24 MHz to 2.3 GHz): FM radio, NOAA Weather Radio, aircraft, ham, marine...
- The big digits are the frequency: scroll over any digit to step it (each digit is its own
  place - 1 MHz, 100 kHz, 10 kHz ... 1 Hz), or click its top half (up) / bottom half (down).
  Or type it in MHz and press Enter, or pick one from "Quick picks".
- If another band is being received (Iridium, GPS...) and you tune outside its slice - digits,
  typing or a quick pick - it stops that band (its capture is kept as usual) and starts Radio
  at the new frequency.
- Sizes are yours to set, in every section: drag the bar with the frequency scale (the grips
  at its ends) up or down to give the spectrum line or the waterfall more room, and drag the
  dividers between panels. The window's size and place, every divider, and the spectrum /
  waterfall split (one for each section) are remembered for next time.
- Drag the spectrum sideways to slide along the band (drag left = up in frequency); the SDR
  moves when you let go. The labels under the spectrum show what is where (FM radio, aircraft,
  marine, NOAA Weather Radio, ham bands, TV, cell phones, ADS-B, GPS...).
- The digits go from 0 Hz up, but the SDR receives about 24 MHz - 2.3 GHz (E4000 tuners from
  about 52 MHz); outside that it stays at the nearest end and says so.
- Press "Start radio" (this uses the receiver on the Radio band). Turn the volume up on the
  spectrum bar. When the frequency leaves the slice the SDR is receiving (2 MHz wide), the
  SDR moves with it.
- Mode: NFM (voice, weather radio), WFM (FM broadcast), AM (aircraft), USB / LSB (ham sideband),
  CW (Morse). Bandwidth: the slider or the box - or drag the edges of the yellow band on the
  spectrum, in any section: grab an edge (the pointer turns into arrows) and drag it out or in.
- Click the spectrum to listen there; the scroll wheel over the spectrum line steps 1 kHz
  (10 kHz with Shift); over the waterfall it zooms.
- While another band is being received (Iridium, GPS...), the Radio section can listen anywhere
  in that band's slice; to go anywhere else, stop it and press Start radio.


WEATHER PICTURES (the Images section)
-------------------------------------
Click "Images" at the top to switch from the tracker to the pictures received from the
Meteor-M weather satellites (Russia; LRPT on 137.9 MHz, a pass every few hours, about 15 min).
- To receive one: during a pass press "Receive a Meteor pass" (or choose "Meteor-M weather
  pictures" as the receiver band and press Start receiver). The status line shows the signal
  over the noise every couple of seconds - a few dB or more is good. Press Stop when the
  satellite has set; a minute or two later the pictures appear in Images.
- Antenna: a whip about 21 in / 53 cm long (a quarter wave at 137.9 MHz - a telescopic one
  pulled out that far), upright on something metal, outdoors with open sky. The higher the
  pass goes, the better the picture.
- The pictures are made by SatDump (free, open source, GPL-3.0 - https://www.satdump.org):
  install it with its Windows installer; the tracker finds it in C:\Program Files\SatDump.
  While receiving, the pass is recorded (about 1 GB, deleted once its pictures are made; a
  recording that gave no pictures is kept so "Make pictures again" can retry it).
- Click a pass, then a picture: scroll to zoom, drag to move, double-click to fit. "Save as..."
  copies it anywhere; "Open" opens it in your picture viewer.
- Passes received with SatDump itself: "Import a SatDump folder..." copies their pictures in.
- The Satellite Console shows the signal during a Meteor pass too (band 7).
- Pictures are kept in data\images (one folder per pass).


THE CONSOLE (more detail)
-------------------------
Double-click "Satellite Console.bat" for a line-by-line view of the receiver, like a radio log,
on whichever band it is on (shown at the top, with the frequency it is tuned to):
- Iridium: every decoded frame with its type, signal and frequency, ring alerts with the
  satellite's name and position, damaged bursts, and the decoder's status every 10 s.
- Orbcomm: each packet, and the satellites' own positions (ephemeris packets).
- ISS / ham: each frame with the satellite and callsign (never its contents).
- GNSS (GPS, Galileo, WAAS, GLONASS): each satellite as it is acquired, then its signal
  (dB-Hz), Doppler and lock every 15 s, when it is lost, its broadcast orbit as it is
  decoded, and the receiver's own position fixes.
- It watches whatever the tracker's receiver is doing, so you can run both together.
- Or receive from the console without the tracker (close the tracker's receiver first):
  press 1-7 to choose the band (Iridium, Orbcomm, ISS & ham 2 m, ham 70 cm, GPS L1,
  GLONASS L1, Meteor-M pictures - it starts on the tracker's last one), then R. Press R again or Q to stop;
  the capture goes into the catalog. The choice is the console's only: the tracker keeps its own.
- B turns beeps on/off (short = not decoded, long = decoded, extra long = a position).
  D shows/hides what was heard but not decoded.
- From the command line: Satellite Console.bat --band gnss1 --receive
  (bands: iridium orbcomm ham2m ham70 gnss1 glo1)
- To list a finished capture:  Satellite Console.bat --capture output-<date-time>
  (or gnss-..., glo-..., orbcomm-..., ham2m-..., ham70-...)


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
  (Orbcomm, ham and GNSS runs are named orbcomm-, ham- and gnss-<date-time> and keep only the
  lines the tracker uses: positions, Doppler, broadcast orbits and fixes)
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
- gr-satellites (ham decoders)   GPL-3.0     https://github.com/daniestevez/gr-satellites
- PocketSDR (GNSS, app\pocketsdr) BSD-2-Clause, (c) T. Takasu   https://github.com/tomojitakasu/PocketSDR
                                  (license in app\pocketsdr\LICENSE.txt)
- Orbcomm decoder (app\orbcomm)   MIT, adapted from ORBCOMM-receiver by Frank Bieberly,
                                  https://github.com/fbieberly/ORBCOMM-receiver (its RRC filter
                                  is from CommPy, BSD-3-Clause; notices in app\orbcomm)
The tracker itself (the files in app\ other than iridium-toolkit and orbcomm) is free software under the
GNU General Public License v3 (LICENSES\GPL-3.0.txt). The source code is the .py files in app\.
