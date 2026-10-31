#!/usr/bin/env python3
"""Remote access switch for the print station (https://<your server>/printer/). Off unless turned on.

    printer-remote on [hours]      open for N hours (default 4); no IPs allowed yet
    printer-remote allow [ip|any]  allow an IP (default: the IP of the latest relay login); any = every address (password still needed)
    printer-remote extend [hours]  push the expiry out N hours from now/expiry (default 1)
    printer-remote off             close now
    printer-remote status

The tunnel (print-remote-tunnel.service) closes itself at expiry, and the droplet's cron
removes its proxy config at the same time on its own clock. Hosts and keys come from config.toml
(remote_admin, tunnel_target, tunnel_key); see tools/bambu/siteconf.py.
"""
import os, subprocess, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "tools", "bambu"))
import siteconf  # noqa: E402

DROPLET = siteconf.get("remote_admin")   # runs printer-remote-ctl on the web server
STATE = siteconf.path("state_dir")
EXPIRES = os.path.join(STATE, "remote_expires")
IPS = os.path.join(STATE, "remote_ips")
UNIT = "print-remote-tunnel.service"
TUNNEL = ["ssh", "-NT", "-i", siteconf.path("tunnel_key"), "-o", "BatchMode=yes",
          "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3", "-o", "ExitOnForwardFailure=yes",
          "-R", "127.0.0.1:{0}:localhost:{0}".format(siteconf.get("remote_port")),
          siteconf.get("tunnel_target")]   # station's remote-only listener


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _write(path, text):
    os.makedirs(STATE, exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def expires():
    """Expiry as epoch seconds; 0 when off."""
    try:
        return int(_read(EXPIRES, "0"))
    except ValueError:
        return 0


def is_on():
    return expires() > time.time()


def allowed_ips():
    return _read(IPS).split()


def _droplet(*args):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", DROPLET,
                        "printer-remote-ctl"] + list(args), capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError("droplet: " + (r.stderr.strip() or "failed"))
    return r.stdout.strip()


def on(hours=4.0):
    exp = int(time.time() + hours * 3600)
    _droplet("on", str(exp))           # local state only once the droplet side is up
    _write(EXPIRES, str(exp)); _write(IPS, "")
    try:
        subprocess.run(["systemctl", "--user", "restart", UNIT], check=True)
    except Exception:
        off()
        raise RuntimeError("tunnel failed to start; access left off")
    return exp


def allow(ip=None):
    if not is_on():
        raise RuntimeError("remote access is off; run 'printer-remote on' first")
    ip = ip or _droplet("lastjump")
    if not ip:
        raise RuntimeError("no relay login found in the last 12 h; give the IP")
    _droplet("allow", ip)
    if ip not in allowed_ips():
        _write(IPS, " ".join(allowed_ips() + [ip]))
    return ip


def extend(hours=1.0):
    if not is_on():
        raise RuntimeError("remote access is off")
    exp = int(max(time.time(), expires()) + hours * 3600)
    _droplet("extend", str(exp))
    _write(EXPIRES, str(exp))
    return exp


def off():
    _write(EXPIRES, "0"); _write(IPS, "")
    subprocess.run(["systemctl", "--user", "stop", UNIT])
    _droplet("off")


def tunnel():
    """Service body: keep the reverse tunnel up until expiry, then close both ends."""
    proc = None
    next_snapshot = 0
    try:
        while is_on():
            if proc is None or proc.poll() is not None:
                proc = subprocess.Popen(TUNNEL)
            if time.monotonic() >= next_snapshot:
                publisher = os.path.join(os.path.dirname(__file__), "..", "json_status", "publish.py")
                python = siteconf.path("venv_python")
                try:
                    subprocess.run([python, publisher], check=True, timeout=45)
                except (subprocess.SubprocessError, OSError):
                    print("status snapshot failed; will retry in five minutes", file=sys.stderr)
                next_snapshot = time.monotonic() + 300
            time.sleep(5)
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
        if not is_on():
            _write(EXPIRES, "0"); _write(IPS, "")
            try:
                _droplet("off")
            except Exception as e:
                print("droplet off failed (its cron expires it anyway):", e, file=sys.stderr)


def status():
    if not is_on():
        return "off"
    left = int(expires() - time.time())
    return "on until %s (%dh%02dm left), ips: %s" % (
        time.strftime("%H:%M", time.localtime(expires())), left // 3600, left % 3600 // 60,
        " ".join(allowed_ips()) or "none yet")


def main(argv):
    cmd = argv[0] if argv else "status"
    arg = argv[1] if len(argv) > 1 else None
    if cmd == "on":
        on(float(arg or 4))
    elif cmd == "allow":
        print("allowed", allow(arg))
    elif cmd == "extend":
        extend(float(arg or 1))
    elif cmd == "off":
        off()
    elif cmd == "_tunnel":
        return tunnel()
    elif cmd != "status":
        print(__doc__); return 2
    print(status())


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except RuntimeError as e:
        print(e, file=sys.stderr); sys.exit(1)
