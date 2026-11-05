# Building Satellite Tracker

The repository holds the source. The ~550 MB Python runtime (`python\`) and the zip are built,
not committed. You need conda or mamba ([radioconda](https://github.com/ryanvolz/radioconda) or
[Miniforge](https://github.com/conda-forge/miniforge)).

## 1. The runtime
```bat
python build\build_runtime.py
```
Creates `python\` from `build\environment.yml` (pinned versions, OpenBLAS instead of MKL), then
removes what is only needed for building software (debug symbols, headers, static libraries).
Use `--conda path\to\mamba.exe` if it can't find conda, `--force` to rebuild.

> Note: the app limits OpenBLAS to one thread (`OPENBLAS_NUM_THREADS=1`). Otherwise OpenBLAS
> reserves a buffer per CPU core (~2.5 GB on 20 cores) and the receiver's memory watchdog stops
> the decoder.

## 2. License texts
```bat
python build\collect_licenses.py
```
Copies each bundled package's license texts from conda's package cache into
`LICENSES\third-party` and lists them in `LICENSES\THIRD-PARTY-PACKAGES.txt`.

## 3. The zip
```bat
python build\make_package.py
```
Writes `dist\SatelliteTracker.zip` (~200 MB). It never includes the `data` folder (your
location, captures, catalog, map cache).

## Running from source
`Satellite Tracker.bat` runs `app\start.py` with the runtime in `python\`. To develop with another
conda environment that has the packages in `environment.yml`: `python app\start.py`.

## Layout
| Path | What |
|---|---|
| `app\start.py` | entry point |
| `app\iridium_tracker.py` | the window: map, timeline, satellite list |
| `app\meteor_rx.py` | Meteor-M receiver: records the 137.9 MHz pass (256 kS/s 16-bit IQ) and reports its signal |
| `app\images.py` | Satellite pictures: runs SatDump's `meteor_m2-x_lrpt` pipeline on a recorded pass, imports SatDump folders |
| `app\images_view.py` | The Images section: passes, pictures, zoomable viewer, save |
| `app\console.py` | Satellite Console: detailed line-by-line log on every band, a band choice, or receiving without the window |
| `app\sdr_source.py` | runs gr-iridium's extractor with the device options and the spectrum tap (UDP) |
| `app\sdr_settings.py` | SDR source settings, device discovery, extractor config |
| `app\source_dialog.py` | the Source panel |
| `app\spectrum.py` | spectrum + waterfall view |
| `app\receiver.py` | runs iridium-extractor + iridium-parser while receiving |
| `app\catalog.py` | adds finished captures to the catalog (`catalog-data.json`, `catalog.txt`) |
| `app\orbitfit.py` | fits orbits to your own position fixes and Doppler |
| `app\paths.py` | where data lives (`data\` next to `app\`; override with `IRIDIUM_TRACKER_DATA`) |
| `app\storage.py` | reduced frame files, retention limits, map cache cap, disk checks |
| `app\data_dialog.py` | the Data panel |
| `app\location_dialog.py` | typed location, terrain height lookup |
| `app\iridium-toolkit\` | [iridium-toolkit](https://github.com/muccc/iridium-toolkit) (BSD-2), vendored at commit `7938ae0` |
| `tools\` | developer tools (orbit accuracy check, capture summary) |
| `build\` | runtime, license and package scripts |
