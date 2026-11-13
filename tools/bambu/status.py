"""Print the P2S status report: job state, temps, nozzle, and every AMS slot. Read-only; works in cloud or LAN mode.
    python3 status.py          (or P2S_ACCESS_CODE=<lan code> python3 status.py after a mode switch)"""
import time, p2s
got = {}
def on_message(c, u, m):
    import json
    d = json.loads(m.payload).get("print", {})
    if "gcode_state" in d or "ams" in d:
        got.update(d)
c, sn = p2s.connect(on_message)
c.loop_start()
p2s.request(c, sn, {"pushing": {"sequence_id": "0", "command": "pushall"}})
for _ in range(16):
    if "ams" in got: break
    time.sleep(0.5)
c.loop_stop(); c.disconnect()
if not got:
    raise SystemExit("no report (wrong access code, or printer still settling after a mode switch)")
p = got
print("state: %s | file: %s | %s%% | %s min left" % (p.get("gcode_state"), p.get("subtask_name"),
      p.get("mc_percent"), p.get("mc_remaining_time")))
print("nozzle %s/%s C  bed %s/%s C  nozzle size %s  sdcard(USB) %s" % (p.get("nozzle_temper"),
      p.get("nozzle_target_temper"), p.get("bed_temper"), p.get("bed_target_temper"),
      p.get("nozzle_diameter"), p.get("sdcard")))
for unit in (p.get("ams") or {}).get("ams", []):
    for t in unit.get("tray", []):
        print("  AMS %s slot %s: %s %s #%s%s remain %s%%" % (unit.get("id"), t.get("id"), t.get("tray_type"),
              t.get("tray_info_idx"), (t.get("tray_color") or "")[:6], " clear" if p2s.tray_clear(t) else "",
              t.get("remain")))
