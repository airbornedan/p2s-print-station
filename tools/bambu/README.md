# tools/bambu

Command-line P2S tools. Settings: `siteconf.py`. Shared connection: `p2s.py`.

## Access code and modes

The LAN access code is a credential; never commit it. Read from `$P2S_ACCESS_CODE`, `config.toml`, or Bambu Studio's saved config. It changes on cloud ↔ LAN switch.

| | Cloud | LAN + Developer |
|---|---|---|
| Status, AMS, file list, USB upload | yes | yes |
| Start, stop, pause, light | no ("verify failed") | yes |
| Camera (port 322) | no | yes |
| Bambu Handy | yes | no |

## Slicing

1. Flatten profiles once (the CLI needs complete settings):
   ```
   python3 profiles.py squashfs-root/resources/profiles/BBL out \
       "Bambu Lab P2S 0.4 nozzle" "0.12mm High Quality @BBL P2S" "Bambu PLA Matte @BBL P2S"
   ```
2. Slice:
   ```
   <AppImage> --slice 0 --arrange 0 --orient 0 \
     --load-settings "out/Bambu Lab P2S 0.4 nozzle.json;out/0.12mm High Quality @BBL P2S.json" \
     --load-filaments "out/Bambu PLA Matte @BBL P2S.json" \
     --export-3mf part.gcode.3mf part.stl
   ```

- `--arrange 0` keeps the mesh's XY: 1:1 plate mm, origin front-left, must fit 0–256 mm. `--arrange 1` recentres.
- Merge a part and its print aids (e.g. break-off fins) into one STL first.
- Weight and time: `Metadata/slice_info.config` in the 3MF.
- The AppImage can't render thumbnails headless; `thumb.py part.gcode.3mf [#RRGGBB]` adds them.
- Flattened `brim_type` is null; set `outer_only` for a brim.

## Scripts

- `render.py part.gcode.3mf out.png "#68724D"`: preview image.
- `upload.py part.gcode.3mf`: copy to the USB stick.
- `start.py <name.gcode.3mf> <ams_slot 0-3> [url] [--vibration]`: start a file on the stick; vibration calibration only with `--vibration`.
- `status.py`, `hms.py`: state, temperatures, AMS, errors.
- `control.py {stop|pause|resume|light_on|light_off}`
- `cam.py {snap out.jpg | clip <secs> out.mp4}`
- `clear_and_check.py`: stop and clear an error.
- `gcodecheck.py part.gcode.3mf`: checks the start G-code has the AMS load, bed leveling and nozzle heat (an incomplete machine profile drops them). The station and `start.py --local` refuse a file that fails.
- `estimate.py parts.toml`: sliced filament estimates and a print-requirements table for a project (format in the docstring). Stops on a slicer warning unless the job has `allow_warnings = true`.
- `slice.py part.stl out.gcode.3mf --supports off|tree|plate`: slices with the station's profiles; `--supports` is required. Writes nothing on a slicer warning (e.g. "floating cantilever") unless `--allow-warnings`. `--keep-position` slices the STL where it is (plate mm) instead of auto-arranging; needed for big parts with supports, which auto-arrange refuses.
- `slicecheck.py part.gcode.3mf`: the slicer's warnings for an already-sliced file (re-slices its model).

## Printer quirks

- Files on the stick are `ftp:///<name>`. `file:///sdcard/<name>` or a missing file fails with `print_error 83902466` and state FAILED.
- FAILED persists after `clean_print_error` or `stop`; starting a new job clears it.
