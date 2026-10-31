"""Flatten Bambu Studio system profiles for the command-line slicer.

The CLI wants complete printer / process / filament settings, but the shipped profiles inherit from each other
("inherits": "fdm_process_common"...). This resolves each chain inside the BBL profile folder and writes one
complete JSON per profile.

    python3 profiles.py <BBL profile dir> <out dir> "Bambu Lab P2S 0.4 nozzle" "0.20mm Standard @BBL P2S" "Bambu PLA Basic @BBL P2S"

The BBL dir comes from the AppImage: `<AppImage> --appimage-extract 'resources/profiles/BBL*'`."""
import json, os, sys

KINDS = ("machine", "process", "filament")


def index(bbl):
    """name -> (kind, path) for every profile JSON in the BBL folder."""
    out = {}
    for kind in KINDS:
        for root, _, files in os.walk(os.path.join(bbl, kind)):
            for f in files:
                if f.endswith(".json"):
                    path = os.path.join(root, f)
                    try:
                        name = json.load(open(path)).get("name") or f[:-5]
                    except (ValueError, OSError):
                        continue
                    out.setdefault(name, (kind, path))
    return out


def flatten(name, profiles):
    kind, path = profiles[name]
    data = json.load(open(path))
    parent = data.get("inherits")
    merged = flatten(parent, profiles)[1] if parent else {}
    merged.update({k: v for k, v in data.items() if k != "inherits"})
    return kind, merged


def main(bbl, out, *names):
    profiles = index(bbl)
    os.makedirs(out, exist_ok=True)
    for name in names:
        kind, data = flatten(name, profiles)
        data["from"] = "system"
        path = os.path.join(out, "%s.json" % name)
        json.dump(data, open(path, "w"), indent=1)
        print(kind, path)


if __name__ == "__main__":
    main(*sys.argv[1:])
