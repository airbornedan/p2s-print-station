"""Start a print of a .gcode.3mf already on the P2S USB stick. LAN + Developer mode only.
    P2S_ACCESS_CODE=<code> python3 start.py <name.gcode.3mf> <ams_slot 0-3> [url] [--local <path to the same .gcode.3mf>]
Maps the model filament to the given AMS slot; bed-level on, calibrations off for speed.
ams_mapping needs one entry per project filament (-1 = unused); with --local it is read from the
file's slice_info, so a project with several filaments (model on filament 2 of 5) maps correctly.
A file on the USB/SD card is addressed as ftp:///<name> (per OpenBambuAPI), NOT file:///sdcard/<name>."""
import json, re, sys, time, zipfile, p2s
args = sys.argv[1:]
local = None
if "--local" in args:
    i = args.index("--local"); local = args[i + 1]; del args[i:i + 2]
name, slot = args[0], int(args[1])
url = args[2] if len(args) > 2 else "ftp:///%s" % name

def mapping(path):
    """[-1, slot, -1, ...]: one entry per project filament, the used ones on `slot`."""
    if not path:
        return [slot]
    info = zipfile.ZipFile(path).read("Metadata/slice_info.config").decode()
    m = re.search(r'key="filament_maps" value="([^"]*)"', info)
    used = [int(x) for x in re.findall(r'<filament id="(\d+)"', info)]
    n = max([len(m.group(1).split()) if m else 0] + used)
    return [slot if i + 1 in used else -1 for i in range(n)]

ams_mapping = mapping(local)
ams_mapping2 = [{"ams_id": 0, "slot_id": s} if s >= 0 else {"ams_id": 255, "slot_id": 255} for s in ams_mapping]
seq = p2s.sequence_id()
payload = {"print": {"sequence_id": seq, "command": "project_file",
    "param": "Metadata/plate_1.gcode", "subtask_name": name.replace(".gcode.3mf", ""),
    "url": url, "md5": "", "file": "",
    "bed_type": "auto", "timelapse": False, "bed_leveling": True,
    "flow_cali": False, "vibration_cali": True, "layer_inspect": False,
    "use_ams": True, "ams_mapping": ams_mapping, "ams_mapping2": ams_mapping2,
    "profile_id": "0", "project_id": "0", "subtask_id": "0", "task_id": "0"}}
job = name.replace(".gcode.3mf", "")
acks, states = [], []
def on_message(c, u, m):
    p = json.loads(m.payload).get("print", {})
    if p.get("command") == "project_file" and p.get("sequence_id") == seq:
        acks.append((p.get("result"), p.get("reason")))
    if "gcode_state" in p:
        states.append((p["gcode_state"], p.get("subtask_name"), p.get("print_error")))
c, sn = p2s.connect(on_message)
c.loop_start(); time.sleep(1)
states.clear()          # only reports that arrive after the request count
p2s.request(c, sn, payload)
started = False
for i in range(40):     # up to 20 s for the printer to pick the job up
    time.sleep(0.5)
    if acks and (acks[-1][0] or "").upper() not in ("", "SUCCESS"):
        break           # refused outright
    if any(st in ("PREPARE", "SLICING", "RUNNING") and sub == job for st, sub, _ in states):
        started = True
        break
    if i % 6 == 5:
        p2s.request(c, sn, {"pushing": {"sequence_id": "2", "command": "pushall"}})
c.loop_stop(); c.disconnect()
print("ack:", acks[-1] if acks else "none")
print("state:", states[-1] if states else "none")
# Exit 0 only when the printer reports this job preparing or running (an old RUNNING/SUCCESS doesn't count).
sys.exit(0 if started else 1)
