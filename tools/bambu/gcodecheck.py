"""Check a sliced .gcode.3mf has the printer's full start sequence.

A slice made with an incomplete machine profile gets a generic start block: no AMS load, so the
printer goes through the motions with an empty nozzle.
    python3 gcodecheck.py part.gcode.3mf
"""
import re, sys, zipfile

NEEDED = {"AMS filament load (M620 S)": re.compile(r"^\s*M620 S"),
          "bed leveling (G29)": re.compile(r"^\s*G29\b"),
          "nozzle heat (M109)": re.compile(r"^\s*M109\b")}


def start_problems(path, plate=1):
    """Messages for whatever the start block lacks; empty means complete."""
    try:
        with zipfile.ZipFile(path) as z, z.open("Metadata/plate_%d.gcode" % plate) as f:
            found, inside = set(), False
            for raw in f:
                ln = raw.decode("utf-8", "ignore")
                if ln.startswith("; EXECUTABLE_BLOCK_START"):
                    inside = True
                elif ln.startswith("; MACHINE_START_GCODE_END"):
                    break
                elif inside:
                    found.update(k for k, rx in NEEDED.items() if rx.match(ln))
            else:
                return ["No machine start G-code found."]
    except (OSError, KeyError, zipfile.BadZipFile):
        return ["No plate G-code found."]
    missing = [k for k in NEEDED if k not in found]
    return ["Start G-code is incomplete (missing %s): re-slice with the full printer profile."
            % ", ".join(missing)] if missing else []


if __name__ == "__main__":
    probs = start_problems(sys.argv[1])
    print("\n".join(probs) or "ok")
    sys.exit(1 if probs else 0)
