"""Slice an STL into a .gcode.3mf with the station's slicer and profiles; stop on a slicer warning.

    python3 slice.py part.stl out.gcode.3mf --supports off|tree|slim|plate [--brim auto|outer|none]
                     [--nozzle 0.4] [--quality "0.20mm Standard @BBL P2S"] [--filament "Generic PLA @BBL P2S"]
                     [--allow-warnings] [--place X,Y | --keep-position] [--allow-center]

--supports is required: the choice is made on purpose, never by default. If the slicer warns (e.g. "floating
cantilever ... enable support generation") no file is written and the exit status is 2, unless
--allow-warnings. Prints grams, minutes and any warnings.

Placement: the plate's center is worn and parts printed there can come loose, so by default the part goes
to a quadrant beside it (the next quadrant each time). --place X,Y puts the footprint's center at plate
mm X,Y (origin front-left); --keep-position slices the STL where it is. Only a part too big to keep off
the center is left to the slicer's auto-arrange. A slice whose footprint (brim included) touches the
center circle (centre_keepout_mm, 30 mm radius) when it could fit beside it is refused unless
--allow-center.
"""
import argparse, json, os, shutil, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import placement  # noqa: E402
import siteconf  # noqa: E402
import slicecheck  # noqa: E402
from estimate import flatten  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stl")
    ap.add_argument("out")
    ap.add_argument("--supports", required=True, choices=("off", "tree", "slim", "plate"))
    ap.add_argument("--brim", default="auto", choices=("auto", "outer", "none"))
    ap.add_argument("--nozzle", default="0.4")
    ap.add_argument("--quality")
    ap.add_argument("--filament")
    ap.add_argument("--allow-warnings", action="store_true")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override a process setting, e.g. --set sparse_infill_density=0%% (repeatable)")
    ap.add_argument("--keep-position", action="store_true",
                    help="slice the STL where it is (plate mm, origin front-left)")
    ap.add_argument("--place", metavar="X,Y", help="put the footprint's center at plate mm X,Y (origin front-left)")
    ap.add_argument("--allow-center", action="store_true", help="allow a footprint on the worn plate center")
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
        if a.supports == "slim":
            proc.update(support_style="tree_slim")
    for kv in a.set:
        k, _, v = kv.partition("=")
        if k not in proc:
            sys.exit("unknown process setting: %s" % k)
        proc[k] = v
    if a.brim == "outer":
        proc.update(brim_type="outer_only", brim_width="5")
    elif a.brim == "none":
        proc.update(brim_type="no_brim")
    radius = siteconf.get("centre_keepout_mm") or 0
    state = siteconf.path("state_dir")
    with tempfile.TemporaryDirectory() as tmp:
        pj, out = os.path.join(tmp, "process.json"), os.path.join(tmp, "out.gcode.3mf")
        model, arrange, next_spot = os.path.abspath(a.stl), "1", None
        if a.keep_position:
            arrange = "0"
        else:
            tris = placement.load_tris(a.stl)
            if a.place:
                x, _, y = a.place.partition(",")
                spot = (float(x), float(y))
            else:
                spot, next_spot = placement.pick_spot(tris, radius, placement.next_start(state))
            if spot:
                moved = placement.move(tris, *spot)
                if not placement.on_plate(placement.bbox(moved)):
                    sys.exit("part runs off the plate at %g,%g" % spot)
                model, arrange = os.path.join(tmp, "placed.stl"), "0"
                placement.write_stl(model, moved)
            else:
                print("too big to keep off the plate center; the slicer arranges it")
        with open(pj, "w") as f:
            json.dump(proc, f)
        r = subprocess.run([siteconf.path("appimage"), "--slice", "0", "--arrange", arrange, "--orient", "0",
                            "--load-settings", "%s;%s" % (os.path.join(prof, machine + ".json"), pj),
                            "--load-filaments", os.path.join(prof, filament + ".json"),
                            "--export-3mf", out, model],
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
        b = placement.sliced_bbox(out)
        if b:
            print("on the plate: x %.0f-%.0f, y %.0f-%.0f mm (brim included; origin front-left)" % (b[0], b[2], b[1], b[3]))
        centre = placement.centre_problem(b, placement.PLATE, radius)
        if centre and not a.allow_center:
            sys.exit("not written: the print sits on the worn plate center (within %d mm); "
                     "use --place X,Y, or --allow-center to override" % radius)
        if next_spot is not None:
            placement.save_start(state, next_spot)
        shutil.copyfile(out, a.out)
        print("wrote", a.out)


if __name__ == "__main__":
    main()
