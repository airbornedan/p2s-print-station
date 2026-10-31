"""Parts of a sliced plate, for skip-part.

The printer skips by G-code label id (the "; model label id:" header, also slice_info's identify_id),
not by the plate_N.json object id. Names come from slice_info when it lists objects (GUI slices),
else from plate_N.json in the same order (CLI slices). Positions are plate mm, origin front-left.
A plate without object labels (label_object_enabled false) can't be skipped and returns [].
"""
import html, json, re, zipfile

LABELS = re.compile(r"^; model label id: *([\d, ]+)$", re.M)


def read_parts(path, plate=1):
    """[{id, name, bbox}] for one plate of a .gcode.3mf; [] if it can't skip parts; None if not a sliced file."""
    gcode, pjson = "Metadata/plate_%d.gcode" % plate, "Metadata/plate_%d.json" % plate
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if gcode not in names:
            return None
        info = z.read("Metadata/slice_info.config").decode("utf-8", "ignore") if "Metadata/slice_info.config" in names else ""
        if re.search(r'key="label_object_enabled"\s+value="false"', info):
            return []
        with z.open(gcode) as g:
            head = g.read(65536).decode("utf-8", "ignore")
        layout = json.loads(z.read(pjson)) if pjson in names else {}
    m = LABELS.search(head)
    if not m:
        return []
    ids = [int(x) for x in re.split(r"[ ,]+", m.group(1).strip()) if x]
    by_id = {int(i): html.unescape(n) for i, n in re.findall(r'<object identify_id="(\d+)" name="([^"]*)"', info)}
    objs = layout.get("bbox_objects") or []
    if len(objs) != len(ids):
        objs = [{}] * len(ids)   # can't pair them up safely; no names or positions from the layout
    parts = []
    for n, (i, o) in enumerate(zip(ids, objs), 1):
        bbox = o.get("bbox")
        parts.append({"id": i, "name": by_id.get(i) or o.get("name") or "Part %d" % n,
                      "bbox": [round(v, 1) for v in bbox] if bbox and len(bbox) == 4 else None})
    return parts


def plate_number(gcode_file):
    """Plate index from the printer's gcode_file ('/data/Metadata/plate_2.gcode' -> 2); default 1."""
    m = re.search(r"plate_(\d+)\.gcode$", gcode_file or "")
    return int(m.group(1)) if m else 1
