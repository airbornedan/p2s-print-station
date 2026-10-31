"""Grab from the P2S chamber camera. LAN + Developer mode only (port 322 is closed in cloud mode).

The P2S exposes RTSP-over-TLS on port 322, like the X1 (not the P1/A1 port-6000 binary stream):
    rtsps://bblp:<access_code>@<ip>:322/streaming/live/1   (H.264, 1920x1080, 30 fps)

    P2S_ACCESS_CODE=<lan code> python3 cam.py snap out.jpg      # one still
    P2S_ACCESS_CODE=<lan code> python3 cam.py clip 10 out.mp4    # N-second H.264 clip (stream copy)
"""
import os, subprocess, sys
sys.path.insert(0, os.path.dirname(__file__))
import p2s

def url():
    return "rtsps://bblp:%s@%s:%d/streaming/live/1" % (p2s.code(), p2s.IP, p2s.CAM_PORT)

def snap(out):
    # -update 1 writes a single image; RTSP over TCP is the reliable transport here.
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-rtsp_transport", "tcp",
                    "-i", url(), "-frames:v", "1", "-update", "1", out], check=True)
    print("wrote", out)

def clip(secs, out):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-rtsp_transport", "tcp",
                    "-i", url(), "-t", str(secs), "-c", "copy", out], check=True)
    print("wrote", out)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snap"
    if cmd == "snap":
        snap(sys.argv[2] if len(sys.argv) > 2 else "cam.jpg")
    elif cmd == "clip":
        clip(int(sys.argv[2]), sys.argv[3])
    else:
        raise SystemExit("usage: cam.py snap <out.jpg> | cam.py clip <secs> <out.mp4>")
