"""Fixed SSH receive command. No paths or commands accepted from the client."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from schema import clean

ROOT = Path("/var/lib/printer-status")
EXPIRES = Path('/etc/printer-remote/expires')


def receive():
    raw = sys.stdin.buffer.read(32769)
    if len(raw) > 32768:
        raise ValueError("oversized document")
    doc = clean(json.loads(raw))
    now = time.time()
    if int(EXPIRES.read_text().strip()) <= now:
        raise ValueError("remote access is off")
    generated = doc["generated_at_unix"]
    if generated is None or not now - 120 <= generated <= now + 30:
        raise ValueError("invalid snapshot time")
    for key in ("observed_at_unix", "ams_observed_at_unix"):
        if doc[key] is not None and not generated - 60 <= doc[key] <= generated + 1:
            raise ValueError("invalid observation time")
    doc["received_at_unix"] = now
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=ROOT, delete=False) as f:
            temporary = f.name
            json.dump(doc, f, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
            os.fchmod(f.fileno(), 0o640)
        os.replace(temporary, ROOT / "snapshot.json")
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    try:
        receive()
    except Exception:
        raise SystemExit("Snapshot rejected")
