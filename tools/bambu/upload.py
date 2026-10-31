"""Upload one file to the P2S USB stick over implicit FTPS (port 990); works in cloud or LAN mode.
Needs a USB drive in the screen-box port, or the printer reports sdcard:false and there is nowhere to store it.
    python3 upload.py <file>"""
import os, sys, ftps
path = sys.argv[1]; name = os.path.basename(path)
f = ftps.connect()
with open(path, "rb") as fh:
    f.storbinary("STOR /" + name, fh)
remote, local = f.size("/" + name), os.path.getsize(path)
f.quit()
print("uploaded %s: %d bytes on the stick (local %d)" % (name, remote, local))
if remote != local:
    sys.exit("upload incomplete: %d of %d bytes" % (remote, local))
