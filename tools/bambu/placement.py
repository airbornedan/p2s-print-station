"""Place a part on the P2S plate away from its worn center (parts printed there come loose).

Plate mm, origin front-left; with --arrange 0 the slicer keeps mesh XY as plate XY."""
import json, os, re, struct, sys, zipfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "print_station"))
from plate_centre import centre_problem  # noqa: E402

PLATE = 256
EDGE = 8        # mm kept clear of the plate edge (room for a brim)
SPOTS = [(64, 64), (192, 64), (192, 192), (64, 192)]   # quadrant centers, used in turn


def load_tris(path):
    data = open(path, "rb").read()
    if data[:5].lower() == b"solid" and b"facet" in data[:2000].lower():
        v = re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)
        return np.array(v, dtype=float).reshape(-1, 3, 3)
    n = struct.unpack("<I", data[80:84])[0]
    body = np.frombuffer(data[84:84 + n * 50], dtype=np.uint8).reshape(n, 50)
    return body[:, 12:48].copy().view("<f4").reshape(n, 3, 3).astype(float)


def write_stl(path, tris):
    rec = np.zeros(len(tris), dtype=[("nrm", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    rec["v"] = tris
    with open(path, "wb") as f:
        f.write(b"\0" * 80 + struct.pack("<I", len(tris)) + rec.tobytes())


def bbox(tris):
    xy = tris[:, :, :2].reshape(-1, 2)
    return [float(xy[:, 0].min()), float(xy[:, 1].min()), float(xy[:, 0].max()), float(xy[:, 1].max())]


def move(tris, cx, cy):
    """Copy of tris with the footprint's bbox center at plate (cx, cy) and its base on z 0."""
    x0, y0, x1, y1 = bbox(tris)
    out = tris.copy()
    out[:, :, 0] += cx - (x0 + x1) / 2
    out[:, :, 1] += cy - (y0 + y1) / 2
    out[:, :, 2] -= tris[:, :, 2].min()
    return out


def on_plate(b):
    return b[0] >= 0 and b[1] >= 0 and b[2] <= PLATE and b[3] <= PLATE


def pick_spot(tris, radius, start=0):
    """First quadrant spot, from `start` round, where the part sits clear of the center circle and
    EDGE mm inside the plate (the spot slides toward the plate middle as far as the part needs).
    None if the part is too big to keep off the center."""
    x0, y0, x1, y1 = bbox(tris)
    hw, hh = (x1 - x0) / 2, (y1 - y0) / 2
    for i in range(len(SPOTS)):
        sx, sy = SPOTS[(start + i) % len(SPOTS)]
        cx = min(max(sx, EDGE + hw), PLATE - EDGE - hw)
        cy = min(max(sy, EDGE + hh), PLATE - EDGE - hh)
        b = [cx - hw - 5, cy - hh - 5, cx + hw + 5, cy + hh + 5]   # 5 mm for a brim
        if hw * 2 + 2 * EDGE <= PLATE and hh * 2 + 2 * EDGE <= PLATE and not _touches(b, radius):
            return (cx, cy), (start + i + 1) % len(SPOTS)
    return None, start


def _touches(b, radius):
    c = PLATE / 2
    dx, dy = max(b[0] - c, 0, c - b[2]), max(b[1] - c, 0, c - b[3])
    return dx * dx + dy * dy < radius * radius


def sliced_bbox(path):
    """Footprint [x0, y0, x1, y1] of a sliced .gcode.3mf, brim included; None if unreadable."""
    try:
        with zipfile.ZipFile(path) as z:
            return json.loads(z.read("Metadata/plate_1.json")).get("bbox_all")
    except Exception:
        return None


def next_start(state_dir):
    try:
        return int(open(os.path.join(state_dir, "slice_spot")).read().strip()) % len(SPOTS)
    except (OSError, ValueError):
        return 0


def save_start(state_dir, i):
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, "slice_spot"), "w") as f:
        f.write(str(i))
