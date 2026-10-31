import json, sys, time, p2s
log=[]
def on_message(c,u,m):
    p=json.loads(m.payload).get("print",{})
    if "gcode_state" in p: log.append(p["gcode_state"])
    if p.get("command") in ("stop","clean_print_error") or "result" in p:
        print("reply:", p.get("command"), p.get("result"), p.get("reason"))
c,sn=p2s.connect(on_message); c.loop_start(); time.sleep(1.5)
p2s.request(c,sn,{"pushing":{"sequence_id":"0","command":"pushall"}}); time.sleep(2)
print("before:", log[-1] if log else "?")
for cmd in ("stop","clean_print_error"):
    log.clear()
    p2s.request(c,sn,{"print":{"sequence_id":"1","command":cmd,"param":""}}); time.sleep(4)
    p2s.request(c,sn,{"pushing":{"sequence_id":"2","command":"pushall"}}); time.sleep(2)
    print("after %s:"%cmd, log[-1] if log else "?")
    if log and log[-1] in ("IDLE","FINISH"): break
c.loop_stop(); c.disconnect()
