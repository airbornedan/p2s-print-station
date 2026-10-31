"""Send one control command to the P2S. LAN + Developer mode only (cloud mode answers "verify failed").
    P2S_ACCESS_CODE=<lan code> python3 control.py {stop|pause|resume|light_on|light_off}"""
import json, sys, time, p2s
CMDS = {
    "stop":   {"print": {"command": "stop", "param": ""}},
    "pause":  {"print": {"command": "pause", "param": ""}},
    "resume": {"print": {"command": "resume", "param": ""}},
    "light_on":  {"system": {"command": "ledctrl", "led_node": "chamber_light", "led_mode": "on",
                             "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}},
    "light_off": {"system": {"command": "ledctrl", "led_node": "chamber_light", "led_mode": "off",
                             "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}},
}
cmd = sys.argv[1]
seq = p2s.sequence_id()
payload = CMDS[cmd]; next(iter(payload.values()))["sequence_id"] = seq
reply = []
def on_message(c, u, m):
    d = json.loads(m.payload)
    for sect in ("print", "system"):
        if d.get(sect, {}).get("sequence_id") == seq and "result" in d.get(sect, {}):
            reply.append((sect, d[sect].get("command"), d[sect].get("result"), d[sect].get("reason")))
c, sn = p2s.connect(on_message)
c.loop_start(); time.sleep(1)
p2s.request(c, sn, payload)
time.sleep(5); c.loop_stop(); c.disconnect()
print(cmd, "->", reply[-1] if reply else "no acknowledgement (cloud mode rejects control; needs LAN + Developer mode)")
