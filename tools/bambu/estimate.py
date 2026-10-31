"""Sliced filament estimates for a project's print jobs.

    python3 estimate.py parts.toml [--force]

Slices each job in parts.toml with the station's slicer and profiles, then writes estimates.json and
print_requirements.md next to it. Unchanged jobs (same files, settings and slicer) are not re-sliced.
Multi-file jobs are sliced per file and summed, so AMS purge waste is not included.

parts.toml:
    title = "Train cane"

    [[job]]
    id = "landscape"
    name = "Landscape"
    files = ["cad/grass.stl", "cad/rock.stl"]   # relative to parts.toml
    colors = ["green", "green"]                  # optional, one per file, for the table
    qty = 1
    nozzle = "0.2"
    quality = "0.10mm Standard @BBL P2S 0.2 nozzle"
    filament = "Generic PLA @BBL P2S 0.2 nozzle"
    supports = "off"                             # off | tree
    notes = "..."

    [[pending]]
    name = "Bottom shell"
    reason = "variant depends on motor test"
"""
import hashlib, json, os, re, subprocess, sys, tempfile, tomllib, zipfile

import siteconf

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.expanduser("~/.local/share/print_station/estimates")


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def flatten(machine, process, filament):
    key = hashlib.md5("|".join([machine, process, filament]).encode()).hexdigest()[:10]
    d = os.path.join(siteconf.path("work_dir"), "prof", key)
    if not all(os.path.isfile(os.path.join(d, n + ".json")) for n in (machine, process, filament)):
        os.makedirs(d, exist_ok=True)
        subprocess.run([sys.executable, os.path.join(HERE, "profiles.py"), siteconf.path("profiles_dir"), d,
                        machine, process, filament], check=True, capture_output=True, text=True)
    return d


def slice_one(stl, job, appimage):
    """Grams and minutes for one STL, or raise."""
    machine = "Bambu Lab P2S %s nozzle" % job["nozzle"]
    prof = flatten(machine, job["quality"], job["filament"])
    with open(os.path.join(prof, job["quality"] + ".json")) as f:
        proc = json.load(f)
    proc.update(curr_bed_type="Textured PEI Plate", enable_support="0")
    if job.get("supports", "off") == "tree":
        proc.update(enable_support="1", support_type="tree(auto)")
    with tempfile.TemporaryDirectory() as tmp:
        pj, out = os.path.join(tmp, "process.json"), os.path.join(tmp, "out.gcode.3mf")
        json.dump(proc, open(pj, "w"))
        r = subprocess.run([appimage, "--slice", "0", "--arrange", "1", "--orient", "0",
                            "--load-settings", "%s;%s" % (os.path.join(prof, machine + ".json"), pj),
                            "--load-filaments", os.path.join(prof, job["filament"] + ".json"),
                            "--export-3mf", out, stl], capture_output=True, text=True, timeout=600)
        try:
            info = zipfile.ZipFile(out).read("Metadata/slice_info.config").decode()
        except (OSError, KeyError, zipfile.BadZipFile):
            raise RuntimeError("slice failed: " + (r.stderr or r.stdout).strip()[-300:])
    g = re.search(r'key="weight"\s+value="([\d.]+)"', info)
    t = re.search(r'key="prediction"\s+value="(\d+)"', info)
    if not g or float(g.group(1)) <= 0:
        raise RuntimeError("no filament weight in slice")
    return float(g.group(1)), int(t.group(1)) / 60 if t else None


def main(manifest, force=False):
    base = os.path.dirname(os.path.abspath(manifest))
    m = tomllib.load(open(manifest, "rb"))
    appimage = siteconf.path("appimage")
    slicer = os.path.basename(appimage)
    out_json = os.path.join(base, "estimates.json")
    old = json.load(open(out_json)) if os.path.isfile(out_json) else {}
    results = {}
    for job in m.get("job", []):
        parts = []
        for i, rel in enumerate(job["files"]):
            stl = os.path.join(base, rel)
            settings = {k: job.get(k) for k in ("nozzle", "quality", "filament", "supports")}
            key = hashlib.sha256(json.dumps([sha(stl), settings, slicer]).encode()).hexdigest()
            prev = next((p for p in old.get(job["id"], {}).get("parts", []) if p.get("key") == key), None)
            if prev and not force:
                parts.append(prev)
                continue
            print("slicing", rel, file=sys.stderr)
            grams, minutes = slice_one(stl, job, appimage)
            parts.append({"file": rel, "color": (job.get("colors") or [None] * len(job["files"]))[i],
                          "grams": grams, "minutes": minutes, "key": key})
        results[job["id"]] = {"name": job["name"], "qty": job.get("qty", 1), "parts": parts,
                              "grams": sum(p["grams"] for p in parts) * job.get("qty", 1)}
    json.dump(results, open(out_json, "w"), indent=1)
    write_table(m, results, os.path.join(base, "print_requirements.md"), slicer)


def write_table(m, results, path, slicer):
    pending = m.get("pending", [])
    total = sum(r["grams"] for r in results.values())
    lines = ["# %s: print requirements" % m.get("title", "Project"), "",
             "Generated by `estimate.py`; edit `parts.toml`, not this file. Sliced estimates (%s), "
             "AMS purge excluded; multi-part jobs print together, so no per-part time." % slicer, "",
             "| Part | Qty | Settings | Colour | PLA (g) | Time |", "|---|---|---|---|---|---|"]
    notes = []
    for job in m.get("job", []):
        r = results[job["id"]]
        setting = "%s mm nozzle, %s" % (job["nozzle"], job["quality"].split(" @")[0])
        if job.get("supports", "off") != "off":
            setting += ", supports"
        multi = len(r["parts"]) > 1
        for p in r["parts"]:
            name = "%s: %s" % (r["name"], os.path.basename(p["file"])[:-4]) if multi else r["name"]
            g = p["grams"] * r["qty"]
            mins = None if multi or not p["minutes"] else round(p["minutes"])
            lines.append("| %s | %d | %s | %s | %s | %s |" % (name, r["qty"], setting, p["color"] or "",
                                                           "%.1f" % g if g >= 1 else "%.2f" % g,
                                                           "%dh%02d" % divmod(mins, 60) if mins else ""))
        if job.get("notes"):
            notes.append("- %s: %s" % (r["name"], job["notes"]))
    if notes:
        lines += [""] + notes
    lines += ["", "**Total: %.0f g PLA%s.**" % (total, " (partial)" if pending else "")]
    if pending:
        lines += ["", "Not yet included:"] + ["- %s: %s" % (p["name"], p["reason"]) for p in pending]
    open(path, "w").write("\n".join(lines) + "\n")
    print(path)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--force"]
    if len(args) != 1:
        sys.exit(__doc__)
    main(args[0], "--force" in sys.argv)
