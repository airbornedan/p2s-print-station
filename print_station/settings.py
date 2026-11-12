"""Settings tab: a few site settings edited from the LAN page, saved into config.toml (comments and tables kept)."""
import os, re, threading

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

# key, label, type, min, max, help
FIELDS = [
    ("phone_alerts", "Phone alerts", bool, None, None, "ntfy pushes for errors, stalls and finished jobs"),
    ("stall_minutes", "Stall alert after", int, 5, 240, "minutes without a new layer"),
    ("delete_timelapse", "Delete timelapse videos", bool, None, None, "empty the USB stick's /timelapse at each new job"),
    ("stick_max_age_days", "Delete print files after", int, 1, 365, "days on the USB stick (running job kept)"),
    ("centre_keepout_mm", "Plate centre keep-out", int, 0, 120, "mm radius refused when the print fits beside it; 0 = off"),
]
_lock = threading.Lock()


def validate(updates):
    """{key: raw value} -> {key: typed value}; ValueError on unknown keys or bad values."""
    spec = {f[0]: f for f in FIELDS}
    out = {}
    for k, v in updates.items():
        if k not in spec:
            raise ValueError("unknown setting: %s" % k)
        _, label, typ, lo, hi, _ = spec[k]
        if typ is bool:
            if not isinstance(v, bool):
                raise ValueError("%s: expected on/off" % label)
        else:
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ValueError("%s: expected a whole number" % label)
            if not lo <= v <= hi:
                raise ValueError("%s: %d to %d" % (label, lo, hi))
        out[k] = v
    return out


def _toml(v):
    return ("true" if v else "false") if isinstance(v, bool) else str(v)


def apply_text(text, updates):
    """Set top-level keys in TOML text: replace an existing line, else add before the first [table]."""
    lines = text.splitlines()
    table = next((i for i, l in enumerate(lines) if re.match(r"\s*\[", l)), len(lines))
    new = []
    for k, v in updates.items():
        line = "%s = %s" % (k, _toml(v))
        hit = next((i for i in range(table) if re.match(r"\s*%s\s*=" % re.escape(k), lines[i])), None)
        if hit is None:
            new.append(line)
        else:
            lines[hit] = line
    if new:
        at = table
        while table < len(lines) and at > 0 and lines[at - 1].lstrip().startswith("#"):   # keep a table's comment with it
            at -= 1
        if at < len(lines):
            new.append("")
        lines[at:at] = new
    return "\n".join(lines) + "\n"


def save(path, updates):
    """Write updates into the config file atomically; refuses a result that does not parse."""
    path = os.path.expanduser(path)
    with _lock:
        text = open(path).read() if os.path.isfile(path) else ""
        out = apply_text(text, updates)
        if tomllib:
            tomllib.loads(out)
        tmp = path + ".tmp"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as f:
            f.write(out)
        if os.path.isfile(path):
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        os.replace(tmp, path)
