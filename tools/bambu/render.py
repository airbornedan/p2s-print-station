"""Picture of a sliced Bambu print on its plate: iso view and top view, in the filament's colour, lit.

    python3 render.py plate_1.gcode out.png [#RRGGBB]   (or a sliced .3mf: its plate_1.gcode is read from the zip)

Reads Bambu Studio G-code (absolute XYZ, relative E), keeps only extruding moves, samples points along them and
rasterises all of them at once with NumPy into a depth buffer (nearest point wins each pixel). Light and shade come
from that depth buffer's slope (screen-space normals), so the relief reads without colouring by feature type. The
plate outline comes from the G-code's printable_area; the filament colour from filament_colour (or the argument)."""
import re, sys, time, zipfile
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
NUM = re.compile(rb"([XYZE])(-?\d*\.?\d+)")
BG, PLATE = (245, 245, 245), (120, 120, 120)
LIGHT = np.array([-0.45, -0.55, 0.70])          # from the upper left, toward the viewer
SIZE, MARGIN = 1000, 30


def read_gcode(path):
    if path.endswith(".3mf"):
        with zipfile.ZipFile(path) as z:
            return z.read(next(n for n in z.namelist() if n.endswith("plate_1.gcode")))
    return open(path, "rb").read()


def header(data, key):
    m = re.search(rb"^; " + key.encode() + rb" = (.*)$", data[:200000] if key != "printable_area" else data,
                  re.M)
    return m.group(1).decode().strip() if m else ""


def parse(data):
    """Extruding segments as float32 arrays: x0 y0 x1 y1 z."""
    x = y = z = 0.0
    xs0, ys0, xs1, ys1, zs = [], [], [], [], []
    for line in data.split(b"\n"):
        if line.startswith(b"G1 ") or line.startswith(b"G0 "):
            nx, ny, e = x, y, 0.0
            for k, v in NUM.findall(line.split(b";", 1)[0]):
                if k == b"X": nx = float(v)
                elif k == b"Y": ny = float(v)
                elif k == b"Z": z = float(v)
                else: e = float(v)
            if e > 0 and (nx != x or ny != y):
                xs0.append(x); ys0.append(y); xs1.append(nx); ys1.append(ny); zs.append(z)
            x, y = nx, ny
    f32 = lambda a: np.asarray(a, dtype=np.float32)
    return f32(xs0), f32(ys0), f32(xs1), f32(ys1), f32(zs)


def sample(x0, y0, x1, y1, z, step):
    """Points every `step` mm along each segment (at least the two ends)."""
    n = np.maximum(1, np.ceil(np.hypot(x1 - x0, y1 - y0) / step)).astype(np.int32)
    seg = np.repeat(np.arange(len(n)), n + 1)
    t = (np.arange(len(seg)) - np.repeat(np.cumsum(n + 1) - (n + 1), n + 1)) / np.repeat(n, n + 1)
    return x0[seg] + (x1[seg] - x0[seg]) * t, y0[seg] + (y1[seg] - y0[seg]) * t, z[seg]


def depth_buffer(px, py, depth, size):
    """Nearest depth per pixel (NaN where nothing is drawn)."""
    keep = (px >= 0) & (px < size) & (py >= 0) & (py < size)
    px, py, depth = px[keep], py[keep], depth[keep]
    order = np.argsort(depth, kind="stable")             # far first; the last write of a pixel wins
    buf = np.full((size, size), np.nan, dtype=np.float32)
    buf[py[order], px[order]] = depth[order]
    return buf


