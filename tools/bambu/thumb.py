"""Write plate thumbnails into a CLI-sliced 3MF, so the printer screen and Handy show a picture of the part.

    python3 thumb.py part.gcode.3mf [#RRGGBB]

The AppImage can't render its own (its GLFW is Wayland-only and GLEW needs a GLX display, so even Xvfb or a
headless Weston fails), so this draws the sliced G-code with render.py: iso view from the front-left, framed
on the part, transparent background, like Bambu Studio's. Writes Metadata/plate_1.png (512 px) and
plate_1_small.png (128 px), which the 3MF's _rels already point to, and adds thumbnail_file to the plate in
model_settings.config. Rewrites the 3MF in place."""
import io, os, re, sys, zipfile
import numpy as np
from PIL import Image
import render

SIZE, MARGIN = 512, 24
MAX_PX_PER_MM = 5   # finer than this, the gaps between sampled points and layers show


def picture(data, rgb):
    x0, y0, x1, y1, z = render.parse(data)
    x, y, zz = render.sample(x0, y0, x1, y1, z, step=0.15)
    a, e = np.radians(-45), np.radians(30)
    u = x * np.cos(a) - y * np.sin(a)
    v = x * np.sin(a) + y * np.cos(a)
    sv, depth = zz * np.cos(e) + v * np.sin(e), -v * np.cos(e) + zz * np.sin(e)
    s = (SIZE - 2 * MARGIN) / max(np.ptp(u), np.ptp(sv), 1e-6)
    n = SIZE if s <= MAX_PX_PER_MM else int(np.ceil(SIZE * MAX_PX_PER_MM / s))   # small parts: draw small, scale up
    s *= n / SIZE
    px = ((u - (u.min() + u.max()) / 2) * s + n / 2).astype(np.int32)
    py = (((sv.min() + sv.max()) / 2 - sv) * s + n / 2).astype(np.int32)
    buf = render.depth_buffer(px, py, depth, n)
    rgba = np.dstack([render.shade(buf, 1 / s, rgb), np.where(np.isnan(buf), 0, 255).astype(np.uint8)])
    return Image.fromarray(rgba, "RGBA").resize((SIZE, SIZE), Image.LANCZOS)


def png(img, size):
    out = io.BytesIO()
    (img if size == SIZE else img.resize((size, size), Image.LANCZOS)).save(out, "PNG")
    return out.getvalue()


def main(path, colour=None):
    with zipfile.ZipFile(path) as z:
        items = [(i, z.read(i.filename)) for i in z.infolist()]
    data = next(b for i, b in items if i.filename == "Metadata/plate_1.gcode")
    hexc = (colour or render.header(data, "filament_colour").split(";")[0] or "#C8C8C8").lstrip("#")
    img = picture(data, tuple(int(hexc[i:i + 2], 16) for i in (0, 2, 4)))
    new = {"Metadata/plate_1.png": png(img, SIZE), "Metadata/plate_1_small.png": png(img, 128)}

    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for i, b in items:
            if i.filename in new:
                continue
            if i.filename == "Metadata/model_settings.config" and b"thumbnail_file" not in b:
                b = re.sub(rb'(\s*)(<metadata key="gcode_file" value="Metadata/plate_1.gcode"/>)',
                           rb'\1\2\1<metadata key="thumbnail_file" value="Metadata/plate_1.png"/>', b, count=1)
            z.writestr(i, b)
        for name, b in new.items():
            z.writestr(name, b)
    os.replace(tmp, path)
    print("thumbnails -> %s" % path)


if __name__ == "__main__":
    main(*sys.argv[1:])
