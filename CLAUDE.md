# Satellite Tracker - notes for Claude

Read this before working on the project. The owner's location, preferences and the personal-data check run before
every push are in `CLAUDE.local.md` beside this file (git-ignored) - read both. Written 2026-11-06 at commit
`7e3bdec` and checked against the code; the code is the source of truth where they disagree.

## What it is
A Windows desktop app (PyQt5 + GNU Radio, bundled Python) that receives satellites with ONE SDR at a time (built and
tested on an RTL-SDR; any gr-osmosdr device works), decodes them, and shows them on a map, globe, sky plot and signal
views. Public repo: this checkout's `origin` (branch `main`, GPL-3.0). Local checkout: `C:\IridiumTracker`.

**Core rule (owner's requirement since 2026-11-01): every position and orbit drawn comes from the owner's own
receptions** - Iridium ring-alert positions + Doppler and Orbcomm's self-reported positions (OEP packets) + Doppler,
both fitted by orbitfit.py; GNSS broadcast ephemerides decoded here; ham Doppler fits. Public TLEs/CelesTrak are for
names, identification, reference facts and comparison in the satellite info window (satinfo.py), and test
validation - **never for drawing a satellite on the map, globe or sky plot**.

**Privacy/law rule**: only technical frames are used. Iridium voice/SBD/paging/data contents and ham frame payloads
are never decoded, shown or stored (console marks VOC/VDA private; `storage.write_reduced` strips ` PAGE(...)`;
`HFR` lines never carry payload bytes). README.txt "PRIVACY AND THE LAW" explains why.

Sections (header buttons): **Tracker** (map/globe/sky/signal views, satellite list, timeline), **Images** (Meteor-M
pictures via SatDump), **Radio** (SDR++-style listening 24 MHz-2.3 GHz), **Integrity** (GNSS jamming/spoofing watch).

## Working rules
- **Push only when the owner says "Yes, push"** (or clearly equivalent).
- Before every push run the personal-data grep in `CLAUDE.local.md` on the staged diff; it must be empty. Never commit
  `data/`, settings, captures, coordinates, names or emails (public repo). **Screenshots from the app show the home
  coordinates** (status bar "You: …", map centred on home) - never commit or attach them.
- Commit messages: short title, body of what/why/how-checked, ending with the `Co-Authored-By:` trailer of the current
  Claude model. Use `git commit -F <file>` (PowerShell quoting broke `-m`).
- Keep `README.txt` (the user guide shipped in the zip) updated with every user-visible feature; `README.md` is the
  GitHub overview, `BUILDING.md` the build notes.
- Style in the tree: plain-language comments that say *why* (often with the measurement behind a constant); British
  spelling in UI text ("colour", "centre" - two buttons still say "Center": globe.py, iridium_tracker.py); nothing may
  take the tracker down (background work catches and logs). New persistent files: write atomically (`.tmp` then
  `os.replace`) as catalog, refpoints/geoid, gnss_rx state/baseline and reduced frames do (settings.json and a few
  others are still written directly).
- Test before claiming: replay recorded data offline, then a live run with the owner. Report measured numbers.
- Only one program can use the SDR. The owner usually has the tracker open and receiving; ask them to press Stop
  rather than killing their processes (killing their receiver from outside was refused by the permission system).
- Network calls the app makes: OSM tiles, CelesTrak (names, `requests`, 15 s timeout), Open-Meteo elevation (sends
  the home lat/lon), Windows Location via PowerShell. Nothing else.

## Hardware it is built around
- SDR: **Nooelec NESDR SMArTee XTR v5** - RTL2832U + **Elonics E4000** tuner (~52-2300 MHz), 0.5 ppm TCXO (PPM 0 is
  correct), **always-on bias tee (~4.5 V)** that powers an active GPS antenna through the coax. WinUSB via Zadig.
- E4000 quirks:
  - **IF gain**: at the driver default the samples sit at ~0.6 % of ADC full scale (under one 8-bit step).
    `--if-gain 60` lifts L-band to ~9-10 % (GPS went from 0-2 satellites at ~33 dB-Hz to 5 at ~37). At 137 MHz with
    gain 40, IF 60 measured *lower* than 50, so Meteor defaults to 50. "Tune gain" (gain_tune.py) measures it.
  - A **DC spike** at the tuned centre. Meteor avoids it in code (tuned 200 kHz below 137.9 MHz), Radio too
    (`RADIO_OFFSET_HZ` 250 kHz). GNSS is tuned exactly on its carrier and relies on the Source panel's **Offset
    tuning** option - which defaults to OFF (`sdr_settings.DEFAULTS['offset_tune']`), is ON in the owner's settings,
    and with it off GNSS lost every satellite. (README.txt wrongly says it is on by default.)
- Antennas used: an **active GPS L1 puck** (3-5 V, bias-tee powered) for all GNSS work (also fine for Iridium);
  telescopic and coil whips for VHF/UHF. GNSS needs open sky: under cover 3-5 GPS satellites at 33-38 dB-Hz;
  outdoors with a clear view 15 satellites for an hour.
- Reference receiver: a **Garmin GPSMAP 64st** handheld (see "Garmin").

## Running, testing, debugging
- Launch: `Satellite Tracker.bat` (pythonw `app\start.py`), `Satellite Console.bat` (`app\console.py`, a line-by-line
  log of every band except Radio; `--receive --band <key>` receives without the window; `--capture NAME` prints a
  finished capture).
- Bundled runtime `C:\IridiumTracker\python` (git-ignored, ~550 MB, `build\build_runtime.py`, pinned
  `build\environment.yml` incl. `gnuradio-iridium=1!1.0.0`). For anything run by hand use the union of what the two
  .bat files and start.py set:
  ```powershell
  $P='C:\IridiumTracker\python'; $env:PATH="$P;$P\Library\mingw-w64\bin;$P\Library\usr\bin;$P\Library\bin;$P\Scripts;$P\bin;$env:PATH"
  $env:PYTHONNOUSERSITE='1'; $env:OPENBLAS_NUM_THREADS='1'; $env:QT_PLUGIN_PATH="$P\Library\plugins"
  cd C:\IridiumTracker\app; & "$P\python.exe" -W error -m py_compile <files>
  ```
  The app itself forces `OPENBLAS_NUM_THREADS=1` (start.py, console.py, and Receiver for every child) - otherwise
  OpenBLAS reserves ~2.5 GB on this 20-core PC and trips the 2 GB Iridium memory watchdog; set it by hand too.
  `pocket_trk.exe` needs `python\Library\bin` on PATH (SoapySDR.dll; else STATUS_DLL_NOT_FOUND, exit 53 / -1073741515).
- Built-in hooks: `start.py --snapshot OUT.png [--at=ISO] [--select=NAME_WITH_UNDERSCORES] [--system=KEY] [--theme=]
  [--map-theme=] [--info] [--windows]`, `--test-receiver=<old .bits>` (replays through the real parser),
  `--start-receiver`.
- **Tests are scratch scripts in `build\_tmp\` (git-ignored)**; no test framework. Patterns:
  - Data isolation: set `IRIDIUM_TRACKER_DATA=<scratch dir>` **before importing any app module** (paths.DATA is read
    at import, and importing `paths` creates `captures\` and moves legacy files from `app\` into DATA). Copy
    `settings.json`, the catalog (`catalog\` folder, or legacy `catalog-data.json`), `*.tle` **and** `*.tle.groups`
    (else CelesTrak is fetched again). Scratch tests `rmtree(argv[1])` - never pass the real `data` folder. Some old
    scratch tests use the real data folder.
  - GUI: `QApplication`, `it.apply_app_colours(app)`, `w = it.Tracker()` (the argument is `replay`, not a parent),
    `w.setAttribute(Qt.WA_DontShowOnScreen)`, `w.show()`, pump `processEvents`, drive it
    (`w.sys_box.setCurrentIndex(w.sys_box.findData('gnss'))`, `w.show_section(3)`, `w.deck.show_view('globe')`), then
    `widget.grab().save(png)` and look at it. **`QT_QPA_PLATFORM=offscreen` has no fonts** (blank text).
  - Without the SDR: gr-osmosdr file device `file=C:/path/x.cf32,rate=2048000,freq=..,repeat=true,throttle=true`
    (forward slashes; complex64 only). Run receivers directly (`python gnss_rx.py --device file=... --spp-state ...`)
    and read their stdout lines.
  - GNSS offline: pocket_trk logs survive in `%TEMP%\gnss_live_*\trk-<run>.log` (+ `trk-<run>.rtcm` orbits; never
    cleaned up, contain position fixes). Replay through `spp.Solver` directly, or `gnss_rx.Lines` after setting
    `sys.argv = ['gnss_rx.py', ...]` *before* `import gnss_rx` (it parses argv and loads GNU Radio at import) - see
    `build\_tmp\spp_eval.py`, `warm_logs.py`.
  - Raw signal: while gnss1 runs (or gnss_rx by hand with `--snap-trigger FILE`), write an absolute stem path into
    `data\integrity\iq-trigger.txt`; gnss_rx saves up to 20 s before + 20 s after as `<stem>.cs8` (+ `.json`). For
    pocket_trk convert to INT8X2 **with Q negated**.
- Live tests need the SDR free (tracker receiver, SDR++, SatDump closed) and usually the owner outdoors.
- `.bat` files should be CRLF (`.gitattributes eol=crlf`), but the working copies are LF (attribute applies on
  checkout); renormalize before packaging - make_package.py zips the working tree as is. Edit files with the Edit tool
  or Python, not PowerShell `Set-Content` (encoding/quotes get mangled).

## Architecture
```
Tracker window (iridium_tracker.py: Data, MapView, Timeline, Tracker; all on the Qt main thread)
  ├─ receiver.Receiver (QObject) ── starts ONE band's subprocess(es), tails their output, writes
  │    data\captures\<prefix><YYYYmmdd-HHMMSS>.{live,log,start,bits,s16,running}
  │    signals: status, running(bool), decoded, frame(kind,line), gnss_line(kind,line)
  │    after Stop (and detached on window close): catalog.py --ready=<cap>  (Meteor: images.py --decode <cap>);
  │    Radio captures and runs that received <200 bytes are deleted instead
  ├─ Data: catalog.load_data() + every not-yet-catalogued .live file (poll every 2 s) → orbits/fixes/info
  ├─ spectrum/sound via UDP from the receiver (sdr_taps: IRS2 spectrum, IRAU audio; CTRL/TUNE back)
  └─ sections: Images (images_view), Radio (radio_view), Integrity (integrity.IntegrityView)
```
- **Everything runs on the Qt main thread** (catalog load, orbit fits, live parsing, Integrity, CelesTrak refresh) -
  slow work freezes the window. A QThread whose Python object is garbage-collected crashes the app; keep references
  (`source_dialog._SEARCHES`).
- Time model: all views draw the scene at the timeline time `Tracker.t` (live, play, scrub), not the clock.
  `refresh()` must build a new scene dict each time (the globe caches by identity). Satellites hide beyond
  `MAX_SIGMA_KM` 1500 and fade beyond `FAINT_SIGMA_KM` 150.
- Bands (`systems.ORDER`, RADIO kept last by convention) vs display systems (`systems.DISPLAY`: iridium, orbcomm, ham,
  gnss; `band['system']`). `systems.get`/`of_name`/`of_capture` **silently fall back to IRIDIUM**; ham and GNSS names
  have no prefix and must be `systems.register()`ed when loaded. Capture prefixes match with startswith (a new one must
  not prefix another). Per-band settings in `settings.json['sdr']` as `<key>_gain`, `<key>_auto_gain`,
  `<key>_if_gain` (Iridium bare keys) - a new band needs all three in `sdr_settings.DEFAULTS`.

| key | prefix | centre / rate | process | output lines |
|---|---|---|---|---|
| iridium | `output-` | 1626.5 MHz − rate/2, default 2 MS/s | `sdr_source.py` (runs gr-iridium's extractor via runpy, monkey-patching osmosdr.source / FlowGraph.__init__) → `.bits` → iridium-toolkit `iridium-parser.py` | IRA, ITL, … (RAW/ERR damaged) |
| orbcomm | `orbcomm-` | 137.5 MHz, 1.2288 MS/s | `orbcomm_rx.py` (vendored fbieberly decoder) | OEP OSY OPK OBAD |
| ham2m / ham70 | `ham2m-`/`ham70-` | 145.55 MHz 2.048 / 436.725 MHz 2.4 | `ham_rx.py` (channelizer + gr-satellites) | HFR HSIG |
| gnss1 | `gnss-` | 1575.42 MHz, 2.048 MS/s | `gnss_rx.py` (GPS L1C/A + Galileo E1B + WAAS) | GCH GEPH GPOS GSPP (+GTIME GNOISE GSPEC) |
| glo1 | `glo-` | 1602 MHz, 2.4 MS/s | `gnss_rx.py --constellation glonass` (~5 of 14 channels fit) | GCH GEPH GPOS (+GTIME GNOISE GSPEC) |
| bds1 | `bds-` | 1561.098 MHz, 2.048 MS/s | `gnss_rx.py --constellation beidou` (B1I) | GCH GEPH GPOS (+GTIME GNOISE GSPEC) |
| meteor | `meteor-` | 137.7 MHz, 1.024 MS/s | `meteor_rx.py` → `.s16` 256 kS/s → SatDump | MSIG |
| radio | `radio-` | anywhere, 2.048 MS/s | `radio_rx.py` (spectrum + sound only) | - |

- **Iridium timing lives in the GUI process**: Receiver reads `.bits` every 250 ms, pipes RAW lines into the parser,
  and writes `.start` (the earliest now − burst ms) - the only time base for bursts (`catalog.capture_start`); replay
  mode doesn't write it. sdr_source.py uses gr-iridium private attributes (`_center_frequency`, `_input_sample_rate`) -
  pinned version matters. Never run the extractor below 14 dB threshold (memory flood crashed the PC once);
  `iq_correction` stays off (mirror bursts).
- **A line format is parsed in five places**; a new/changed kind must be handled in all that apply:
  `receiver._orbcomm_output` regex/filters, `catalog.read_*`/`analyze_*`, the tracker's own live parsers
  (`Data._read_live`, `_read_live_orbcomm`, `_read_live_ham`, `_read_live_gnss`), console.py's describers, and
  `integrity.Monitor.line`. Plus `systems.*['keep_kinds']` to survive in reduced files, and `receiver.DAMAGED` if it
  is a failure. GTIME/GNOISE/GSPEC are never saved and reach Integrity only through the tracker's own Receiver
  signals (if the Console receives, Integrity sees nothing).
- Receivers are stopped with `taskkill /T /F` - **their shutdown code never runs**; anything they must keep is written
  atomically while running (gnss-solver.json at most once a minute, noise baseline, IQ snapshots). Every band
  receiver except `sdr_source.py` takes `--parent-pid` and exits when the program that started it dies. The
  `.running` marker (PID) is the SDR lock checked by tracker, console and Tune gain.
- **Catalog** (`catalog.py`, runs after each Stop): analyses each finished capture **once** into
  `data\catalog\<capture>.json` + `index.json` (since 2026-11-06; if `index.json` is missing it falls back to the
  legacy `catalog-data.json` / `.previous.json`; an unreadable index falls back to globbing the folder). Results are
  frozen: changing an analyser does nothing until `catalog.py --redo` (which keeps old results for captures whose files
  are gone; the tracker runs a full redo when home moves > 2 km). After 7 days / 1 GB `storage.py` leaves only
  `.frames.gz` (the `keep_kinds` lines of the time). `save_data` writes only changed files atomically, **deletes
  capture files not in the dict it is given - always pass the full catalog**, and rewrites `index.json` (the tracker
  reloads on its mtime). **Nothing locks against two catalog runs at once** (tracker Stop, console Stop, a redo) -
  overlapping runs can drop each other's new entries until the next run.
  - History: on 2026-11-05 an interrupted save truncated the old 11 MB single file; the tracker silently showed no
    history and every catalog run crashed. Recovered (56 complete entries + re-analysis); `data\
    catalog-data.truncated-20261105.json` is the damaged original. The per-capture layout is the fix.
- Orbits: Iridium/Orbcomm `orbitfit.fit` (circular + J2, fixes + Doppler); in iridium_tracker.py `IRIDIUM_FLOOR_KM` 12
  + `IRIDIUM_DRIFT_KM_H` 2.5 and `FIT_DAYS` 7 (latest week). Ham `orbitfit.fit_doppler` (needs passes on 2+ orbits,
  scaled to 436.5 MHz). GNSS `gnss.EphemerisOrbit` / `SBASOrbit` / `GlonassOrbit` from decoded broadcast
  ephemerides. `Data.fit_orbits` wraps each `_fit_one` so one bad fit records "orbit fit failed" in `no_orbit` (the
  plane-direction retry pass is not wrapped). `fit_orbits` does nothing while there is no home location.

## GNSS in depth (gnss_rx.py, gnss.py, rtcm3.py, spp.py, posquality.py)
- Decoder: **PocketSDR `pocket_trk.exe`** (T. Takasu, BSD-2) in `app\pocketsdr\`. Live mode: samples → int8 IQ
  (`Quantizer`, noise scaled to ~±24) → Windows named pipe → one long-running pocket_trk (restarted if it exits); its
  `-log` and `-rtcm` files are tailed. **Q is negated** in `Quantizer` (pocket_trk's IQ convention is gr-osmosdr's
  mirror; without it every Doppler has the wrong sign). Orbits come only from the RTCM output (its `$EPH` log lines are
  masked) - keep `-rtcm`. RTCM decoded: 1019 GPS, 1045/1046 Galileo, 1042 BeiDou (BDT = GPST − 14 s, week+1356), 1020
  GLONASS. Chunks mode (`--mode chunks`) is an old fallback.
- pocket_trk options file: `-opt FILE`, lines `name = value` (`thres_cn0_l`, `thres_cn0_u`, `thres_pli`, ...). The app
  passes `data\pocket_trk.opt` if it exists (live mode only). Tested on a recording (2026-11-06): the default lock
  threshold is ~35 dB-Hz; 32 *finds* weak satellites (G23, G27) but they drop within ~2 s whatever `thres_cn0_u` /
  `thres_pli` are; ≤30 locks noise (22 satellites below the horizon). Defaults kept. pocket_trk runs are not
  repeatable (search timing varies) - compare settings over several runs.
- Lines emitted (exact formats in `Lines` / `NoiseMeter`):
  `GCH: t sat sig cn0 dop lock [sync nav err lol]`, `GEPH: t sat <fields|GLO,…|SBAS,…>`, `GPOS: t lat lon h nsat`,
  `GSPP: t lat lon h nsat hdop n_waas_iono n_fast n_longterm n_smoothed`, `GTIME: t offset_s nsat spread`,
  `GNOISE: t power rise clip peak peak_mhz floor learning rms_pct`, `GSPEC: t f_lo f_hi v1..v128`.
  **GSPP field order is a contract** with catalog.analyze_gnss and iridium_tracker._read_live_gnss, which both read
  n_smoothed as f[8] (< 4 ⇒ `quick` fix, excluded from accuracy) - change together. `catalog.read_gnss` drops
  kind + t (fields shift by 2); `integrity.Monitor.line` indexes the full split.
- Timing/clock: the stream's start time = sliding 10 s minimum of (arrival − samples so far/rate) − `usb_latency()`;
  each sample's PC time = that + its sample number/rate (re-anchored after dropped samples). `usb_latency` = two USB
  buffers + 5 ms for RTL-SDR (133 ms with default buffers; 0 for other devices). GTIME = GPS − PC, verified against
  NTP to ~2 ms. The owner's PC clock ran ~0.75 s behind; Settings > PC clock sets it (elevated `Set-Date -Adjust`).
- WAAS satellites S31/S33/S35 = PRN 131/133/135 (strongest signals, 34-42 dB-Hz). They give no position themselves;
  type 9 gives the GEO's own position (for the map); types 1/2-5/24/25/18/26 are corrections used by spp.py.
- GLONASS: FDMA, only the middle channels fit 2.4 MS/s; a channel's `$CH` lines are dropped until its slot has been
  decoded, then carry the slot name. BeiDou B1I: pocket_trk's channel PRNs are unreliable (its C38 carried C43's
  orbit) - `BeidouNames` names each by its decoded orbit vs the nearest catalog satellite (≤300 km), holding lines
  until then. A steady spur at ~1560.33 MHz sits near B1I.
- Galileo is tracked but its orbits rarely decode at 33-36 dB-Hz; a second system needs ≥2 of its satellites with
  orbits (own clock offset), so in practice fixes are GPS-only. GLONASS/BeiDou can't be received with GPS (the SDR
  sees ~2 MHz).
- **Own solver `spp.py`** (GSPP lines, GPS band only): pocket_trk `$OBS` (P, L, D, C/N0) → broadcast orbit/clock, TGD,
  Sagnac, Saastamoinen troposphere, ionosphere from the WAAS grid (MT18/26, pierce point 350 km, 5° grid, all 4
  corners needed) else Klobuchar (decoded from GPS subframe 4 page 18) → weighted LSQ (one clock per system, elevation
  mask 10°, drops up to 2 outliers). WAAS fast (PRC) + long-term corrections (IODE must match).
  **Carrier smoothing**: Hatch filter, 100 s; pocket_trk's code−carrier difference has a common per-epoch term
  (receiver clock steering, +16.7 m/s and 1 ms steps) removed with the median across satellites.
  **SETTLE_S = 30**: a satellite's smoothing starts 30 s after its first pseudorange (restarts after a cycle slip
  ≥8 m, a zero carrier phase or one missed epoch); the code loop is still pulling in - without it a side-by-side test
  against the Garmin showed 11 m and −31 m errors. Fix gate: HDOP ≤ 6 and ≥4 satellites smoothed ≥30 s; before that
  "quick" fixes (residual rms < 15 m).
  - Measured (8-9 recorded sessions): 95 % radius 3-16 m vs pocket_trk 11-32 m; half within 1-4 m vs 4-9 m; session
    middles agree to 1.4 m rms horizontally / 5.3 m height vs pocket_trk 2.4 / 8.5. With smoothing and WAAS off it
    reproduces pocket_trk (validates the maths). Live outdoor hour (2026-11-06): ±3 m per fix, 10-min average
    ±1.9 m, 2.6 m from home; pocket_trk ±12 m.
- Warm start: `data\gnss-solver.json` keeps GPS's ionosphere model and the newest decoded orbit of each GPS/Galileo
  satellite (orbits older than 7 days dropped at load; the ionosphere model too if the file is older than 7 days). The
  solver starts with saved orbits whose toe is within 4 h → first (quick) fix 4-11 s before pocket_trk's own; most
  start-up time is pocket_trk acquiring/syncing. `--narrow-search` (only satellites predicted up) exists but is
  **off**: on a recording it locked 4-5 satellites vs 6 with the full list (pocket_trk searches all in parallel).
- `posquality.py` (the "Your position" panel, Navigation view): per-fix R95/CEP50 over 10 min; average uncertainty =
  2.45·σ/√n_eff with n_eff = span/(2τ), **τ ≥ 120 s** (errors last minutes; without the floor a 2-min average claimed
  0.6 m where the Garmin showed 4 m); needs ≥5 min; `long_term()` = every session's middle (split at 10-min gaps),
  uncertainty from how much sessions disagree (uses own-solver fixes once 3+ sessions exist). Distances shown to a ★
  reference point (or one named "antenna"), else home. Also "Up but not heard" (G/E satellites ≥15° up by their own
  orbit but not tracked - a long list means the sky view is blocked), start-up history, sea-level heights.
  `headline`/`details` read keys the caller sets (`source`, `ref`, `ref_m`, `long`, `pocket_r95_m`, `geoid_sep`).
- Session stats (`catalog.session_stats`): kept for every GNSS capture, used (start-up history) and backfilled for
  gnss1 ones: first-fix times (pocket_trk and own solver), fixes, satellites, C/N0 median, middle + accuracy.

## Integrity section (integrity.py) - jamming/spoofing watch
- Watches **GPS L1 only** (`WATCH_BANDS = ('gnss1',)`; the BeiDou spur kept alerting; its module docstring still says
  all GNSS bands). gnss_rx's `NoiseMeter` learns the quiet band (30 s, per band and gain, `data\noise-<key>.npz`;
  deleting the file relearns live) and reports band power rise and narrowband peaks (FFTs at **random offsets** -
  fixed offsets made a 1-2 ms sweep look like a carrier).
- Checks: jam - noise rise (3/6 dB), narrowband peak (5/12 dB), clipping, all-satellite C/N0 drop, lock loss; spoof -
  position vs home (250/1500 m) and drift, GPS-time jumps (PC clock steps removed via time.monotonic), **Doppler vs
  each non-WAAS satellite's own orbit** (after the median offset, ~+654 Hz on this SDR; needs ≥3 with orbits; off by
  50 Hz flags; alert when ≥2 and ≥half are off; real satellites match to 1-1.5 Hz rms), below-horizon satellites, too
  strong / too even. Hold-off 3 s raise / 20 s clear.
- **Arming**: nothing is raised until ≥4 satellites at median ≥33 dB-Hz for 2 min with the band at its quiet level;
  disarms after 5 min of poor signal without extra noise (antenna/cable problem, not an attack).
- Snapshots per event in `data\integrity\<time>-<check>\`: `event.json` (summary: duration, rise/fall shape -
  gradual both ways ⇒ "moving source", signal kind/where in band, per-satellite C/N0 drop), `lines.txt` (2 min before
  → 90 s after, ≤15 min), `waterfall.png`, raw `samples.cs8` ±20 s (~160 MB, newest 5). Limits: 20 per day, newest
  200, one per stuck signal. Event list `data\integrity-events.jsonl`.

## Garmin GPSMAP 64st (reference receiver)
- MediaTek GPS+GLONASS chip, WAAS capable, **no Galileo or BeiDou**, barometric altimeter. Fixes in < 40 s after power
  on because its chip keeps last position, time and self-made orbit predictions (`Garmin\GPS\BEE.BIN`/`ARC.BIN`,
  `MT.NVM`) - the idea behind our warm start.
- **Live over USB** (`app\garmin_usb.py`, ctypes SetupAPI/DeviceIoControl, Garmin USB protocol - no Spanner program
  needed): needs Garmin's USB driver, the unit's Interface = Garmin Spanner (or Serial) and **"No"** to its
  mass-storage prompt. Gives PVT (D800: position, ellipsoid + sea-level height, EPE/EPH/EPV, fix type incl. WAAS, GPS
  time) and satellite records (prn, SNR, elevation, azimuth, status bits 1 eph / 2 WAAS / 4 used). No ephemerides, no
  raw ranges. `python garmin_usb.py [seconds]` prints; `python garmin_usb.py log FILE` records and reconnects across
  power cycles (`GSTATE`/`GPVT`/`GSATS` lines) and saves the geoid separation to `data\geoid.json`. Only one program
  can open the device.
- **As a drive** ("Yes" to mass storage): GPX 1.1 in `Garmin\GPX` (`Current\Current.gpx`, `Archive\`, daily
  `Waypoints_*.gpx`), settings profiles as XML (`Garmin\Profiles\*.gpf` - satellite system and WAAS switches are NOT in
  them), `.Position.gpx` last position, SQLite R-tree geocache DB. Every 30 s the tracker checks for a Garmin drive;
  when its GPX files changed it copies them to `data\garmin\<unit id>\` and turns waypoints and still stretches
  (< 30 m from their middle for ≥3 min, no gap ≥60 s), both within 500 m of home, into reference points
  (`data\references.json`).
- Findings: side by side outdoors the Garmin's 10-min average was 1.5 m from the home setting (home is accurate); the
  local geoid separation is in `data\geoid.json`; indoors it tracked 7 GPS (incl. G23/G27 the SDR missed) but no
  GLONASS and no WAAS. Earlier 13-18 m disagreements were the Garmin indoors elsewhere. A Garmin on top of the GPS puck
  blocks it.

## Other bands - facts that matter
- Iridium carrier is 1626.1 MHz in `catalog.match_doppler` but 1626.0 in `orbitfit.CARRIER`.
- Orbcomm rate must be a multiple of 38 400 in code (comments say 9600). Orbcomm satellites are named by the nearest
  TLE within 300 km of their own reported position (names only).
- Meteor: `meteor_rx` rate/4 must equal `images.BASEBAND_RATE` (256 kS/s). The 2026-11-05 pass got no signal
  (antenna suspected). Meteor on the map from its own measured Doppler is agreed but waits for a successful pass.
- `systems.of_frequency(137.7 MHz)` resolves to ORBCOMM (first match) - sound/spectrum fall back to it.

## Data folder (`data\`, git-ignored; override with IRIDIUM_TRACKER_DATA)
`settings.json` (home, sdr, storage, audio, images, ui), `catalog\` (+ `index.json`), legacy `catalog-data.json` /
`.previous.json` (read only if `catalog\index.json` is missing - editing them does nothing) and
`catalog-data.truncated-20261105.json`, `catalog.txt`, `captures\`, `tiles\`, `*-names.tle(.groups)`, `sdr.conf`,
`gnss-solver.json`, `noise-<key>.npz`, `integrity\` (+ `iq-trigger.txt`), `integrity-events.jsonl`,
`references.json`, `geoid.json`, `garmin\`, `images\`, `new-satellite.wav`, `tracker-error.log`, optional
`pocket_trk.opt`.

## Known issues / stale text (fix when touching nearby)
- `gnss_rx.Lines.line` `$POS` branch checks `len(f) > 11` but reads `f[12]` (IndexError in the reader thread on a
  12-field line; only ValueError is caught).
- The tracker's storage protection (`_housekeeping`, `show_settings` in iridium_tracker.py) hard-codes the Iridium
  prefix (`'output-%s' % self.receiver.stamp`) - a running non-Iridium capture isn't protected; use `receiver.capname`.
- Messages still say "run 2-decode.bat" (iridium_tracker.py, receiver.py); the iridium_tracker docstring mentions
  `3-tracker.bat`/`1-receive.bat` and catalog-data.json; catalog.py, paths.py, BUILDING.md and the `Data` docstring
  name `catalog-data.json`; BUILDING.md's layout table lacks the GNSS/integrity/Garmin modules; README.txt still says
  `app\settings.json`/`app\tracker-error.log` (now in `data\`), "BeiDou is not received" in one place, offset tuning
  "on by default", and YOUR DATA lists catalog-data.json; README.md says GNSS uses "3-minute IQ pieces" (live pipe is
  the default); console help says keys 1-7 and omits `bds1`; refpoints docstring says "nearest" (the tracker uses ★ /
  "antenna"); gpxio.py refers to a non-existent `geoid.py` (it's refpoints.py); integrity.py's docstring says all GNSS
  bands; `sdr_settings` points at BUILDING.md for IF gain notes that aren't there; `dist\` holds an old
  `IridiumTracker.zip` (make_package writes `SatelliteTracker.zip`).

## History (main milestones, newest first)
- 7e3bdec warm start, per-capture catalog + session stats, reference points, GPX, sea-level heights, Garmin import
- 5245d3b position line: average uncertainty + all-sessions figure
- b3be6ea solver settle 30 s (found with the Garmin side by side); Garmin USB reader
- 0ab48c9 own solver: carrier smoothing + WAAS corrections
- 969fc50 position accuracy line; crash-safe catalog saving (after the truncation)
- 24abaf6 / 8d55253 Integrity section: snapshots, arming, GPS-only; jamming/spoofing watch
- 85dc195 BeiDou B1I band; 090aab0 orbit-fit crash fix; 86fbb58 window fit, PC clock, IF gain all bands, input meter,
  Tune gain; 630057e E4000 IF gain 60 dB for GNSS/Meteor
- Earlier: Iridium tracker, Orbcomm, ham/ISS (gr-satellites), Meteor + SatDump Images, Radio section, GNSS GPS/Galileo/
  WAAS, GLONASS, globe/sky/signal views, storage limits, packaging.

## Ideas the owner wants to come back to
GNSS reflectometry snow depth (favourite); auto PPM from the GPS Doppler offset (~+0.42 ppm); precise antenna
position (hours of averaging / RTKLIB + CORS); sky C/N0 map; scintillation logging; Meteor on the map from Doppler
after a good pass; longer Garmin side-by-side runs outdoors; hydrogen line (1420 MHz) with a filtered LNA + horn.
