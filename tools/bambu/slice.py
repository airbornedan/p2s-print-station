"""Slice an STL into a .gcode.3mf with the station's slicer and profiles; stop on a slicer warning.

    python3 slice.py part.stl out.gcode.3mf --supports off|tree|plate [--brim auto|outer|none]
                     [--nozzle 0.4] [--quality "0.20mm Standard @BBL P2S"] [--filament "Generic PLA @BBL P2S"]
                     [--allow-warnings]

--supports is required: the choice is made on purpose, never by default. If the slicer warns (e.g. "floating
cantilever ... enable support generation") no file is written and the exit status is 2, unless
--allow-warnings. Prints grams, minutes and any warnings.
"""
import argparse, json, os, shutil, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import siteconf  # noqa: E402
import slicecheck  # noqa: E402
from estimate import flatten  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stl")
    ap.add_argument("out")
    ap.add_argument("--supports", required=True, choices=("off", "tree", "plate"))
    ap.add_argument("--brim", default="auto", choices=("auto", "outer", "none"))
    ap.add_argument("--nozzle", default="0.4")
    ap.add_argument("--quality")
    ap.add_argument("--filament")
    ap.add_argument("--allow-warnings", action="store_true")
    a = ap.parse_args()
    suffix = "" if a.nozzle == "0.4" else " %s nozzle" % a.nozzle
    machine = "Bambu Lab P2S %s nozzle" % a.nozzle
    quality = a.quality or ("0.20mm Standard @BBL P2S" + suffix)
    filament = a.filament or ("Generic PLA @BBL P2S" + suffix)
    prof = flatten(machine, quality, filament)
    with open(os.path.join(prof, quality + ".json")) as f:
        proc = json.load(f)
    proc.update(curr_bed_type="Textured PEI Plate", enable_support="0")
    if a.supports != "off":
        proc.update(enable_support="1", support_type="tree(auto)",
                    support_on_build_plate_only="1" if a.supports == "plate" else "0")
    if a.brim == "outer":
        proc.update(brim_type="outer_only", brim_width="5")
    elif a.brim == "none":
        proc.update(brim_type="no_brim")
    with tempfile.TemporaryDirectory() as tmp:
        pj, out = os.path.join(tmp, "process.json"), os.path.join(tmp, "out.gcode.3mf")
        with open(pj, "w") as f:
            json.dump(proc, f)
        r = subprocess.run([siteconf.path("appimage"), "--slice", "0", "--arrange", "1", "--orient", "0",
                            "--load-settings", "%s;%s" % (os.path.join(prof, machine + ".json"), pj),
                            "--load-filaments", os.path.join(prof, filament + ".json"),
                            "--export-3mf", out, os.path.abspath(a.stl)],
                           capture_output=True, text=True, timeout=600, cwd=tmp)
        warns, err = slicecheck.read_result(os.path.join(tmp, "result.json"))
        if err or not os.path.isfile(out):
            sys.exit("slice failed: %s %s" % (err or "", (r.stderr or r.stdout).strip()[-300:]))
        with open(os.path.join(tmp, "result.json")) as f:
            plate = json.load(f)["sliced_plates"][0]
        grams = sum(x.get("total_used_g", 0) for x in plate.get("filaments", []))
        print("%.1f g, %d min, supports %s" % (grams, round(plate.get("total_predication", 0) / 60), a.supports))
        for w in warns:
            print("SLICER WARNING:", w)
        if warns and not a.allow_warnings:
            sys.exit("not written: fix the warning (supports/orientation) or pass --allow-warnings")
        shutil.copyfile(out, a.out)
        print("wrote", a.out)


if __name__ == "__main__":
    main()