def shade(buf, mm_per_px, colour):
    """Lambert light from the depth buffer's slope; ambient keeps the shadows readable."""
    filled = np.where(np.isnan(buf), np.nanmin(buf) if np.isfinite(buf).any() else 0, buf)
    gy, gx = np.gradient(filled / mm_per_px)
    gx, gy = np.clip(gx, -4, 4), np.clip(gy, -4, 4)       # cliff edges at the silhouette stay bounded
    n = np.stack([-gx, gy, np.ones_like(gx)], axis=-1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    lit = np.clip(n @ (LIGHT / np.linalg.norm(LIGHT)), 0, 1)
    k = 0.38 + 0.62 * lit
    img = np.empty(buf.shape + (3,), dtype=np.uint8)
    img[:] = BG
    mask = ~np.isnan(buf)
    img[mask] = (np.array(colour, dtype=np.float32) * k[mask][:, None]).clip(0, 255).astype(np.uint8)
    return img


def plate_box(data):
    pts = [tuple(map(float, p.split("x"))) for p in header(data, "printable_area").split(",") if "x" in p]
    xs, ys = [p[0] for p in pts] or [0, 256], [p[1] for p in pts] or [0, 256]
    return min(xs), min(ys), max(xs), max(ys)


def top_view(x, y, z, box, colour):
    x0, y0, x1, y1 = box
    s = (SIZE - 2 * MARGIN) / max(x1 - x0, y1 - y0)
    px = ((x - x0) * s + MARGIN).astype(np.int32)
    py = ((y1 - y) * s + MARGIN).astype(np.int32)
    img = Image.fromarray(shade(depth_buffer(px, py, z, SIZE), 1 / s, colour))
    ImageDraw.Draw(img).rectangle([MARGIN, MARGIN, MARGIN + (x1 - x0) * s, MARGIN + (y1 - y0) * s],
                                  outline=PLATE, width=3)
    return img


def iso_view(x, y, z, box, colour):
    """From the front-left, 30 degrees up; framed on the whole plate."""
    a, e = np.radians(-45), np.radians(30)
    x0, y0, x1, y1 = box
    cxp, cyp = (x0 + x1) / 2, (y0 + y1) / 2

    def project(px, py, pz):
        cx, cy = px - cxp, py - cyp
        u = cx * np.cos(a) - cy * np.sin(a)
        v = cx * np.sin(a) + cy * np.cos(a)
        return u, pz * np.cos(e) + v * np.sin(e), -v * np.cos(e) + pz * np.sin(e)

    corners = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float32)
    cu, cv, _ = project(corners[:, 0], corners[:, 1], np.zeros(4))
    su, sv, depth = project(x, y, z)
    lo_u, hi_u = min(cu.min(), su.min()), max(cu.max(), su.max())
    lo_v, hi_v = min(cv.min(), sv.min()), max(cv.max(), sv.max())
    s = (SIZE - 2 * MARGIN) / max(hi_u - lo_u, hi_v - lo_v)
    off_v = (SIZE - 2 * MARGIN - (hi_v - lo_v) * s) / 2
    X = lambda u: ((u - lo_u) * s + MARGIN)
    Y = lambda v: ((hi_v - v) * s + MARGIN + off_v)
    buf = depth_buffer(X(su).astype(np.int32), Y(sv).astype(np.int32), depth, SIZE)
    img = Image.fromarray(shade(buf, 1 / s, colour))
    d = ImageDraw.Draw(img)
    pts = [(X(u), Y(v)) for u, v in zip(cu, cv)]
    d.line(pts + pts[:1], fill=PLATE, width=3)
    return img


def main(src, out, colour=None):
    t0 = time.time()
    data = read_gcode(src)
    x0, y0, x1, y1, z = parse(data)
    t1 = time.time()
    hexc = (colour or header(data, "filament_colour").split(";")[0] or "#C8C8C8").lstrip("#")
    rgb = tuple(int(hexc[i:i + 2], 16) for i in (0, 2, 4))
    box = plate_box(data)
    x, y, zz = sample(x0, y0, x1, y1, z, step=0.15)
    iso, top = iso_view(x, y, zz, box, rgb), top_view(x, y, zz, box, rgb)
    t2 = time.time()
    card = Image.new("RGB", (2000, 1125), BG)
    card.paste(iso, (0, 110)); card.paste(top, (1000, 110))
    d = ImageDraw.Draw(card)
    font, small = ImageFont.truetype(FONT, 28), ImageFont.truetype(FONT, 22)
    layers = len(np.unique(z))
    d.text((20, 18), "%s   %d layers, plate %g x %g mm" % (src.rsplit("/", 1)[-1], layers, box[2] - box[0], box[3] - box[1]),
           font=font, fill=(0, 0, 0))
    d.text((20, 58), "read %.1f s, draw %.1f s" % (t1 - t0, t2 - t1), font=small, fill=(90, 90, 90))
    d.text((1000 + MARGIN, 72), "TOP: front of the plate at the bottom", font=small, fill=(90, 90, 90))
    card.save(out)
    print("read %.2f s, draw %.2f s, %d layers, %d segments -> %s" % (t1 - t0, t2 - t1, layers, len(x0), out))


if __name__ == "__main__":
    main(*sys.argv[1:])
