"""Send G-code lines to the P2S (MQTT gcode_line, per OpenBambuAPI). LAN + Developer mode only.
For one-off machine settings such as Bambu's purge-chute offset files; not for printing.
    python3 gcode.py <file.gcode>             show the lines and the printer state; sends nothing
    python3 gcode.py <file.gcode> --yes       send them
Refuses unless the printer is idle (IDLE, FINISH or FAILED). Each send is appended to
~/.local/share/print_station/gcode_sent.log."""
import json, os, sys, time, p2s

IDLE = ("IDLE", "FINISH", "FAILED")
LOG = os.path.expanduser("~/.local/share/print_station/gcode_sent.log")

args = sys.argv[1:]
send = "--yes" in args
args = [a for a in args if a != "--yes"]
if len(args) != 1:
    sys.exit(__doc__)
path = args[0]
lines = [l.split(";")[0].strip() for l in open(path)]
lines = [l for l in lines if l]
if not lines:
    sys.exit("no G-code in %s" % path)

state, acks, seq = {}, [], p2s.sequence_id()
def on_message(c, u, m):
    p = json.loads(m.payload).get("print", {})
    if "gcode_state" in p:
        state["s"] = p["gcode_state"]
    if p.get("command") == "gcode_line" and p.get("sequence_id") == seq:
        acks.append((p.get("result"), p.get("reason")))

c, sn = p2s.connect(on_message)
c.loop_start()
p2s.request(c, sn, {"pushing": {"sequence_id": "0", "command": "pushall"}})
for _ in range(20):
    if "s" in state: break
    time.sleep(0.5)
st = state.get("s")
print("printer state:", st)
print("\n".join("  " + l for l in lines))
if st not in IDLE:
    c.loop_stop(); c.disconnect()
    sys.exit("refused: printer is %s, not idle" % st)
if not send:
    c.loop_stop(); c.disconnect()
    sys.exit("not sent: add --yes to send")

p2s.request(c, sn, {"print": {"sequence_id": seq, "command": "gcode_line", "param": "\n".join(lines) + "\n"}})
for _ in range(20):
    if acks: break
    time.sleep(0.5)
c.loop_stop(); c.disconnect()
ack = acks[-1] if acks else None
os.makedirs(os.path.dirname(LOG), exist_ok=True)
with open(LOG, "a") as f:
    f.write("%s %s ack=%s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.path.basename(path), ack, " | ".join(lines)))
print("sent; ack:", ack or "none (cloud mode rejects control; needs LAN + Developer mode)")
