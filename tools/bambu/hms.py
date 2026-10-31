import json, time, p2s
got={}
def on_message(c,u,m):
    p=json.loads(m.payload).get("print",{})
    if "gcode_state" in p: got.update(p)
c,sn=p2s.connect(on_message); c.loop_start()
p2s.request(c,sn,{"pushing":{"sequence_id":"0","command":"pushall"}})
for _ in range(16):
    if got: break
    time.sleep(0.5)
c.loop_stop(); c.disconnect()
print("gcode_state:", got.get("gcode_state"), "| print_error:", got.get("print_error"), "| mc_print_error_code:", got.get("mc_print_error_code"))
print("hms:", got.get("hms"))
