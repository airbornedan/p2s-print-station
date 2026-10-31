# Phone app

Home Screen web app: status, AMS, camera, push alerts. Read-only: no printer controls.

Separate loopback Flask app (port 8080) reached through a restricted reverse SSH tunnel. Devices enroll with a one-use code; sessions are hashed, HttpOnly, Strict cookies.

## Setup

1. Point a hostname (e.g. `print.example.com`) at your web server.
2. Server: install `deploy/print-mobile-*.conf` (edit hostname), get a certificate, add `deploy/reload-apache-after-renewal` as a certbot deploy hook.
3. Server: key-only `printer-mobile` account; append `deploy/sshd-match.conf` to `sshd_config`.
4. Station host: set the server in `deploy/print-mobile-tunnel.service`, install as a user unit, enable.
5. `config.toml`: `mobile_enabled = true`, `mobile_origin = "https://print.example.com"`. Restart the station.
6. From `print_station/`: `python -m mobile.device_admin enroll`. Enter the code on the phone, Add to Home Screen, enable notifications.

## Tests

From `print_station/`: `python -m unittest mobile.test_mobile mobile.test_push`; `node --check mobile/static/app.js`
