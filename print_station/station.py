"""Print station — LAN web UI for the Bambu P2S.

Monitor (status, cameras, pause/resume/stop/light, skip part) and Inbox (place, slice, preview, send & start).
Control commands need the printer in LAN + Developer mode; pass the code:
    P2S_ACCESS_CODE=<lan code> python3 station.py
then open http://<this-box>:8078/ on the LAN. Port 8079 (localhost only) is the remote listener for
the web-server tunnel: everything arriving there is treated as remote (see remote/printer_remote.py).
Site settings (printer IP, paths, remote hosts) come from config.toml; see tools/bambu/siteconf.py.

A single long-lived MQTT client keeps the latest report in memory, so the page polls a
local endpoint and never waits on the printer.
"""
import json, os, re, shutil, subprocess, sys, threading, time
import urllib.request
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "bambu"))
import p2s  # noqa: E402
import siteconf  # noqa: E402
import gcodecheck  # noqa: E402
import slicecheck  # noqa: E402
from plate_watchdog import PrintMonitor
from job_alerts import JobAlerts
from status_cache import StatusCache
from camera_relay import CameraHub, FFmpegReader, WebcamReader
import parts as plate_parts
from stick_cleanup import StickCleanup
import plate_centre

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "remote"))
import printer_remote  # noqa: E402

from flask import Flask, jsonify, request, Response  # noqa: E402

PORT = siteconf.get("port")
REMOTE_PORT = siteconf.get("remote_port")   # tunnel target; always remote
PRINT_INBOX = siteconf.path("inbox")
CAM_W = siteconf.get("camera_width")  # downscale the 1080p stream for the page

# ---- live printer state, kept fresh by one persistent MQTT client -------------------------

_state = {"print": {}, "connected": False, "error": None, "updated": 0.0, "ams_updated": 0.0}
_conn = {"c": None, "sn": None}   # live MQTT client, so a send can ask for a fresh report
_lock = threading.Lock()
_reconnect = threading.Event()   # set to force the MQTT loop to drop and reconnect (e.g. new LAN code)


_print_monitor = PrintMonitor()


def _ntfy(msg, pri=4):
    """Phone alert through ntfy.sh; the topic lives in a private file (siteconf ntfy_topic_file)."""
    try:
        topic = open(siteconf.path("ntfy_topic_file")).read().strip()
    except OSError:
        return
    def send():
        try:
            req = urllib.request.Request("https://ntfy.sh/" + topic, data=msg.encode(),
                                         headers={"Title": "Printer", "Priority": str(pri)})
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:
            print("ntfy failed:", e, file=sys.stderr)
    threading.Thread(target=send, daemon=True).start()


_job_alerts = JobAlerts(_ntfy, stall_min=siteconf.get("stall_minutes"))


def _job_alert_ticker():
    while True:
        time.sleep(30)
        try:
            _job_alerts.tick()
        except Exception as e:
            print("job alert tick failed:", e, file=sys.stderr)


threading.Thread(target=_job_alert_ticker, daemon=True).start()
_mobile_status = StatusCache()


def _on_message(c, u, m):
    try:
        p = json.loads(m.payload).get("print", {})
    except Exception:
        return
    if not p:
        return
    with _lock:
        _state["print"].update(p)        # reports are incremental; merge
        _state["updated"] = time.time()
        if "ams" in p:
            _state["ams_updated"] = _state["updated"]
    # Monitor consumes raw reports, never stale fields from the merged UI snapshot.
    _mobile_status.observe(p)
    _print_monitor.observe(p)
    _job_alerts.observe(p)
    _stick_cleanup.observe(p)


def _mqtt_loop():
    """Connect once, keep the state fresh, re-poll periodically, reconnect on drop or on request."""
    while True:
        c = None
        try:
            c, sn = p2s.connect(_on_message)
            c.loop_start()
            _conn.update(c=c, sn=sn)
            _mobile_status.connection(True)
            with _lock:
                _state["connected"], _state["error"] = True, None
            p2s.request(c, sn, {"pushing": {"sequence_id": "0", "command": "pushall"}})
            while True:
                # the printer pushes incremental updates while printing; a periodic pushall
                # keeps temps/idle state current and detects a dead link. Wake early if a new
                # LAN code was entered so we reconnect with it.
                _mobile_status.connection(c.is_connected())
                if _reconnect.wait(4):
                    _reconnect.clear()
                    break
                p2s.request(c, sn, {"pushing": {"sequence_id": "0", "command": "pushall"}})
        except Exception as e:
            _conn.update(c=None, sn=None)
            _mobile_status.connection(False)
            with _lock:
                _state["connected"], _state["error"] = False, str(e)
            time.sleep(5)  # access code may have changed on a mode switch; retry
        finally:
            _mobile_status.connection(False)
            try:
                if c:
                    c.loop_stop(); c.disconnect()
            except Exception:
                pass


def _send_command(payload, wait=5):
    """Publish one command on a short-lived connection; return the printer's reply to it ({} if none)."""
    reply = {}
    done = threading.Event()
    seq = p2s.sequence_id()
    next(iter(payload.values()))["sequence_id"] = seq

    def on_msg(c, u, m):
        d = json.loads(m.payload)
        for sect in ("print", "system"):
            s = d.get(sect, {})
            if s.get("sequence_id") == seq and "result" in s:   # the reply to this command, not any other
                reply.update({"command": s.get("command"), "result": s.get("result")})
                done.set()

    c, sn = p2s.connect(on_msg)
    c.loop_start()
    p2s.request(c, sn, payload)
    done.wait(wait)
    c.loop_stop(); c.disconnect()
    return reply


