# P2S print station

LAN web page for a Bambu Lab P2S: pick a file, place it, slice, preview, print, watch. No Bambu Studio GUI or Handy.

Optional: remote monitoring through your web server, a phone app with push alerts, email alerts, public JSON status.

**Caveats:** unofficial; firmware updates may break it. Flask's dev server: keep it on the LAN or behind the remote layer. Print control needs the printer in LAN + Developer mode.

## Needs

- Linux, Python 3.11+, ffmpeg
- Bambu Studio AppImage (command-line slicing)
- USB stick in the printer's screen port

## Install

```
git clone https://github.com/airbornedan/p2s-print-station ~/print-station
python3 -m venv ~/.local/share/print_station/venv
~/.local/share/print_station/venv/bin/pip install -r ~/print-station/requirements.txt
mkdir -p ~/.config/print_station
cp ~/print-station/config.example.toml ~/.config/print_station/config.toml   # then edit
```

Extract the P2S profiles from the AppImage (not shipped; AGPL):

```
cd ~/.local/share/print_station/bbl && <AppImage> --appimage-extract 'resources/profiles/BBL*'
```

Run: `cd ~/print-station/print_station && ~/.local/share/print_station/venv/bin/python station.py`, open `http://<host>:8078/`.
As a service: `deploy/print-station.service` → `~/.config/systemd/user/`, then `systemctl --user enable --now print-station`.

Vibration calibration runs on every 12th send (`vibration_every`), or when ticked on Send.

Access code: from Bambu Studio's saved config, `$P2S_ACCESS_CODE`, or the page. It changes on cloud ↔ LAN switch.

## Email alerts

`~/.config/print_station/mail.toml`:

```
[mail]
smtp_host = "smtp.example.com"
smtp_port = 587
from_address = "printer@example.com"
to_address = "you@example.com"
smtp_password = "..."
# smtp_user = "..."   # if not the from address
```

## Remote monitoring (optional)

Timed, password-protected `https://<server>/printer/` via reverse SSH tunnel. Remote can monitor, pause, resume, stop and skip parts; not slice or start.

Server (Apache):
1. `a2enmod proxy proxy_http headers`; add `IncludeOptional /etc/apache2/printer-remote/*.conf` to the HTTPS vhost.
2. `remote/printer-remote-ctl` → `/usr/local/sbin/`; cron: `* * * * * root /usr/local/sbin/printer-remote-ctl expire`.
3. `htpasswd -c /etc/apache2/.htpasswd-printer <user>`.
4. Key-only `tunnel` account, limited to `PermitListen 127.0.0.1:8079`. Station host's key for `remote_admin`.

Station host: install `remote/print-remote-tunnel.service` as a user unit; link `remote/printer_remote.py` as `printer-remote`.

```
printer-remote on 2      # open 2 hours
printer-remote allow <ip|any>
printer-remote off
```

**JSON status:** while remote is on, the station pushes a sanitized snapshot every 5 min. Server: key-only `printer-status` account with forced command `python3 json_status/receive.py` (plus `schema.py`), writable `/var/lib/printer-status`; serve `json_status/status.php`.

**Phone app:** see `print_station/mobile/README.md`.

## Tests

From `print_station/`:

```
python -m unittest test_gcodecheck test_plate_watchdog test_printer_stage mobile.test_mobile mobile.test_push
(cd json_status && python -m unittest discover -p 'test_*.py')
```

## License

MIT
