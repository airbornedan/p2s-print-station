"""One status-only MQTT observation, followed by a restricted snapshot upload."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from schema import clean

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "remote"))
import printer_remote

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "bambu"))
import p2s
import siteconf


def observe():
    # Nothing retained between runs: a progress update cannot freshen old AMS data.
    values = {}
    observed = ams_observed = None
    lock = threading.Lock()
    ready = threading.Event()

    def receive(client, userdata, message):
        nonlocal observed, ams_observed
        try:
            report = json.loads(message.payload).get("print", {})
            with lock:
                if "gcode_state" in report:
                    # State and progress must come from the same report.
                    for key in ("gcode_state", "mc_percent", "mc_remaining_time", "layer_num", "total_layer_num"):
                        values[key] = report.get(key)
                    observed = time.time()
                if isinstance(report.get("ams", {}).get("ams"), list):
                    values["ams"] = report["ams"]["ams"]
                    ams_observed = time.time()
                if observed and ams_observed:
                    ready.set()
        except (ValueError, TypeError, AttributeError):
            pass

    client = None
    try:
        client, serial = p2s.connect(receive)
        client.loop_start()
        p2s.request(client, serial, {"pushing": {"sequence_id": p2s.sequence_id(), "command": "pushall"}})
        ready.wait(12)
    except Exception:
        # No raw network exceptions or identifiers cross the public boundary.
        pass
    finally:
        if client:
            client.disconnect()
            client.loop_stop()
    slots = []
    for unit in values.get("ams", []):
        for tray in unit.get("tray", []):
            slots.append({"unit": unit.get("id"), "slot_index": tray.get("id"),
                          "material": tray.get("tray_type"), "name": tray.get("tray_sub_brands"),
                          "color": "#" + (tray.get("tray_color") or "")[:6],
                          "remaining_percent": tray.get("remain")})
    return clean({"generated_at_unix": time.time(), "observed_at_unix": observed,
                  "ams_observed_at_unix": ams_observed, "state": values.get("gcode_state"),
                  "progress_percent": values.get("mc_percent"), "remaining_minutes": values.get("mc_remaining_time"),
                  "layer": values.get("layer_num"), "total_layers": values.get("total_layer_num"), "ams": slots})


if __name__ == "__main__":
    if not printer_remote.is_on():
        raise SystemExit("Remote access is off; not observing or publishing")
    document = observe()
    if not printer_remote.is_on():
        raise SystemExit("Remote access expired; not publishing")
    result = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                             "-o", "IdentitiesOnly=yes", "-i", siteconf.path("status_key"),
                             siteconf.get("status_target")],
                            input=json.dumps(document), text=True, capture_output=True, timeout=25)
    if result.returncode:
        raise SystemExit("Status upload failed")
    print("Status snapshot published")