def _control(cmd):
    """Send one control command; return (ok, reply)."""
    payloads = {
        "stop":   {"print": {"command": "stop", "param": ""}},
        "pause":  {"print": {"command": "pause", "param": ""}},
        "resume": {"print": {"command": "resume", "param": ""}},
        "light_on":  {"system": {"command": "ledctrl", "led_node": "chamber_light", "led_mode": "on",
                                 "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}},
        "light_off": {"system": {"command": "ledctrl", "led_node": "chamber_light", "led_mode": "off",
                                 "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}},
    }
    if cmd not in payloads:
        return False, {"error": "unknown command"}
    reply = _send_command(payloads[cmd])
    if not reply:
        return False, {"error": "no acknowledgement (printer may be in cloud mode; control needs LAN + Developer)"}
    return (reply.get("result") or "").upper() == "SUCCESS", reply


# ---- camera: RTSP-over-TLS (port 322, LAN + Developer mode) relayed to the browser as MJPEG ----

def _cam_cmd(width=CAM_W, fps=10):
    """width None keeps the native 1080p."""
    url = "rtsps://bblp:%s@%s:%d/streaming/live/1" % (p2s.code(), p2s.IP, p2s.CAM_PORT)
    scale = ["-vf", "scale=%d:-2" % width] if width else []
    return ["ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp", "-timeout", "5000000", "-i", url] + scale + [
            "-r", str(fps), "-q:v", "6",
            "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]


def _log_redacted(pipe):
    """ffmpeg errors to the journal, with the access code (part of the camera URL) masked."""
    for line in iter(pipe.readline, b""):
        text = line.decode("utf-8", "replace").rstrip()
        code = p2s.code()
        print("camera:", text.replace(code, "***") if code else text, file=sys.stderr, flush=True)


# One reader serves every viewer at the local page's rate; remote and mobile viewers take 1 fps from it.
_cameras = CameraHub({
    "bambu": lambda: FFmpegReader(_cam_cmd(), log=_log_redacted),
    "webcam": lambda: WebcamReader(siteconf.get("webcam_url")),
})

# Notification snapshots also reuse this reader instead of opening a second RTSP connection.
_print_monitor.camera = _cameras.snapshot
_mobile_push = None
_mobile_devices = None
if siteconf.get("mobile_enabled"):
    from mobile.auth import Devices
    from mobile.push import PushService
    _mobile_devices = Devices()
    _mobile_push = PushService(_mobile_devices, origin=siteconf.get("mobile_origin"))
    _print_monitor.notify = _mobile_push.deliver



# ---- web ----------------------------------------------------------------------------------

app = Flask(__name__)

# ---- remote access (<server>/printer/, switched by remote/printer_remote.py) ----------------
# The droplet's Apache checks the IP allowlist and password and sets X-Print-Remote. Remote
# viewers get monitor + pause/resume/stop/skip part/light only; everything else stays LAN-only.
REMOTE_OK = {("GET", "/"), ("GET", "/cam.mjpeg"), ("GET", "/api/camera-health"), ("GET", "/api/status"), ("GET", "/api/remote"),
             ("POST", "/api/remote/extend"), ("POST", "/api/control/pause"), ("POST", "/api/control/resume"),
             ("POST", "/api/control/stop"), ("POST", "/api/skip"), ("POST", "/api/control/light_on"), ("POST", "/api/control/light_off")}


def _is_remote():
    # The tunnel lands on REMOTE_PORT, so anything there is remote whatever headers it carries
    # (a process on the droplet can reach the tunnel without going through Apache).
    return request.environ.get("SERVER_PORT") == str(REMOTE_PORT) or request.headers.get("X-Print-Remote") == "1"


@app.before_request
def _same_origin_only():
    """Refuse posts from other sites' pages (a browser on the LAN can be made to post here).
    Browsers send Origin on cross-site posts; scripts and curl send none."""
    origin = request.headers.get("Origin")
    if request.method != "POST" or not origin:
        return None
    hosts = {request.host, request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()}
    if urlparse(origin).netloc not in hosts:
        return jsonify({"ok": False, "error": "cross-site request refused"}), 403
    return None


@app.before_request
def _remote_gate():
    if not _is_remote():
        return None
    if not printer_remote.is_on():
        return jsonify({"ok": False, "error": "remote access is off"}), 403
    # Apache appends the real client IP last; re-check it against the allowlist.
    client = request.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
    allowed = printer_remote.allowed_ips()
    if "any" not in allowed and client not in allowed:
        return jsonify({"ok": False, "error": "address not allowed"}), 403
    if (request.method, request.path) not in REMOTE_OK:
        return jsonify({"ok": False, "error": "not available remotely"}), 403
    return None


@app.get("/api/remote")
def api_remote():
    return jsonify({"remote": _is_remote(), "expires": printer_remote.expires()})


@app.post("/api/remote/extend")
def api_remote_extend():
    try:
        return jsonify({"ok": True, "expires": printer_remote.extend(1)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.get("/cam.mjpeg")
def cam_mjpeg():
    """?src=bambu (printer camera, default) or webcam. Remote viewers get one full-size frame a second."""
    import uuid
    source = request.args.get("src", "bambu")
    if source not in ("bambu", "webcam"):
        return jsonify(error="Unknown camera"), 400
    owner = "desktop-" + uuid.uuid4().hex
    try:
        key = _cameras.acquire(owner, source)
    except ValueError:
        return jsonify(error="Too many viewers"), 429
    gen = _cameras.frames(key, owner, fps=1 if _is_remote() else (10 if source == "bambu" else 5), auto_renew=True)
    return Response(gen, mimetype="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/camera-health")
def api_camera_health():
    return jsonify({src: _cameras.health(src) for src in ("bambu", "webcam")})


@app.post("/api/code")
def api_code():
    code = request.values.get("code") or (request.get_json(silent=True) or {}).get("code") or ""
    code = code.strip()
    if not code:
        return jsonify({"ok": False, "error": "empty code"}), 400
    os.environ["P2S_ACCESS_CODE"] = code
    _reconnect.set()   # reconnect MQTT with the new code; camera uses it on next open
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    with _lock:
        p = dict(_state["print"])
        meta = {"connected": _state["connected"], "error": _state["error"],
                "age": round(time.time() - _state["updated"], 1) if _state["updated"] else None}
    ams = []
    for unit in (p.get("ams") or {}).get("ams", []):
        for t in unit.get("tray", []):
            col = (t.get("tray_color") or "")[:6]
            typ = t.get("tray_type") or ""
            sub = (t.get("tray_sub_brands") or "").strip()
            if sub:
                name = "Bambu " + sub                 # RFID-tagged spool, e.g. "Bambu PLA Matte"
            elif typ:
                name = "Generic " + typ               # untagged spool, only the material is known
            else:
                name = "empty"
            ams.append({"unit": unit.get("id"), "slot": t.get("id"), "type": typ or None,
                        "idx": t.get("tray_info_idx"), "name": name, "color": "#" + col if col else None,
                        "remain": t.get("remain")})
    stage = _mobile_status.snapshot()
    out = {
        "stage": stage["stage"], "stage_code": stage["stage_code"],
        "meta": meta,
        "state": p.get("gcode_state"), "file": p.get("subtask_name"),
        "percent": p.get("mc_percent"), "remaining": p.get("mc_remaining_time"),
        "layer": p.get("layer_num"), "total_layer": p.get("total_layer_num"),
        "nozzle": p.get("nozzle_temper"), "nozzle_target": p.get("nozzle_target_temper"),
        "bed": p.get("bed_temper"), "bed_target": p.get("bed_target_temper"),
        "nozzle_size": p.get("nozzle_diameter"), "sdcard": p.get("sdcard"),
        "usb_free_mb": _usb_free_mb(p), "recording": _recording_on(p),
        "lights": [l.get("mode") for l in (p.get("lights_report") or []) if l.get("node") == "chamber_light"],
        "print_error": p.get("print_error"), "speed": p.get("spd_lvl"), "ams": ams,
        "autoslow": _autoslow_get(),
        "parts": _skip_view(p),
    }
    return jsonify(out)


SPEED_NAMES = {1: "Silent", 2: "Normal", 3: "Sport", 4: "Ludicrous"}


@app.post("/api/speed/<int:lvl>")
def api_speed(lvl):
    """Set print speed level (1 Silent, 2 Normal, 3 Sport, 4 Ludicrous); confirm via spd_lvl."""
    if lvl not in SPEED_NAMES:
        return jsonify({"ok": False, "error": "level must be 1-4"}), 400
    ok, spd = _set_speed(lvl)
    return jsonify({"ok": ok, "speed": spd}), (200 if ok else 502)


def _set_speed(lvl):
    got = {}

    def on_msg(c, u, m):
        p = json.loads(m.payload).get("print", {})
        if "spd_lvl" in p:
            got["spd_lvl"] = p["spd_lvl"]
    c, sn = p2s.connect(on_msg)
    c.loop_start()
    p2s.request(c, sn, {"print": {"sequence_id": "1", "command": "print_speed", "param": str(lvl)}})
    time.sleep(1.5)
    p2s.request(c, sn, {"pushing": {"sequence_id": "2", "command": "pushall"}})
    time.sleep(2)
    c.loop_stop(); c.disconnect()
    return got.get("spd_lvl") == lvl, got.get("spd_lvl")


# ---- auto-slow: tall, narrow prints run slower (speed and acceleration) above a set layer ----
# Baked into the G-code at send time. The visible walls run slow from the first layer, so the
# surface looks the same all the way up; everything else slows from the chosen layer.

AUTOSLOW = os.path.join(siteconf.path("state_dir"), "autoslow.json")
SLENDER = 3.0      # slow only if height > 3 x the narrower side of the first-layer footprint
SLOW_FROM = 2.5    # ... starting where the print reaches 2.5 x that width
SLOW_SPEED = 0.5   # feedrate factor (the printer's Silent level)
SLOW_ACCEL = 0.5   # acceleration factor
VISIBLE = ("Outer wall", "Overhang wall")   # slow on every layer


def _slow_line(line, fs, fa):
    """Scale F on a move and S on M204 by the given factors."""
    def sub(m, f):
        return m.group(1) + ("%d" % round(float(m.group(2)) * f))
    if re.match(r"G[0-3] ", line) or line.startswith("G1 F"):
        return re.sub(r"(\bF)([0-9.]+)", lambda m: sub(m, fs), line)
    if line.startswith("M204 S"):
        return re.sub(r"(\bS)([0-9.]+)", lambda m: sub(m, fa), line)
    return line


def _slow_gcode(src, dst, layer):
    """Copy a sliced .gcode.3mf with the auto-slow applied from `layer` (printer layer numbering)."""
    import hashlib, zipfile
    with zipfile.ZipFile(src) as z:
        lines = z.read("Metadata/plate_1.gcode").decode("utf-8").split("\n")
    cur, feature, body = 0, "", False
    for i, ln in enumerate(lines):
        if ln.startswith("M73 L"):
            cur, body = int(ln[5:].split()[0]), True
        elif ln.startswith("; FEATURE: "):
            feature = ln[11:].strip()
        elif ln.startswith("; MACHINE_END_GCODE_START"):
            break
        elif body and (feature in VISIBLE or cur >= layer):
            lines[i] = _slow_line(ln, SLOW_SPEED, SLOW_ACCEL)
    g = "\n".join(lines).encode("utf-8")
    with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
        for it in zi.infolist():
            if it.filename == "Metadata/plate_1.gcode":
                zo.writestr(it, g)
            elif it.filename == "Metadata/plate_1.gcode.md5":
                zo.writestr(it, hashlib.md5(g).hexdigest().upper())
            else:
                with zi.open(it) as f, zo.open(it, "w") as o:
                    shutil.copyfileobj(f, o, 1 << 20)


def _autoslow_suggest(stl, first_h, layer_h):
    """From the placed mesh: should this job slow down, and from which layer/height."""
    import numpy as np
    v = _load_tris(stl).reshape(-1, 3)
    z0 = v[:, 2].min()
    base = v[v[:, 2] < z0 + 0.3]               # what touches the bed (fin foot included, brim not)
    width = float(min(np.ptp(base[:, 0]), np.ptp(base[:, 1])))
    height = float(v[:, 2].max() - z0)
    z = SLOW_FROM * width
    layer = 1 + max(0, int(np.ceil((z - first_h) / layer_h)))
    return {"suggest": height > SLENDER * width and z < height, "layer": layer, "z": round(z),
            "width": round(width), "height": round(height)}


def _autoslow_get():
    try:
        with open(AUTOSLOW) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _autoslow_set(job, layer):
    os.makedirs(os.path.dirname(AUTOSLOW), exist_ok=True)
    with open(AUTOSLOW, "w") as f:
        json.dump({"job": job, "layer": int(layer), "done": False}, f)


def _autoslow_loop():
    """Track the slowed job for the status line: mark it once it reaches its slow layer; clear it when it ends."""
    while True:
        time.sleep(5)
        a = _autoslow_get()
        if not a or a.get("done"):
            continue
        with _lock:
            p = dict(_state["print"])
        if p.get("subtask_name") != a["job"]:
            continue
        if p.get("gcode_state") in ("FINISH", "FAILED", "IDLE"):
            os.remove(AUTOSLOW)                  # job over before reaching the layer
            continue
        if (p.get("layer_num") or 0) >= a["layer"]:
            a["done"] = True
            with open(AUTOSLOW, "w") as f:
                json.dump(a, f)


@app.post("/api/control/<cmd>")
def api_control(cmd):
    ok, reply = _control(cmd)
    return jsonify({"ok": ok, "reply": reply}), (200 if ok else 502)


# ---- skip part: the running job's parts, read once per job from its file on the USB stick ----
import ftps  # noqa: E402

SKIPPABLE_STATES = {"RUNNING", "PAUSE"}
PARTS_RETRY = 60   # seconds before retrying a failed fetch
_parts = {"key": None, "parts": None, "busy": False, "retry_at": 0.0}
_parts_lock = threading.Lock()
_ftps_lock = threading.Lock()   # one FTPS session at a time from this process
_stick_cleanup = StickCleanup(ftps.connect, _ftps_lock, timelapse=siteconf.get("delete_timelapse"))


def _job_key(p):
    return (p.get("task_id"), p.get("subtask_name"), plate_parts.plate_number(p.get("gcode_file")))


def _fetch_parts(key):
    _, name, plate = key
    dest = os.path.join(WORK, "running_job.gcode.3mf")
    result, retry_at = None, 0.0
    try:
        os.makedirs(WORK, exist_ok=True)
        with _ftps_lock:
            ftps.download(name + ".gcode.3mf", dest, timeout=60)
        result = plate_parts.read_parts(dest, plate) or []
    except Exception as e:
        print("skip part: can't read the job's parts:", type(e).__name__, e, file=sys.stderr, flush=True)
        retry_at = time.time() + PARTS_RETRY
    finally:
        try:
            os.remove(dest)
        except OSError:
            pass
    with _parts_lock:
        if _parts["key"] == key:
            _parts.update(parts=result, busy=False, retry_at=retry_at)


def _running_parts(p):
    """Parts of the running or paused job ([{id, name, bbox}]), or None while unknown or not printing."""
    if p.get("gcode_state") not in SKIPPABLE_STATES or not p.get("subtask_name"):
        return None
    key = _job_key(p)
    with _parts_lock:
        if _parts["key"] == key and (_parts["busy"] or _parts["parts"] is not None or time.time() < _parts["retry_at"]):
            return _parts["parts"]
        _parts.update(key=key, parts=None, busy=True, retry_at=0.0)
    threading.Thread(target=_fetch_parts, args=(key,), daemon=True).start()
    return None


def _skip_view(p):
    """Parts for the page, with skipped flags; None unless the job has two or more skippable parts."""
    parts = _running_parts(p)
    if not parts or len(parts) < 2:
        return None
    skipped = set(p.get("s_obj") or [])
    return [dict(x, skipped=x["id"] in skipped) for x in parts]


@app.post("/api/skip")
def api_skip():
    j = request.get_json(force=True, silent=True) or {}
    ids = j.get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        return jsonify({"ok": False, "error": "no parts chosen"}), 400
    with _lock:
        p = dict(_state["print"])
    view = _skip_view(p)
    if not view:
        return jsonify({"ok": False, "error": "no multi-part print running"}), 409
    live = {x["id"] for x in view if not x["skipped"]}
    ids = sorted(set(ids))
    if not set(ids) <= live:
        return jsonify({"ok": False, "error": "unknown or already skipped part"}), 400
    if set(ids) == live:
        return jsonify({"ok": False, "error": "that would skip every part left; use Stop instead"}), 400
    already = sorted(set(p.get("s_obj") or []))
    reply = _send_command({"print": {"command": "skip_objects", "obj_list": already + ids}})
    # The printer's skipped list (s_obj) is the proof; the command reply alone isn't.
    deadline = time.time() + 10
    while time.time() < deadline:
        if _conn["c"]:
            p2s.request(_conn["c"], _conn["sn"], {"pushing": {"sequence_id": "0", "command": "pushall"}})
        time.sleep(1.5)
        with _lock:
            now = set(_state["print"].get("s_obj") or [])
        if set(ids) <= now:
            return jsonify({"ok": True, "skipped": sorted(now), "reply": reply})
    err = "printer did not confirm the skip"
    if not reply:
        err += " (no acknowledgement; control needs LAN + Developer mode)"
    return jsonify({"ok": False, "error": err, "reply": reply}), 502


def _sliced_info(path):
    """Grams and minutes baked into a sliced .gcode.3mf, else (None, None)."""
    import re as _re, zipfile
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            g = t = None
            cfg = next((n for n in names if n.endswith("slice_info.config")), None)
            if cfg:
                s = z.read(cfg).decode("utf-8", "ignore")
                mw = _re.search(r'key="weight"\s+value="([\d.]+)"', s)
                mp = _re.search(r'key="prediction"\s+value="(\d+)"', s)
                g = round(float(mw.group(1)), 1) if mw else None
                t = round(int(mp.group(1)) / 60) if mp else None
            return g, t
    except Exception:
        return None, None


def _inbox_path(name):
    """Full path of a file directly in the inbox, or None (no subfolders, no '..', no absolute paths)."""
    if not name or name != os.path.basename(name):
        return None
    full = os.path.realpath(os.path.join(PRINT_INBOX, name))
    return full if os.path.dirname(full) == os.path.realpath(PRINT_INBOX) and os.path.isfile(full) else None


@app.get("/api/inbox")
def api_inbox():
    os.makedirs(PRINT_INBOX, exist_ok=True)
    items = []
    for name in sorted(os.listdir(PRINT_INBOX)):
        full = os.path.join(PRINT_INBOX, name)
        if not os.path.isfile(full):
            continue
        low = name.lower()
        if low.endswith(".gcode.3mf"):
            kind = "sliced"
        elif low.endswith(".stl"):          # placement needs the STL mesh
            kind = "model"
        else:
            continue
        st = os.stat(full)
        row = {"name": name, "kind": kind, "size": st.st_size, "mtime": st.st_mtime}
        if kind == "sliced":
            row["grams"], row["minutes"] = _sliced_info(full)
        items.append(row)
    return jsonify({"inbox": PRINT_INBOX, "items": items})


# ---- slice-profile catalog (extracted BBL profiles) and model footprint --------------------

BBL_DIR = siteconf.path("profiles_dir")
PLATE_MM = 256  # P2S build plate is 256 x 256 mm
CENTRE_MM = siteconf.get("centre_keepout_mm")  # worn plate centre: refuse prints there that could sit aside


def _names(kind):
    d = os.path.join(BBL_DIR, kind)
    if not os.path.isdir(d):
        return []
    return sorted(n[:-5] for n in os.listdir(d) if n.endswith(".json"))


def _fits(name, nozzle):
    """A P2S profile with no nozzle suffix is the 0.4 default; others name their nozzle."""
    import re as _re
    if name.endswith("@BBL P2S"):
        return nozzle == "0.4"
    m = _re.search(r"@BBL P2S (0\.\d) nozzle$", name)
    return bool(m) and m.group(1) == nozzle


@app.get("/api/profiles")
def api_profiles():
    import re as _re
    nozzle = request.args.get("nozzle", "0.4")
    machines = [n for n in _names("machine") if "P2S" in n]
    noz = sorted({m.group(1) for n in machines for m in [_re.search(r"P2S (0\.\d) nozzle", n)] if m} | {"0.4"})
    qualities = [n for n in _names("process") if "P2S" in n and _fits(n, nozzle)]
    filaments = [n for n in _names("filament") if "P2S" in n and _fits(n, nozzle)]
    return jsonify({"nozzle": nozzle, "nozzles": noz, "qualities": qualities, "filaments": filaments})


def _hull(xy):
    """Convex hull (monotone chain) of Nx2 points, returned as a list of [x,y]."""
    pts = sorted(set(map(tuple, xy.round(2).tolist())))
    if len(pts) < 3:
        return [list(p) for p in pts]
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo = []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    up = []
    for p in reversed(pts):
        while len(up) >= 2 and cross(up[-2], up[-1], p) <= 0:
            up.pop()
        up.append(p)
    return [list(p) for p in lo[:-1] + up[:-1]]


def _stl_footprint(path):
    import struct, re as _re
    import numpy as np
    data = open(path, "rb").read()
    if data[:5].lower() == b"solid" and b"facet" in data[:2000].lower():
        v = _re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)
        pts = np.array(v, dtype=float) if v else np.zeros((0, 3))
    else:
        n = struct.unpack("<I", data[80:84])[0]
        body = np.frombuffer(data[84:84 + n * 50], dtype=np.uint8).reshape(n, 50)
        pts = body[:, 12:48].copy().view("<f4").reshape(-1, 3)
    if not len(pts):
        return None
    xy = pts[:, :2]
    return {
        "bbox": [float(xy[:, 0].min()), float(xy[:, 1].min()), float(xy[:, 0].max()), float(xy[:, 1].max())],
        "hull": _hull(xy),
        "dz": round(float(pts[:, 2].max() - pts[:, 2].min()), 1),
        "plate": PLATE_MM,
    }


OVERHANG_DEG = 30      # faces within 30 degrees of flat, facing down, off the bed, count as overhang
OVERHANG_ASK_MM2 = 100 # more overhang than this (1 cm2): ask whether to print with supports


def _overhangs(path):
    """Downward-facing area that would print in the air, in the file's own orientation."""
    import numpy as np
    t = _load_tris(path)
    c = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    area = np.linalg.norm(c, axis=1) / 2
    nz = c[:, 2] / np.maximum(2 * area, 1e-12)
    off_bed = t[:, :, 2].min(axis=1) > t[:, :, 2].min() + 0.3
    mm2 = float(area[off_bed & (nz < -np.cos(np.radians(OVERHANG_DEG)))].sum())
    return {"mm2": round(mm2), "ask": mm2 > OVERHANG_ASK_MM2}


@app.get("/api/model")
def api_model():
    name = request.args.get("name", "")
    full = _inbox_path(name)
    if not full or not name.lower().endswith(".stl"):
        return jsonify({"error": "not an STL in the inbox"}), 400
    try:
        fp = _stl_footprint(full)
        if fp:
            fp["overhang"] = _overhangs(full)
            fp["centre_keepout"] = CENTRE_MM
        return jsonify(fp) if fp else (jsonify({"error": "no geometry"}), 400)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- slice -> preview -> send pipeline ----------------------------------------------------

import glob, hashlib
from flask import send_file

HERE = os.path.dirname(os.path.abspath(__file__))
BAMBU = os.path.join(HERE, "..", "tools", "bambu")
PROFILES_PY = os.path.join(BAMBU, "profiles.py")
RENDER_PY = os.path.join(BAMBU, "render.py")
THUMB_PY = os.path.join(BAMBU, "thumb.py")
# Slicer pinned to a known-working build (config: appimage); a desktop Studio may be newer.
APPIMAGE = next(iter(glob.glob(siteconf.path("appimage"))), None)
WORK = siteconf.path("work_dir")
_pending = {}   # last successful slice, ready to send


def _flatten(machine, process, filament):
    key = hashlib.md5("|".join([machine, process, filament]).encode()).hexdigest()[:10]
    d = os.path.join(WORK, "prof", key)
    need = [machine + ".json", process + ".json", filament + ".json"]
    if not all(os.path.isfile(os.path.join(d, n)) for n in need):
        os.makedirs(d, exist_ok=True)
        subprocess.run([sys.executable, PROFILES_PY, BBL_DIR, d, machine, process, filament],
                       check=True, capture_output=True, text=True)
    return d


def _load_tris(path):
    import struct, re as _re
    import numpy as np
    data = open(path, "rb").read()
    if data[:5].lower() == b"solid" and b"facet" in data[:2000].lower():
        v = _re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)
        return np.array(v, dtype=float).reshape(-1, 3, 3)
    n = struct.unpack("<I", data[80:84])[0]
    body = np.frombuffer(data[84:84 + n * 50], dtype=np.uint8).reshape(n, 50)
    return body[:, 12:48].copy().view("<f4").reshape(n, 3, 3).astype(float)


def _write_stl(path, tris):
    import struct
    import numpy as np
    n = len(tris)
    rec = np.zeros(n, dtype=[("nrm", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    rec["v"] = tris
    with open(path, "wb") as f:
        f.write(b"\0" * 80 + struct.pack("<I", n) + rec.tobytes())


def _place_stl(src, dst, deg, tx, ty):
    """Rotate the mesh about its footprint (hull) centroid and move that centroid to plate (tx, ty),
    so the sliced part lands where the UI drew it. Bambu Studio --arrange 0 then keeps it put:
    with the plate origin at the front-left corner, mesh XY maps 1:1 to plate mm (verified on the P2S).
    Returns the placed footprint bbox [x0, y0, x1, y1] in plate mm."""
    import numpy as np
    tris = _load_tris(src)
    xy = tris[:, :, :2].reshape(-1, 2)
    c = np.array(_hull(xy)).mean(axis=0)   # same reference the UI places by
    a = np.radians(deg)
    R = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
    tris[:, :, :2] = (tris[:, :, :2] - c) @ R.T + [tx, ty]
    _write_stl(dst, tris)
    pxy = tris[:, :, :2].reshape(-1, 2)
    return [float(pxy[:, 0].min()), float(pxy[:, 1].min()), float(pxy[:, 0].max()), float(pxy[:, 1].max())]


@app.post("/api/slice")
def api_slice():
    if not APPIMAGE:
        return jsonify({"ok": False, "error": "Bambu Studio AppImage not found"}), 500
    j = request.get_json(force=True, silent=True) or {}
    name = j.get("name", "")
    src = _inbox_path(name)
    if not src or not name.lower().endswith(".stl"):
        return jsonify({"ok": False, "error": "not an STL in the inbox"}), 400
    nozzle = j.get("nozzle", "0.4")
    machine = "Bambu Lab P2S %s nozzle" % nozzle
    process, filament = j.get("quality", ""), j.get("filament", "")
    if not _fits(process, nozzle) or not _fits(filament, nozzle):
        return jsonify({"ok": False, "error": "quality/filament not valid for this nozzle"}), 400
    os.makedirs(WORK, exist_ok=True)
    try:
        prof = _flatten(machine, process, filament)
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "error": "profile flatten failed: " + (e.stderr or "")[-300:]}), 500

    place = j.get("place", {}) or {}
    rot = int(place.get("rot", 0)) % 360
    # Place the part where the user dragged it: translate (and rotate) the mesh so --arrange 0 keeps it.
    # This is the whole point of placement — the P2S plate centre wears out, so printing to a side sticks better.
    tx = float(place.get("x", PLATE_MM / 2))
    ty = float(place.get("y", PLATE_MM / 2))
    stl = os.path.join(WORK, "placed.stl")
    try:
        placed_bbox = _place_stl(src, stl, rot, tx, ty)
    except Exception as e:
        return jsonify({"ok": False, "error": "placement failed: %s" % e}), 500
    arrange = "0"   # honour our exact XY; don't let the slicer re-home it to centre
    x0, y0, x1, y1 = placed_bbox
    if x0 < 0 or y0 < 0 or x1 > PLATE_MM or y1 > PLATE_MM:
        # The slicer would reject this as "nothing inside the print volume" with a cryptic error.
        return jsonify({"ok": False, "error": "Part is off the plate — drag it fully inside the 256 mm bed."}), 400

    # Brim and supports from the page override the flattened process profile.
    brim = j.get("brim", "auto")
    supports = j.get("supports", "off")   # off (default) | tree (anywhere) | plate (tree, from the plate only)
    over = {"enable_support": "0"}
    if supports in ("tree", "plate"):
        over.update({"enable_support": "1", "support_type": "tree(auto)",
                     "support_on_build_plate_only": "1" if supports == "plate" else "0"})
    over["curr_bed_type"] = "Textured PEI Plate"   # all plates are textured; the CLI defaults to Cool Plate
    if brim == "none":
        over["brim_type"] = "no_brim"
    elif brim == "tight":
        over.update({"brim_type": "outer_only", "brim_width": "5", "brim_object_gap": "0"})
    proc_json = os.path.join(WORK, "process.json")
    with open(os.path.join(prof, process + ".json")) as f:
        pj = json.load(f)
    # Site defaults for this filament (config.toml [filament_overrides."<profile>"]): filament keys go into
    # the filament profile, anything else into the process profile.
    fil_json = os.path.join(prof, filament + ".json")
    fil_over = dict((siteconf.get("filament_overrides") or {}).get(filament, {}))
    if fil_over:
        with open(fil_json) as f:
            fj = json.load(f)
        for k, v in fil_over.items():
            target = fj if k in fj else over
            cur = target.get(k)
            target[k] = [str(v)] * len(cur) if isinstance(cur, list) else str(v)
        fil_json = os.path.join(WORK, "filament.json")
        with open(fil_json, "w") as f:
            json.dump(fj, f)
    pj.update(over)
    with open(proc_json, "w") as f:
        json.dump(pj, f)
    try:
        slow = _autoslow_suggest(stl, float(pj.get("initial_layer_print_height", 0.2)), float(pj.get("layer_height", 0.2)))
    except Exception:
        slow = None

    base = re.sub(r"\.(stl|3mf|obj|step|stp)$", "", name, flags=re.I)
    out = os.path.join(WORK, base + ".gcode.3mf")
    if os.path.exists(out):
        os.remove(out)
    cmd = [APPIMAGE, "--slice", "0", "--arrange", arrange, "--orient", "0",
           "--load-settings", "%s;%s" % (os.path.join(prof, machine + ".json"), proc_json),
           "--load-filaments", fil_json,
           "--export-3mf", out, stl]
    try:
        rj = os.path.join(WORK, "result.json")
        if os.path.exists(rj):
            os.remove(rj)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=240, cwd=WORK)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "slice timed out"}), 500
    if r.returncode != 0 or not _job_meta(out):
        tail = "\n".join((r.stderr or r.stdout or "").strip().splitlines()[-4:])
        return jsonify({"ok": False, "error": "slice failed:\n" + tail}), 500

    st = os.stat(out)
    _slicer_checks[(os.path.abspath(out), st.st_mtime, st.st_size)] = slicecheck.read_result(rj)
    grams, minutes = _sliced_info(out)
    color = j.get("color") or "#8a8a8a"
    prev = os.path.join(WORK, "preview.png")
    try:
        subprocess.run([sys.executable, RENDER_PY, out, prev, color], timeout=90, capture_output=True)
    except Exception:
        prev = None
    try:   # plate picture for the printer screen and Handy; a print without one still works
        subprocess.run([sys.executable, THUMB_PY, out, color], timeout=90, capture_output=True)
    except Exception:
        pass

    # pre-send checks
    checks = []
    if fil_over:
        checks.append({"ok": True, "msg": "Filament defaults: " + ", ".join("%s %s" % (k, v) for k, v in fil_over.items())})
    sliced_bbox = (_job_meta(out) or {}).get("bbox")
    if sliced_bbox:
        x0, y0, x1, y1 = sliced_bbox   # what the slicer actually lays down, brim included
        m = 3  # keep a few mm off the physical edge
        on_plate = x0 >= m and y0 >= m and x1 <= PLATE_MM - m and y1 <= PLATE_MM - m
        w, h = x1 - x0, y1 - y0
        checks.append({"ok": on_plate,
                       "msg": "On the plate (%d×%d mm, centred ~%d,%d)" % (round(w), round(h), round((x0 + x1) / 2), round((y0 + y1) / 2))
                       if on_plate else "Off the plate — drag it back inside the 256 mm bed (now %d,%d to %d,%d)"
                       % (round(x0), round(y0), round(x1), round(y1))})
    try:
        oh = _overhangs(stl)
    except Exception:
        oh = None
    if oh and oh["ask"]:
        checks.append({"ok": supports != "off", "msg": "About %d cm² of overhang; %s" % (
            round(oh["mm2"] / 100), "supports on" if supports != "off" else "no supports — check the preview")})
    slot = _slot_arg(j.get("ams"))

    _pending.clear()
    _refresh_ams()
    _pending.update({"path": out, "usb_name": base + ".gcode.3mf", "slot": slot, "grams": grams,
                     "tray": _tray_identity(slot)})
    probs = _send_problems(out, slot)
    checks += [{"ok": False, "msg": m} for m in probs] or [{"ok": True, "msg": "Printer, nozzle, USB stick and AMS slot ready"}]
    return jsonify({"ok": True, "grams": grams, "minutes": minutes, "autoslow": slow,
                    "preview": "api/preview?t=%d" % int(time.time()) if prev else None, "checks": checks})


def _job_meta(path):
    """Nozzle, filaments and plate bbox from a sliced .gcode.3mf; None if it isn't a complete slice."""
    import re as _re, zipfile
    try:
        with zipfile.ZipFile(path) as z:
            info = z.read("Metadata/slice_info.config").decode("utf-8", "ignore")
            plate = json.loads(z.read("Metadata/plate_1.json"))
            if "Metadata/plate_1.gcode" not in z.namelist():
                return None
    except Exception:
        return None
    noz = _re.search(r'key="nozzle_diameters"\s+value="([\d.]+)', info)
    fils = [{"type": t, "grams": float(g)} for t, g in
            _re.findall(r'<filament [^>]*type="([^"]+)"[^>]*used_g="([\d.]+)"', info)]
    plates = len(_re.findall(r"<plate>", info))
    return {"nozzle": float(noz.group(1)) if noz else None, "filaments": fils,
            "bbox": plate.get("bbox_all"), "plates": plates}


SENDABLE_STATES = {"IDLE", "FINISH", "FAILED"}   # FAILED is also what a stopped job leaves


def _slot_arg(v):
    """AMS slot from a request: an explicit 0-3, else None (never a default)."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if 0 <= v <= 3 else None


def _refresh_ams(timeout=10.0):
    """Ask the printer for a full report; True once an AMS report newer than the request arrives."""
    c, sn = _conn["c"], _conn["sn"]
    if not c:
        return False
    t0 = time.time()
    try:
        p2s.request(c, sn, {"pushing": {"sequence_id": "0", "command": "pushall"}})
    except Exception:
        return False
    while time.time() - t0 < timeout:
        with _lock:
            if _state["ams_updated"] >= t0:
                return True
        time.sleep(0.25)
    return False


def _tray_identity(slot):
    """What the printer says is in a slot: material, Bambu filament id, colour."""
    if slot is None:
        return None
    with _lock:
        p = dict(_state["print"])
    for unit in (p.get("ams") or {}).get("ams", []):
        for t in unit.get("tray", []):
            if str(t.get("id")) == str(slot):
                return {"type": t.get("tray_type") or None, "idx": t.get("tray_info_idx") or None,
                        "color": (t.get("tray_color") or "")[:6].upper() or None}
    return None


def _tray_desc(t):
    return "empty" if not t or not t.get("type") else "%s %s #%s" % (t["type"], t.get("idx") or "?", t.get("color") or "?")


USB_MARGIN_MB = 50                     # free space kept on the USB stick beyond the file being sent


def _usb_free_mb(p):
    """Free space on the printer's USB stick (MB) from the camera report, or None if not reported."""
    try:
        return int((p.get("ipcam") or {})["tl_external_free_kb"]) // 1024
    except (KeyError, TypeError, ValueError):
        return None


def _recording_on(p):
    """The printer records every print to the USB stick's ipcamera folder; that fills the stick."""
    return (p.get("ipcam") or {}).get("ipcam_record") == "enable"


_slicer_checks = {}   # (path, mtime, size) -> (warnings, error)


def _slicer_check(path):
    """The slicer's own warnings for a sliced file (re-sliced once, then cached)."""
    try:
        st = os.stat(path)
    except OSError:
        return [], "file not found"
    key = (os.path.abspath(path), st.st_mtime, st.st_size)
    if key not in _slicer_checks:
        _slicer_checks[key] = slicecheck.slicer_warnings(path, APPIMAGE) if APPIMAGE else ([], "no slicer configured")
    return _slicer_checks[key]


def _send_problems(path, slot, expect=None, fresh=False, allow_runout=False, allow_warnings=False):
    """Everything that should stop a send, as messages; empty means go.
    fresh: first get a new AMS report from the printer. expect: the slot's spool when it was sliced."""
    meta = _job_meta(path)
    if not meta:
        return ["Not a complete sliced file (no plate G-code/metadata)."]
    if fresh and not _refresh_ams():
        return ["No fresh AMS report from the printer; not sending."]
    with _lock:
        p = dict(_state["print"])
        connected, age = _state["connected"], time.time() - _state["updated"] if _state["updated"] else None
    probs = gcodecheck.start_problems(path)
    if not allow_warnings:
        warns, err = _slicer_check(path)
        override = " Tick 'Print despite slicer warning' to send anyway."
        probs += ["Slicer warning: %s%s" % (w, override) for w in warns]
        if err:
            probs.append("Slicer check couldn't run: %s.%s" % (err, override))
    if not connected or age is None or age > 30:
        return probs + ["No fresh printer status (connected? current access code?)."]
    if not p.get("sdcard"):
        probs.append("No USB stick in the printer's screen-box port.")
    else:
        free, need = _usb_free_mb(p), os.path.getsize(path) // 2 ** 20 + 1 + USB_MARGIN_MB
        if free is not None and free < need:
            probs.append("USB stick nearly full: %d MB free, this job needs %d MB (with margin)%s." % (
                free, need, "; printer video recording is on and fills it" if _recording_on(p) else ""))
    if p.get("gcode_state") not in SENDABLE_STATES:
        probs.append("Printer is busy (%s)." % p.get("gcode_state"))
    try:
        live = float(p.get("nozzle_diameter"))
    except (TypeError, ValueError):
        live = None
    if meta["nozzle"] and live and abs(meta["nozzle"] - live) > 0.01:
        probs.append("Sliced for a %.1f mm nozzle; printer has %.1f mm." % (meta["nozzle"], live))
    if meta["plates"] > 1 or len(meta["filaments"]) != 1:
        probs.append("Only single-plate, single-filament jobs are supported.")
    b = meta.get("bbox")
    centre = plate_centre.centre_problem(b, PLATE_MM, CENTRE_MM)
    if centre:
        probs.append(centre)
    if b and (b[0] < 0 or b[1] < 0 or b[2] > PLATE_MM or b[3] > PLATE_MM):
        probs.append("Sliced print (brim included) runs off the plate.")
    tray = next((a for a in _status_ams() if str(a["slot"]) == str(slot)), None)
    if slot is None:
        probs.append("No AMS slot chosen.")
    elif not tray or not tray.get("type"):
        probs.append("AMS slot %s is empty." % slot)
    elif meta["filaments"]:
        want = meta["filaments"][0]
        if tray["type"].upper() != want["type"].upper():
            probs.append("Sliced for %s; slot %s holds %s." % (want["type"], slot, tray["type"]))
        rem = tray.get("remain")
        if rem is not None and rem >= 0 and rem / 100.0 * 1000.0 < want["grams"] and not allow_runout:
            probs.append("Low filament: ~%d g left, needs %.1f g. Tick 'Let it run out' to print anyway." % (rem * 10, want["grams"]))
    if slot is not None and expect is not None:
        now = _tray_identity(slot)
        if now != expect:
            probs.append("AMS slot %s changed since slicing (was %s, now %s). Re-slice." % (slot, _tray_desc(expect), _tray_desc(now)))
    return probs


def _status_ams():
    with _lock:
        p = dict(_state["print"])
    out = []
    for unit in (p.get("ams") or {}).get("ams", []):
        for t in unit.get("tray", []):
            out.append({"slot": t.get("id"), "type": t.get("tray_type"), "remain": t.get("remain")})
    return out


@app.get("/api/preview")
def api_preview():
    prev = os.path.join(WORK, "preview.png")
    if not os.path.isfile(prev):
        return "", 404
    return send_file(prev, mimetype="image/png")


# Vibration calibration: every Nth send (successful or not), or when ticked; either resets the count.
VIBE_FILE = os.path.join(siteconf.path("state_dir"), "vibration.json")


def _vibe_sends():
    try:
        with open(VIBE_FILE) as f:
            return int(json.load(f)["sends"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def _vibe_take(ticked):
    """Count this send; True if it should run the vibration calibration."""
    n, every = _vibe_sends() + 1, int(siteconf.get("vibration_every"))
    run = bool(ticked) or (every > 0 and n >= every)
    os.makedirs(os.path.dirname(VIBE_FILE), exist_ok=True)
    with open(VIBE_FILE, "w") as f:
        json.dump({"sends": 0 if run else n}, f)
    return run


@app.get("/api/vibration")
def api_vibration():
    every = int(siteconf.get("vibration_every"))
    return jsonify({"sends": _vibe_sends(), "every": every,
                    "due": every > 0 and _vibe_sends() + 1 >= every})


@app.post("/api/send")
def api_send():
    # Either send what we just sliced (_pending), or a pre-sliced file straight from the inbox ({name, slot}).
    j = request.get_json(force=True, silent=True) or {}
    name = j.get("name")
    if name:
        src = _inbox_path(name)
        if not src or not name.lower().endswith(".gcode.3mf"):
            return jsonify({"ok": False, "error": "not a sliced file in the inbox"}), 400
        path, usb_name, slot = src, name, _slot_arg(j.get("slot"))
        expect = None
    else:
        if not _pending.get("path") or not os.path.isfile(_pending["path"]):
            return jsonify({"ok": False, "error": "nothing sliced to send"}), 400
        path, usb_name, slot = _pending["path"], _pending["usb_name"], _pending.get("slot")
        expect = _pending.get("tray")
    slow_layer = j.get("slow_layer")
    runout = bool(j.get("allow_runout"))       # use up a nearly empty spool: AMS auto-refill or a run-out pause
    warnok = bool(j.get("allow_warnings"))     # the slicer warned (e.g. needs supports) and Dan chose to print anyway
    probs = _send_problems(path, slot, expect, fresh=True, allow_runout=runout, allow_warnings=warnok)
    if probs:
        return jsonify({"ok": False, "error": " ".join(probs)}), 400
    if slow_layer:
        os.makedirs(os.path.join(WORK, "slowed"), exist_ok=True)
        slowed = os.path.join(WORK, "slowed", usb_name)   # same name: start.py asks the printer for usb_name
        try:
            _slow_gcode(path, slowed, int(slow_layer))
        except Exception as e:
            return jsonify({"ok": False, "error": "auto-slow rewrite failed: %s" % e}), 500
        path = slowed
    env = dict(os.environ)
    try:
        up = subprocess.run([sys.executable, os.path.join(BAMBU, "upload.py"), path],
                            capture_output=True, text=True, timeout=120, env=env, cwd=BAMBU)
        if up.returncode != 0:
            return jsonify({"ok": False, "error": "upload failed: " + (up.stderr or "")[-300:]}), 502
        # The upload takes a while; check the printer and the spool again right before starting.
        probs = _send_problems(path, slot, expect, fresh=True, allow_runout=runout, allow_warnings=warnok)
        if probs:
            return jsonify({"ok": False, "error": "Uploaded but not started: " + " ".join(probs)}), 409
        # Persist the expected job before start so early reports are not missed.
        try:
            token = _print_monitor.expect(usb_name)
        except (OSError, RuntimeError) as exc:
            return jsonify({"ok": False, "error": "notification monitor not ready: " + str(exc)}), 503
        vibration = _vibe_take(j.get("vibration"))
        try:
            st = subprocess.run([sys.executable, os.path.join(BAMBU, "start.py"), usb_name, str(slot), "--local", path]
                                + (["--vibration"] if vibration else []),
                                capture_output=True, text=True, timeout=60, env=env, cwd=BAMBU)
        except subprocess.TimeoutExpired:
            _print_monitor.cancel(token)
            raise
        ok = st.returncode == 0   # start.py exits 0 only on a fresh PREPARE/RUNNING for this job
        if not ok:
            _print_monitor.cancel(token)
        if ok and slow_layer:
            _autoslow_set(usb_name.replace(".gcode.3mf", ""), slow_layer)
        with _lock:
            rec = _recording_on(_state["print"])
        return jsonify({"ok": ok, "upload": up.stdout.strip(), "start": st.stdout.strip(), "vibration": vibration,
                        "warning": "Printer video recording is on; it fills the USB stick." if rec else None,
                        "error": None if ok else "printer did not start: " + st.stdout.strip()}), (200 if ok else 502)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "send timed out"}), 500


@app.get("/")
def index():
    page = PAGE.replace("<body>", '<body class="remote">', 1) if _is_remote() else PAGE
    return Response(page, mimetype="text/html")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Print station</title>
<style>
:root{--bg:#14171c;--card:#1d222b;--line:#2b323d;--fg:#e8edf3;--mut:#9aa6b4;--accent:#4a9eff;
      --run:#3ecf6b;--warn:#ffb020;--err:#ff5d5d}
*{box-sizing:border-box}body{margin:0;font:15px/1.4 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:12px 20px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:16px}
h1{font-size:17px;margin:0;font-weight:600}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mut)}
.dot.on{background:var(--run)}.dot.off{background:var(--err)}
nav.tabs{display:flex;gap:6px}
.tab-btn{padding:7px 14px;font-size:14px;background:transparent;border:1px solid transparent;color:var(--mut)}
.tab-btn:hover{color:var(--fg);border-color:var(--line)}
.tab-btn.on{color:var(--fg);background:#2a3140;border-color:var(--line)}
#linktxt{margin-left:auto}
.statusbar{max-width:1200px;margin:16px auto 0;padding:0 16px}
main{max-width:1200px;margin:0 auto;padding:16px}
.panel.hidden{display:none}
.monitor{display:grid;gap:14px;align-items:start;grid-template-columns:minmax(0,1.7fr) minmax(300px,1fr)}
.monitor .right{display:flex;flex-direction:column;gap:14px}
@media(max-width:820px){.monitor{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.job{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.state{font-size:22px;font-weight:650}.state.RUNNING{color:var(--run)}.state.FAILED{color:var(--err)}
.state.PAUSE{color:var(--warn)}
.file{color:var(--mut)}
.bar{height:10px;background:#11151b;border-radius:6px;overflow:hidden;margin:14px 0 6px}
.bar>i{display:block;height:100%;background:var(--accent);width:0;transition:width .4s}
.sub{display:flex;gap:20px;color:var(--mut);font-size:13px;flex-wrap:wrap}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px}
.stat{background:#11151b;border-radius:9px;padding:10px 12px}
.stat .k{color:var(--mut);font-size:12px}.stat .v{font-size:17px;font-weight:600;margin-top:2px}
.slots{display:grid;gap:8px}
.slot{display:flex;align-items:center;gap:10px;font-size:13px}
.sw{width:16px;height:16px;border-radius:4px;border:1px solid #0006;flex:0 0 auto}
.btns{display:flex;gap:10px;flex-wrap:wrap}
button{font:inherit;color:var(--fg);background:#2a3140;border:1px solid var(--line);border-radius:9px;
       padding:10px 16px;cursor:pointer}
button:hover{border-color:var(--accent)}button:active{transform:translateY(1px)}
button.stop{background:#3a2024;border-color:#5a2a30}button.stop:hover{border-color:var(--err)}
button.lit{background:#3a3620;border-color:var(--warn);color:#ffd98a}
.seg{display:flex;gap:0;flex-wrap:wrap}
.seg button{border-radius:0;border-left-width:0;padding:8px 12px;font-size:13px;flex:1}
.seg button:first-child{border-radius:8px 0 0 8px;border-left-width:1px}
.seg button:last-child{border-radius:0 8px 8px 0}
.seg button.on{background:#8c96a3;border-color:#8c96a3;color:#14171c}
button:disabled{opacity:.4;cursor:default}
#partmap{width:100%;max-width:260px;display:block;margin:0 auto 10px;background:#11151b;border:1px solid var(--line);border-radius:8px}
#partmap rect{fill:#2a3140;stroke:var(--mut);stroke-width:1;cursor:pointer}
#partmap rect.sel{fill:#5a2a30;stroke:var(--err)}#partmap rect.gone{fill:none;stroke-dasharray:4 3;cursor:default}
#partmap text{fill:var(--fg);font-size:14px;text-anchor:middle;dominant-baseline:middle;pointer-events:none}
.part{display:flex;align-items:center;gap:8px;padding:4px 0}.part.gone{color:var(--mut);text-decoration:line-through}
.muted{color:var(--mut);font-size:13px}
.err{color:var(--err);font-size:13px}
.camhead{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.cambtns{display:flex;gap:8px}
.camhead button{padding:7px 14px;font-size:14px}
.camwrap{position:relative;background:#000;border-radius:9px;overflow:hidden;aspect-ratio:16/9}
.camwrap img{width:100%;display:block}
.camoff{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;
         padding:20px;color:var(--mut);font-size:13px}
.inbox{display:flex;flex-direction:column;gap:8px}
.frow{display:flex;align-items:center;gap:12px;padding:12px 14px;background:#11151b;border:1px solid var(--line);
      border-radius:9px;cursor:pointer}
.frow:hover{border-color:var(--accent)}.frow.sel{border-color:var(--accent);background:#19202b}
.frow .nm{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.frow .meta{color:var(--mut);font-size:12px;margin-left:auto;white-space:nowrap}
.tag{font-size:11px;padding:2px 7px;border-radius:5px;border:1px solid var(--line);color:var(--mut)}
.tag.sliced{color:var(--run);border-color:#2b4a36}.tag.model{color:var(--accent);border-color:#244056}
.empty{color:var(--mut);padding:24px;text-align:center}
.inboxgrid{display:grid;gap:14px;align-items:start;grid-template-columns:minmax(0,1fr) minmax(0,1.25fr)}
@media(max-width:820px){.inboxgrid{grid-template-columns:1fr}}
.field{margin-bottom:12px}
.field label{display:block;color:var(--mut);font-size:12px;margin-bottom:5px}
select,input[type=text]{width:100%;font:inherit;color:var(--fg);background:#11151b;border:1px solid var(--line);
       border-radius:8px;padding:9px 10px}
details{border-top:1px solid var(--line);margin-top:6px;padding-top:10px}
summary{cursor:pointer;color:var(--mut);font-size:13px;margin-bottom:10px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.plate{width:100%;max-width:340px;aspect-ratio:1;background:#0d1014;border:1px solid var(--line);border-radius:8px;
       touch-action:none;display:block}
.placerow{display:flex;align-items:center;gap:12px;margin:10px 0}
.chk{display:flex;align-items:center;gap:8px;color:var(--fg);font-size:13px}
.big{width:100%;margin-top:10px;background:#1f3147;border-color:#2b5375;color:#cfe4ff}
.big:hover{border-color:var(--accent)}
.warn{color:var(--warn);font-size:13px}.ok{color:var(--run);font-size:13px}
.amspick{display:flex;flex-direction:column;gap:6px}
.arow{display:flex;align-items:center;gap:10px;padding:8px 10px;background:#11151b;border:1px solid var(--line);
      border-radius:8px;cursor:pointer;font-size:13px}
.arow:hover{border-color:var(--accent)}
.arow.sel{border-color:var(--accent);background:#19202b}
.arow.off{opacity:.5}
.arow .meta{margin-left:auto;color:var(--mut)}
.preview{width:100%;max-width:340px;border:1px solid var(--line);border-radius:8px;background:#0d1014;display:block;margin-bottom:8px}
.checks{display:flex;flex-direction:column;gap:3px;margin:6px 0}
.chk-line{font-size:13px;padding:2px 0}
.chk-line.ok{color:#8fcf9a}
.chk-line.bad{color:#e08a8a}
.remotebar{display:none;align-items:center;gap:12px;padding:8px 16px;background:#2b2414;border-bottom:1px solid var(--line)}
.remote .remotebar{display:flex}
.remote .lanonly{display:none!important}
.camhead .seg button{padding:7px 12px;flex:none}
.remotecams{display:none;gap:14px;grid-template-columns:1fr 1fr;margin-bottom:14px}
.remote .remotecams{display:grid}
@media(max-width:820px){.remotecams{grid-template-columns:1fr}}
.remote main,.remote .statusbar{max-width:none}
.remote .monitor{grid-template-columns:1fr}
.remote .monitor .cam{display:none}
.remote .monitor .right{flex-direction:row;flex-wrap:wrap}
.remote .monitor .right>.card{flex:1 1 300px}
.zoomable{cursor:zoom-in}
.zoom{position:fixed;inset:0;background:#000;z-index:50;display:none}
.zoom.on{display:block}
.zoom img{width:100%;height:100%;object-fit:contain;display:block;cursor:zoom-out}
.zoom .zbtns{position:absolute;top:12px;right:12px;display:flex;gap:8px}
</style></head><body>
<header>
  <span class="dot" id="link"></span><h1>Print station</h1>
  <nav class="tabs">
    <button class="tab-btn on" data-tab="monitor">Monitor</button>
    <button class="tab-btn lanonly" data-tab="inbox">Inbox</button>
  </nav>
  <span class="muted" id="linktxt"></span>
</header>
<div class="remotebar"><span>Remote view · monitor, pause, skip part and stop only</span>
  <span class="muted" id="remleft"></span><button id="extend">Extend 1 h</button></div>
<div class="statusbar">
  <div class="card status">
    <div class="job"><span class="state" id="state">—</span><span class="file" id="file"></span></div>
    <div class="muted" id="stage"></div>
    <div class="bar"><i id="barfill"></i></div>
    <div class="sub">
      <span id="pct">—</span><span id="eta"></span><span id="layer"></span><span id="slowinfo"></span><span id="perr" class="err"></span>
    </div>
  </div>
</div>
<main>
  <section class="panel" id="panel-monitor">
    <div class="remotecams">
      <div class="card">
        <div class="camhead"><span class="muted">Printer camera</span><span class="cambtns" id="rlightslot"></span></div>
        <div class="camwrap"><img id="rcam-bambu" class="zoomable" alt="printer camera"><div class="camoff" id="rcamoff-bambu">Printer camera needs LAN + Developer mode and the current access code.</div></div>
      </div>
      <div class="card">
        <div class="camhead"><span class="muted">Webcam</span></div>
        <div class="camwrap"><img id="rcam-webcam" class="zoomable" alt="webcam"><div class="camoff" id="rcamoff-webcam">Webcam unavailable or busy (another viewer).</div></div>
      </div>
    </div>
    <div class="monitor">
      <div class="card cam">
        <div class="camhead">
          <span class="muted" id="camlabel">Camera</span>
          <span class="cambtns"><span class="seg" id="camsrc"><button data-src="bambu">Printer</button><button data-src="webcam">Webcam</button></span>
            <span id="lightslot"><button id="light">Light</button></span><button id="camtoggle">Stop</button></span>
        </div>
        <div class="camwrap"><img id="cam" class="zoomable" alt="camera"><div class="camoff" id="camoff">Camera needs LAN + Developer mode and the current access code.</div></div>
      </div>
      <div class="right">
        <div class="card">
          <div class="grid">
            <div class="stat"><div class="k">Nozzle</div><div class="v" id="noz">—</div></div>
            <div class="stat"><div class="k">Bed</div><div class="v" id="bed">—</div></div>
            <div class="stat"><div class="k">Nozzle size</div><div class="v" id="nsz">—</div></div>
            <div class="stat"><div class="k">USB stick</div><div class="v" id="usb">—</div></div>
          </div>
        </div>
        <div class="card">
          <div class="btns">
            <button id="pause">Pause</button>
            <button id="resume">Resume</button>
            <button id="stop" class="stop">Stop print</button>
          </div>
          <div class="lanonly"><div class="muted" style="margin:14px 0 6px">Speed</div>
          <div class="seg" id="speed">
            <button data-lvl="1">Silent</button>
            <button data-lvl="2">Normal</button>
            <button data-lvl="3">Sport</button>
            <button data-lvl="4">Ludicrous</button>
          </div></div>
          <div class="muted" id="ctlmsg" style="margin-top:10px"></div>
        </div>
        <div class="card" id="partscard" style="display:none">
          <div class="muted" style="margin-bottom:10px">Parts on this plate · pick the ones to skip</div>
          <svg id="partmap" viewBox="0 0 256 256" aria-label="plate map"></svg>
          <div id="parts"></div>
          <div class="btns" style="margin-top:10px"><button id="skipbtn" class="stop" disabled>Skip selected</button></div>
          <div class="muted" id="skipmsg" style="margin-top:8px"></div>
        </div>
        <div class="card">
          <div class="muted" style="margin-bottom:10px">AMS filament</div>
          <div class="slots" id="slots"></div>
        </div>
      </div>
    </div>
  </section>

  <section class="panel hidden lanonly" id="panel-inbox">
    <div class="inboxgrid">
      <div class="card">
        <div class="camhead">
          <span class="muted">Print inbox · <span id="inboxpath"></span></span>
          <button id="refreshinbox">Refresh</button>
        </div>
        <div class="inbox" id="inbox"><div class="empty">Loading…</div></div>
      </div>
      <div class="card" id="detail"><div class="empty">Select a file to set up a print.</div></div>
    </div>
  </section>
</main>
<div class="zoom" id="zoom"><div class="zbtns"><button id="zswitch">Other camera</button><button id="zclose">Close</button></div></div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function temps(c,t){c=c==null?'—':Math.round(c); t=t==null?0:Math.round(t);
  return t>0? c+' → '+t+'°C' : c+'°C';}
let last={};
async function tick(){
  let d; try{ d=await (await fetch('api/status')).json(); }catch(e){ return; }
  last=d;
  const m=d.meta||{};
  $('link').className='dot '+(m.connected?'on':'off');
  $('linktxt').textContent=m.connected?('updated '+(m.age??'?')+'s ago'):('disconnected'+(m.error?': '+m.error:''));
  const st=d.state||'—';
  $('state').textContent=st; $('state').className='state '+st;
  $('file').textContent=d.file||'';
  $('stage').textContent=(m.connected && m.age!=null && m.age<=30)?(d.stage||''):'';
  const pct=d.percent==null?0:d.percent;
  $('barfill').style.width=pct+'%';
  $('pct').textContent=(d.percent==null?'—':pct+'%');
  $('eta').textContent=d.remaining?('~'+d.remaining+' min left'):'';
  $('layer').textContent=(d.layer!=null&&d.total_layer!=null)?('layer '+d.layer+'/'+d.total_layer):'';
  $('perr').textContent=(d.print_error&&d.print_error!==0)?('print_error '+d.print_error):'';
  const as=d.autoslow; $('slowinfo').textContent=(as&&as.job===d.file)?(as.done?'slowed from layer '+as.layer:'slows from layer '+as.layer):'';
  $('noz').textContent=temps(d.nozzle,d.nozzle_target);
  $('bed').textContent=temps(d.bed,d.bed_target);
  $('nsz').textContent=d.nozzle_size?d.nozzle_size+' mm':'—';
  $('usb').textContent=!d.sdcard?'none':(d.usb_free_mb!=null?(d.usb_free_mb/1024).toFixed(1)+' GB free':'present')+(d.recording?' — video recording ON, fills the stick':'');
  const s=$('slots'); s.innerHTML='';
  (d.ams||[]).forEach(a=>{const row=document.createElement('div');row.className='slot';
    row.innerHTML='<span class="sw" style="background:'+(a.color||'#444')+'"></span>'+
      '<b>slot '+a.slot+'</b> '+(a.name||'empty')+
      ' <span class="muted">· '+(a.remain==null||a.remain<0?'?':a.remain+'%')+' left</span>';
    s.appendChild(row);});
  const running=(st==='RUNNING'), paused=(st==='PAUSE');
  $('pause').disabled=!running; $('resume').disabled=!paused;
  $('stop').disabled=!(running||paused||st==='PREPARE'||st==='SLICING');
  lightOn=(d.lights||[]).includes('on');
  const lb=$('light'); lb.classList.toggle('lit',lightOn);
  lb.textContent=lightOn?'Light: on':'Light: off';
  document.querySelectorAll('#speed button').forEach(b=>b.classList.toggle('on',+b.dataset.lvl===d.speed));
  showParts(d.parts);
}

// skip part: shown only while a multi-part job is running or paused (the server sends parts only then)
let partSig='', skipSel=new Set(), skipArm=null;
function showParts(parts){
  const card=$('partscard');
  if(!parts||parts.length<2){ card.style.display='none'; partSig=''; skipSel.clear(); return; }
  card.style.display='';
  const sig=parts.map(x=>x.id+(x.skipped?'s':'')).join(',');
  if(sig===partSig) return;            // unchanged: keep the user's ticks
  partSig=sig;
  parts.forEach(x=>{ if(x.skipped) skipSel.delete(x.id); });
  const map=$('partmap'), list=$('parts'); map.innerHTML=''; list.innerHTML='';
  const ns='http://www.w3.org/2000/svg';
  parts.forEach((x,n)=>{
    const num=n+1;
    if(x.bbox){ const [x0,y0,x1,y1]=x.bbox;
      const r=document.createElementNS(ns,'rect');
      r.setAttribute('x',x0); r.setAttribute('y',256-y1); r.setAttribute('width',Math.max(x1-x0,2)); r.setAttribute('height',Math.max(y1-y0,2));
      r.dataset.id=x.id; if(x.skipped) r.classList.add('gone');
      else r.addEventListener('click',()=>togglePart(x.id));
      map.appendChild(r);
      const t=document.createElementNS(ns,'text'); t.setAttribute('x',(x0+x1)/2); t.setAttribute('y',256-(y0+y1)/2); t.textContent=num;
      map.appendChild(t); }
    const row=document.createElement('label'); row.className='part'+(x.skipped?' gone':'');
    row.innerHTML='<input type="checkbox"'+(x.skipped?' disabled':'')+'> <b>'+num+'</b> '+esc(x.name)+(x.skipped?' · skipped':'');
    const cb=row.querySelector('input'); cb.checked=skipSel.has(x.id); cb.dataset.id=x.id;
    cb.addEventListener('change',()=>togglePart(x.id,cb.checked));
    list.appendChild(row);
  });
  map.style.display=parts.some(x=>x.bbox)?'':'none';
  syncSkip();
}
function togglePart(id,on){
  if(on===undefined) on=!skipSel.has(id);
  on?skipSel.add(id):skipSel.delete(id); syncSkip();
}
function syncSkip(){
  document.querySelectorAll('#parts input').forEach(cb=>cb.checked=skipSel.has(+cb.dataset.id));
  document.querySelectorAll('#partmap rect').forEach(r=>r.classList.toggle('sel',skipSel.has(+r.dataset.id)));
  const b=$('skipbtn'); clearTimeout(skipArm); skipArm=null;
  b.disabled=!skipSel.size; b.textContent=skipSel.size?('Skip '+skipSel.size+' part'+(skipSel.size>1?'s':'')):'Skip selected';
}
$('skipbtn').addEventListener('click',async()=>{
  const b=$('skipbtn'), n=skipSel.size; if(!n) return;
  if(!skipArm){ b.textContent='Confirm: skip '+n+' part'+(n>1?'s':'')+' (can\'t undo)';
    skipArm=setTimeout(syncSkip,6000); return; }
  clearTimeout(skipArm); skipArm=null; b.disabled=true; $('skipmsg').textContent='skipping…';
  try{ const r=await (await fetch('api/skip',{method:'POST',headers:{'Content-Type':'application/json'},
         body:JSON.stringify({ids:[...skipSel]})})).json();
    $('skipmsg').textContent=r.ok?'✓ skipped':'✗ '+(r.error||'failed');
    if(r.ok) skipSel.clear();
  }catch(e){ $('skipmsg').textContent='✗ skip failed: '+e; }
  partSig=''; syncSkip(); tick();
});
let lightOn=false;
async function send(cmd,btn){
  $('ctlmsg').textContent='…'; btn.disabled=true;
  try{ const r=await (await fetch('api/control/'+cmd,{method:'POST'})).json();
    $('ctlmsg').textContent=(r.ok?'✓ ':'✗ ')+cmd+' — '+JSON.stringify(r.reply);
  }catch(e){ $('ctlmsg').textContent='✗ '+cmd+' failed: '+e; }
  finally{ btn.disabled=false; }   // tick() re-applies the real enabled state on the next poll
  setTimeout(tick,600);
}
['pause','resume','stop'].forEach(c=>$(c).addEventListener('click',e=>send(c,e.target)));
$('light').addEventListener('click',e=>send(lightOn?'light_off':'light_on',e.target));

// speed: Silent/Normal/Sport/Ludicrous -> print_speed level
document.querySelectorAll('#speed button').forEach(b=>b.addEventListener('click',async()=>{
  const lvl=b.dataset.lvl; $('ctlmsg').textContent='setting speed…';
  document.querySelectorAll('#speed button').forEach(x=>x.disabled=true);
  try{ const r=await (await fetch('api/speed/'+lvl,{method:'POST'})).json();
    $('ctlmsg').textContent=(r.ok?'✓ speed ':'✗ speed ')+(r.speed||lvl);
  }catch(e){ $('ctlmsg').textContent='✗ speed failed: '+e; }
  finally{ document.querySelectorAll('#speed button').forEach(x=>x.disabled=false); setTimeout(tick,600); }
}));

// camera: an <img> on the MJPEG endpoint; toggle to spare CPU, show a hint if it won't load.
let camOn=true, camSrc='bambu';
try{camSrc=localStorage.getItem('camsrc')||'bambu';}catch(e){}
const camNames={bambu:'Printer camera',webcam:'Webcam'};
function camUrl(src){return 'cam.mjpeg?src='+src+'&t='+Date.now();}
function setSrc(src,note){camSrc=src; try{localStorage.setItem('camsrc',src);}catch(e){}
  document.querySelectorAll('#camsrc button').forEach(b=>b.classList.toggle('on',b.dataset.src===src));
  $('camlabel').textContent=note||camNames[src];}
// Streams end after 10 minutes (the station enforces it too) and pause while the tab is hidden.
const CAM_LIMIT=600000; let camTimer=null, camHidden=false, rcamOn=false, rcamTimer=null;
function camPaused(off,again,why){off.style.display='flex';off.textContent=(why||'Camera paused after 10 minutes.')+' ';
  const b=document.createElement('button');b.textContent='Reconnect camera';
  b.addEventListener('click',e=>{e.stopPropagation();again();});off.appendChild(b);}
function camPause(){camStop();$('camtoggle').textContent='Start';camPaused($('camoff'),camStart);}
function camStart(){camOn=true;camHidden=false;clearTimeout(camTimer);camTimer=setTimeout(camPause,CAM_LIMIT);
  $('camoff').style.display='none';$('cam').style.display='block';
  setSrc(camSrc,$('camlabel').dataset.note); $('camlabel').dataset.note='';
  $('cam').src=camUrl(camSrc);$('camtoggle').textContent='Stop';}
document.querySelectorAll('#camsrc button').forEach(b=>b.addEventListener('click',()=>{camSrc=b.dataset.src;camStart();}));
function camStop(){camOn=false;clearTimeout(camTimer);$('cam').src='';$('cam').style.display='none';
  $('camoff').style.display='flex';$('camoff').textContent='Camera stopped.';$('camtoggle').textContent='Start';}
$('cam').addEventListener('error',()=>{if(!camOn||!$('cam').getAttribute('src'))return;
  if(camSrc==='bambu'){$('camlabel').dataset.note='Webcam (printer camera unavailable)';camSrc='webcam';camStart();return;}
  $('cam').style.display='none';$('camoff').style.display='flex';
  $('camoff').textContent='Webcam unavailable or busy (another viewer).';});
$('cam').addEventListener('load',()=>{if(camOn){$('camoff').style.display='none';$('cam').style.display='block';}});
$('camtoggle').addEventListener('click',()=>camOn?camStop():camStart());
// The station stops a camera after three failed connects; offer a manual reconnect instead of retrying.
const NOT_RESPONDING='Camera not responding.';
setInterval(async()=>{if(document.hidden||!(camOn||rcamOn))return;
  try{const h=await (await fetch('api/camera-health')).json();
    if(document.body.classList.contains('remote')){
      if(rcamOn)['bambu','webcam'].forEach(src=>{if(h[src].state==='stopped'&&$('rcam-'+src).getAttribute('src')){$('rcam-'+src).src='';camPaused($('rcamoff-'+src),rcamStart,NOT_RESPONDING);}});
    }else if(camOn&&h[camSrc].state==='stopped'){camStop();$('camtoggle').textContent='Start';camPaused($('camoff'),camStart,NOT_RESPONDING);}
  }catch(e){}},5000);
document.addEventListener('visibilitychange',()=>{
  if(document.body.classList.contains('remote'))return;
  if(document.hidden&&camOn){camStop();camHidden=true;}
  else if(!document.hidden&&camHidden)camStart();});

// tabs: Monitor / Inbox, sharing the status bar above.
function showTab(name){
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.toggle('on',b.dataset.tab===name));
  document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('hidden',p.id!=='panel-'+name));
  if(name==='inbox') loadInbox();
}
document.querySelectorAll('.tab-btn').forEach(b=>b.addEventListener('click',()=>showTab(b.dataset.tab)));

function human(n){return n>1048576?(n/1048576).toFixed(1)+' MB':(n/1024).toFixed(0)+' KB';}
async function loadInbox(){
  const box=$('inbox');
  let d; try{ d=await (await fetch('api/inbox')).json(); }
  catch(e){ box.innerHTML='<div class="empty">Could not read inbox.</div>'; return; }
  $('inboxpath').textContent=d.inbox||'';
  if(!d.items||!d.items.length){ box.innerHTML='<div class="empty">Empty. Drop .stl / .3mf files into '+(d.inbox||'the inbox')+'.</div>'; return; }
  box.innerHTML='';
  d.items.forEach(it=>{
    const row=document.createElement('div'); row.className='frow'; row.dataset.name=it.name;
    let meta=human(it.size);
    if(it.kind==='sliced'&&it.grams!=null) meta=it.grams+' g · ~'+it.minutes+' min · '+meta;
    row.innerHTML='<span class="tag '+it.kind+'">'+(it.kind==='sliced'?'sliced':'model')+'</span>'+
      '<span class="nm">'+esc(it.name)+'</span><span class="meta">'+meta+'</span>';
    row.addEventListener('click',()=>{
      document.querySelectorAll('.frow').forEach(r=>r.classList.remove('sel'));
      row.classList.add('sel'); selectFile(it);
    });
    box.appendChild(row);
  });
}
// ---- file detail: settings (lean + advanced) and drag-to-place --------------------------
let place={x:128,y:128,rot:0}, foot=null, curNozzle='0.4', drag=null;

function amsPickHTML(){
  return '<div class="amspick" id="amsslot">'+(last.ams||[]).map(a=>
    '<div class="arow'+(a.type?'':' off')+'" data-slot="'+a.slot+'">'+
      '<span class="sw" style="background:'+(a.color||'#444')+'"></span>'+
      '<span>slot '+a.slot+' · '+(a.name||'empty')+'</span>'+
      '<span class="meta">'+(a.remain!=null&&a.remain>=0?a.remain+'%':'')+'</span></div>').join('')+'</div>';
}
function wireAms(){
  const rows=[...document.querySelectorAll('#amsslot .arow')];
  rows.forEach(r=>r.addEventListener('click',()=>{rows.forEach(x=>x.classList.remove('sel')); r.classList.add('sel');}));
  // No default: the slot must be picked explicitly, or the slice/send refuses.
}
function selectedSlot(){const r=document.querySelector('#amsslot .arow.sel'); return r?+r.dataset.slot:null;}
function opt(list,sel){return (list||[]).map(n=>'<option'+(n===sel?' selected':'')+'>'+esc(n)+'</option>').join('');}

function detailModelHTML(it){
  return '<div class="muted" style="margin-bottom:12px">'+esc(it.name)+'</div>'+
    '<div class="field"><label>Quality (layer height)</label><select id="q"></select></div>'+
    '<div class="field"><label>Print from AMS slot</label>'+amsPickHTML()+'</div>'+
    '<details id="adv"><summary>Advanced</summary>'+
      '<div class="two">'+
        '<div class="field"><label>Nozzle</label><select id="noz"></select></div>'+
        '<div class="field"><label>Brim</label><select id="brim">'+
          '<option value="auto">Auto</option><option value="none">Off</option>'+
          '<option value="tight">On, no gap</option></select></div>'+
      '</div>'+
      '<div class="field"><label>Supports <span class="muted" id="ohinfo"></span></label><select id="sup">'+
        '<option value="off">Off</option><option value="tree">Tree (auto)</option>'+
        '<option value="plate">Tree, from the plate only</option></select></div>'+
      '<div class="field"><label>Filament profile</label><select id="fil"></select></div>'+
          '</details>'+
    '<div class="field" style="margin-top:14px"><label>Placement on plate (drag the part; rotate 90°)</label>'+
      '<canvas id="plate" class="plate" width="340" height="340"></canvas>'+
      '<div class="placerow"><button id="rotate">Rotate 90°</button><button id="center">Center</button>'+
        '<span class="muted" id="placeinfo"></span></div></div>'+
    '<button id="slicebtn" class="big">Slice &amp; preview</button>'+
    '<div id="sliceout" style="margin-top:10px"></div>';
}
function detailSlicedHTML(it){
  return '<div class="muted" style="margin-bottom:12px">'+esc(it.name)+'</div>'+
    '<div class="field">'+(it.grams!=null?('<b>'+it.grams+' g</b> · ~'+it.minutes+' min — already sliced'):'already sliced')+'</div>'+
    '<div class="field"><label>Print from AMS slot</label>'+amsPickHTML()+'</div>'+
    vibeHTML()+runoutHTML()+warnokHTML()+'<button id="sendbtn" class="big">Send &amp; start</button>'+
    '<div id="sliceout" style="margin-top:10px"></div>';
}

async function selectFile(it){
  const det=$('detail');
  if(it.kind==='sliced'){ det.innerHTML=detailSlicedHTML(it); wireAms();
    $('sendbtn').addEventListener('click',()=>doSend({name:it.name, slot:selectedSlot(), vibration:vibeOn(), allow_runout:runoutOn(), allow_warnings:warnokOn()})); return; }
  curNozzle=((last.nozzle_size||'0.4')+'');
  det.innerHTML=detailModelHTML(it);
  wireAms();
  await loadProfiles();
  await loadFootprint(it.name);
  $('noz').addEventListener('change',async e=>{curNozzle=e.target.value; await loadProfiles();});
  $('rotate').addEventListener('click',()=>{place.rot=(place.rot+90)%360; clampPlace(); drawPlate();});
  $('center').addEventListener('click',()=>{place.x=foot?foot.plate/2:128; place.y=place.x; drawPlate();});
  const cv=$('plate');
  cv.addEventListener('pointerdown',e=>{const p=cvToMM(e); drag={dx:place.x-p.x,dy:place.y-p.y}; cv.setPointerCapture(e.pointerId);});
  cv.addEventListener('pointermove',e=>{if(!drag)return; const p=cvToMM(e); place.x=p.x+drag.dx; place.y=p.y+drag.dy; clampPlace(); drawPlate();});
  cv.addEventListener('pointerup',()=>{drag=null;});
  $('slicebtn').addEventListener('click',()=>doSlice(it.name));
}

function slotColor(slot){const a=(last.ams||[]).find(x=>+x.slot===+slot); return a?a.color:null;}
function checksHTML(checks){ return (checks||[]).map(c=>
  '<div class="chk-line '+(c.ok?'ok':'bad')+'">'+(c.ok?'✓':'✕')+' '+esc(c.msg)+'</div>').join(''); }

async function doSlice(name){
  const out=$('sliceout'), btn=$('slicebtn');
  const body={name:name, quality:$('q').value, ams:selectedSlot(), nozzle:$('noz').value,
    filament:$('fil').value, brim:$('brim').value, supports:$('sup').value,
    color:slotColor(selectedSlot()), place:{rot:place.rot, x:place.x, y:place.y}};
  btn.disabled=true; out.innerHTML='<span class="muted">Slicing… (first run flattens profiles, ~30–60 s)</span>';
  try{
    const r=await fetch('api/slice',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await r.json();
    if(!j.ok){ out.innerHTML='<div class="chk-line bad">✕ '+esc(j.error||'slice failed')+'</div>'; return; }
    out.innerHTML=
      (j.preview?'<img class="preview" src="'+j.preview+'">':'')+
      '<div class="field"><b>'+(j.grams!=null?j.grams+' g':'?')+'</b> · ~'+(j.minutes!=null?j.minutes:'?')+' min</div>'+
      '<div class="checks">'+checksHTML(j.checks)+'</div>'+
      (j.autoslow?'<label class="chk"><input type="checkbox" id="slowon"'+(j.autoslow.suggest?' checked':'')+'> Slow down above layer '+
        '<input type="number" id="slowlayer" min="2" value="'+j.autoslow.layer+'" style="width:80px"> '+
        '<span class="muted">(≈'+j.autoslow.z+' mm, outer wall slow throughout; part '+j.autoslow.height+' mm tall on a '+j.autoslow.width+' mm base)</span></label>':'')+
      vibeHTML()+runoutHTML()+warnokHTML()+'<button id="sendbtn" class="big">Send &amp; start</button>';
    $('sendbtn').addEventListener('click',()=>doSend({slow_layer:($('slowon')&&$('slowon').checked)?+$('slowlayer').value:null, vibration:vibeOn(), allow_runout:runoutOn(), allow_warnings:warnokOn()}));
  }catch(e){ out.innerHTML='<div class="chk-line bad">✕ '+esc(e)+'</div>'; }
  finally{ btn.disabled=false; }
}

// Vibration calibration: off by default (run it every 10-15 prints).
let vibeInfo=null;
async function vibeLoad(){ try{ vibeInfo=await (await fetch('api/vibration')).json(); }catch(e){} }
vibeLoad();
function vibeHTML(){ const v=vibeInfo, due=v&&v.due;
  return '<label class="chk"><input type="checkbox" id="vibe"'+(due?' checked disabled':'')+'> Vibration calibration'+
    (v&&v.every?' <span class="muted">('+(due?'due: every '+v.every+' sends':v.sends+' of '+v.every+' sends since last')+')</span>':'')+'</label>'; }
function vibeOn(){ return !!($('vibe')&&$('vibe').checked); }
function runoutHTML(){ return '<label class="chk"><input type="checkbox" id="runout"> Let it run out <span class="muted">(low spool: AMS refills from a matching spool, or the printer pauses; phone alert either way)</span></label>'; }
function runoutOn(){ return !!($('runout')&&$('runout').checked); }
function warnokHTML(){ return '<label class="chk"><input type="checkbox" id="warnok"> Print despite slicer warning <span class="muted">(e.g. "floating cantilever": the slicer thinks it needs supports)</span></label>'; }
function warnokOn(){ return !!($('warnok')&&$('warnok').checked); }
async function doSend(body){
  const btn=$('sendbtn'), out=$('sliceout');
  if(body&&body.name&&body.slot==null){ out.innerHTML='<div class="chk-line bad">✕ pick an AMS slot first</div>'; return; }
  btn.disabled=true; const prev=btn.textContent; btn.textContent='Sending…';
  try{
    const r=await fetch('api/send',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body||{})});
    const j=await r.json();
    const line=document.createElement('div');
    line.className='chk-line '+(j.ok?'ok':'bad');
    line.textContent=(j.ok?'✓ Sent and started — see the Monitor tab':'✕ '+(j.error||j.start||'send failed'));
    out.appendChild(line);
    if(j.warning){ const w=document.createElement('div'); w.className='chk-line bad'; w.textContent='⚠ '+j.warning; out.appendChild(w); }
    if(j.ok) setTimeout(()=>{showTab('monitor');},j.warning?4000:1200);
  }catch(e){ const l=document.createElement('div'); l.className='chk-line bad'; l.textContent='✕ '+e; out.appendChild(l); }
  finally{ btn.disabled=false; btn.textContent=prev; vibeLoad(); }
}

async function loadProfiles(){
  let p; try{ p=await (await fetch('api/profiles?nozzle='+curNozzle)).json(); }catch(e){ return; }
  const qsel=p.qualities.find(n=>/0\.20mm Standard/.test(n))||p.qualities[0];
  $('q').innerHTML=opt(p.qualities,qsel);
  $('noz').innerHTML=opt(p.nozzles,curNozzle);
  const fsel=p.filaments.find(n=>/PLA Basic/.test(n))||p.filaments[0];
  $('fil').innerHTML=opt(p.filaments,fsel);
}
async function loadFootprint(name){
  let fp; try{ fp=await (await fetch('api/model?name='+encodeURIComponent(name))).json(); }catch(e){ return; }
  if(fp.error){ foot=null; $('placeinfo').textContent=fp.error; return; }
  foot=fp; place=sidePlace(fp); drawPlate();
  const oh=fp.overhang;
  if(oh){ $('ohinfo').textContent=oh.ask?'— about '+(oh.mm2/100).toFixed(0)+' cm² overhangs; supports?':'— none needed';
    if(oh.ask) $('adv').open=true; }
}
function sidePlace(fp){ // left of the worn centre, else in front of it, else centred
  const c=fp.plate/2, gap=(fp.centre_keepout||0)+8, h=fp.hull, n=h.length;
  const cx=h.reduce((a,p)=>a+p[0],0)/n, cy=h.reduce((a,p)=>a+p[1],0)/n, b=fp.bbox;
  const x=c-gap-(b[2]-cx); if(x-(cx-b[0])>=5) return {x:x,y:c,rot:0};
  const y=c-gap-(b[3]-cy); if(y-(cy-b[1])>=5) return {x:c,y:y,rot:0};
  return {x:c,y:c,rot:0};
}
function cvToMM(e){const cv=$('plate'),r=cv.getBoundingClientRect(),s=foot?foot.plate:256;
  return {x:(e.clientX-r.left)/r.width*s, y:s-(e.clientY-r.top)/r.height*s};}
function hullTransformed(){ // hull points centered on their own centroid, rotated, then placed
  const h=foot.hull, c=h.reduce((a,p)=>[a[0]+p[0],a[1]+p[1]],[0,0]).map(v=>v/h.length);
  const a=place.rot*Math.PI/180, ca=Math.cos(a), sa=Math.sin(a);
  return h.map(p=>{const dx=p[0]-c[0],dy=p[1]-c[1];
    return [place.x+dx*ca-dy*sa, place.y+dx*sa+dy*ca];});
}
function clampPlace(){ if(!foot)return; const pts=hullTransformed(), s=foot.plate;
  const xs=pts.map(p=>p[0]), ys=pts.map(p=>p[1]);
  const minx=Math.min(...xs),maxx=Math.max(...xs),miny=Math.min(...ys),maxy=Math.max(...ys);
  if(minx<0)place.x-=minx; if(maxx>s)place.x-=(maxx-s);
  if(miny<0)place.y-=miny; if(maxy>s)place.y-=(maxy-s); }
function drawPlate(){
  const cv=$('plate'),g=cv.getContext('2d'),W=cv.width,s=foot?foot.plate:256,k=W/s;
  g.clearRect(0,0,W,W); g.fillStyle='#0d1014'; g.fillRect(0,0,W,W);
  g.strokeStyle='#2b323d'; g.lineWidth=1;
  for(let i=1;i<8;i++){const x=W*i/8; g.beginPath();g.moveTo(x,0);g.lineTo(x,W);g.moveTo(0,x);g.lineTo(W,x);g.stroke();}
  g.strokeStyle='#4a5565'; g.strokeRect(1,1,W-2,W-2);
  if(!foot)return;
  const pts=hullTransformed().map(p=>[p[0]*k, W-p[1]*k]);
  g.beginPath(); pts.forEach((p,i)=>i?g.lineTo(p[0],p[1]):g.moveTo(p[0],p[1])); g.closePath();
  g.fillStyle='rgba(74,158,255,.30)'; g.fill(); g.strokeStyle='#4a9eff'; g.lineWidth=1.5; g.stroke();
  $('placeinfo').textContent='x '+place.x.toFixed(0)+' · y '+place.y.toFixed(0)+' · '+place.rot+'° · '+
    Math.round(foot.bbox[2]-foot.bbox[0])+'×'+Math.round(foot.bbox[3]-foot.bbox[1])+'×'+foot.dz+' mm';
}
$('refreshinbox').addEventListener('click',loadInbox);

// remote: time left until access closes, and a one-hour extension
let remExp=0;
async function remoteInfo(){try{remExp=(await (await fetch('api/remote')).json()).expires||0;}catch(e){}}
function remTick(){if(!remExp)return; const s=Math.max(0,Math.round(remExp-Date.now()/1000));
  $('remleft').textContent=s?('closes in '+Math.floor(s/3600)+'h '+String(Math.floor(s%3600/60)).padStart(2,'0')+'m'):'access closed';}
$('extend').addEventListener('click',async e=>{e.target.disabled=true;
  try{const r=await (await fetch('api/remote/extend',{method:'POST'})).json(); if(r.ok)remExp=r.expires;}catch(err){}
  e.target.disabled=false; remTick();});
if(document.body.classList.contains('remote')){remoteInfo().then(remTick); setInterval(remTick,15000); setInterval(remoteInfo,60000);}

// remote: both cameras side by side, one full-size frame a second each
function rcamStart(){rcamOn=true;clearTimeout(rcamTimer);rcamTimer=setTimeout(()=>rcamStop(true),CAM_LIMIT);
  ['bambu','webcam'].forEach(src=>{$('rcamoff-'+src).style.display='none';$('rcam-'+src).src=camUrl(src);});}
function rcamStop(paused){rcamOn=false;clearTimeout(rcamTimer);
  ['bambu','webcam'].forEach(src=>{$('rcam-'+src).src='';
    if(paused)camPaused($('rcamoff-'+src),rcamStart);});}
function remoteCams(){
  $('rlightslot').appendChild($('light'));
  ['bambu','webcam'].forEach(src=>{const img=$('rcam-'+src), off=$('rcamoff-'+src);
    img.addEventListener('load',()=>{off.style.display='none';});
    img.addEventListener('error',()=>{if(!rcamOn||!img.getAttribute('src'))return; img.src='';
      camPaused(off,rcamStart,NOT_RESPONDING);});});   // manual reconnect only
  document.addEventListener('visibilitychange',()=>{if(document.hidden){if(rcamOn){rcamStop(false);rcamTimer='hidden';}}
    else if(rcamTimer==='hidden')rcamStart();});
  rcamStart();
}

// click a camera to fill the window; Esc or click again to return
let zoomed=null, zoomHome=null;
function zoomOpen(img){zoomClose(); zoomHome=img.parentNode; zoomed=img;
  $('zoom').insertBefore(img,$('zoom').firstChild); $('zoom').classList.add('on');}
function zoomClose(){if(!zoomed)return; zoomHome.insertBefore(zoomed,zoomHome.firstChild);
  zoomed=null; $('zoom').classList.remove('on');}
document.querySelectorAll('.zoomable').forEach(i=>i.addEventListener('click',()=>zoomed===i?zoomClose():zoomOpen(i)));
$('zclose').addEventListener('click',zoomClose);
document.addEventListener('keydown',e=>{if(e.key==='Escape')zoomClose();});
$('zswitch').addEventListener('click',()=>{
  if(document.body.classList.contains('remote')){
    zoomOpen($(zoomed&&zoomed.id==='rcam-bambu'?'rcam-webcam':'rcam-bambu'));
  }else{camSrc=camSrc==='bambu'?'webcam':'bambu'; camStart();}});

if(document.body.classList.contains('remote')) remoteCams(); else camStart();
tick(); setInterval(tick,2000);
</script>
</body></html>"""


if __name__ == "__main__":
    threading.Thread(target=_mqtt_loop, daemon=True).start()
    threading.Thread(target=_autoslow_loop, daemon=True).start()
    from werkzeug.serving import make_server
    remote = make_server("127.0.0.1", REMOTE_PORT, app, threaded=True)
    threading.Thread(target=remote.serve_forever, daemon=True).start()
    if siteconf.get("mobile_enabled"):
        from mobile.app import create_app
        mobile_app = create_app(_mobile_status, _cameras, devices=_mobile_devices, push=_mobile_push,
                                origin=siteconf.get("mobile_origin"))
        mobile_server = make_server("127.0.0.1", 8080, mobile_app, threaded=True)
        threading.Thread(target=mobile_server.serve_forever, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
