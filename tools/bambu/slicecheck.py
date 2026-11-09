"""The slicer's own warnings for a sliced .gcode.3mf, e.g. "floating cantilever ... enable support generation".

Bambu Studio shows these in the app; the CLI only writes them to result.json, so this re-slices the file's
model (with the file's own settings) in a temp dir and reads them back. A file exported as G-code only
(no model inside) can't be checked.
    python3 slicecheck.py part.gcode.3mf [appimage]
"""
import json, os, subprocess, sys, tempfile, zipfile


def has_model(path):
    """True if the 3mf carries mesh geometry the slicer can re-slice."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if any(n.startswith("3D/Objects/") for n in names):
                return True
            return b"<mesh" in z.read("3D/3dmodel.model")
    except (OSError, KeyError, zipfile.BadZipFile):
        return False


def read_result(result_json):
    """(warnings, error) from a slicer result.json."""
    try:
        with open(result_json) as f:
            j = json.load(f)
    except (OSError, ValueError):
        return [], "the slicer gave no result"
    if j.get("return_code") != 0:
        return [], j.get("error_string") or "slicer error %s" % j.get("return_code")
    return [p["warning_message"] for p in j.get("sliced_plates", []) if p.get("warning_message")], None


def slicer_warnings(path, appimage, timeout=300):
    """(warnings, error): the slicer's warnings for the file; error says why the check couldn't run."""
    if not has_model(path):
        return [], "the file has no model inside (exported as G-code only)"
    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run([appimage, "--slice", "0", "--outputdir", tmp, os.path.abspath(path)],
                           cwd=tmp, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return [], "the slicer check timed out"
        except OSError as e:
            return [], "slicer not found (%s)" % e
        return read_result(os.path.join(tmp, "result.json"))


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import siteconf
    warns, err = slicer_warnings(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else siteconf.path("appimage"))
    print("\n".join(warns) or ("not checked: " + err if err else "ok"))
    sys.exit(1 if warns or err else 0)
