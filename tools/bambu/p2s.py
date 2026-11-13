"""Shared P2S LAN helpers: printer IP, serial and access code, and an MQTT connection.

Settings come from siteconf (config.toml). The access code is read from $P2S_ACCESS_CODE or access_code
in config.toml, else the code saved in Bambu Studio's config. It CHANGES when the printer is switched between
cloud and LAN/Developer mode, so pass it explicitly after a mode switch:
    P2S_ACCESS_CODE=<lan code> python3 status.py
The serial comes from the same config. Control commands (start/stop/pause/ledctrl) are only accepted in
LAN + Developer mode; in cloud mode the printer answers "verify failed". Status, file listing and USB upload
work in both modes. The code is a credential: never commit it or write it into the notes."""
import json, os, re, ssl, time

import siteconf

IP = siteconf.get("printer_ip")
CAM_PORT, FTPS_PORT, MQTT_PORT = 322, 990, 8883   # 322 (camera) only answers in LAN mode


def _conf():
    s = open(siteconf.path("bambu_studio_conf")).read()
    pairs = re.findall(r'"([^"]+)"\s*:\s*"([^"]*)"', re.search(r'"(?:user_)?access_code"\s*:\s*\{([^}]*)\}', s).group(1))
    return pairs[0]   # (serial, saved_code)


def serial():
    return _conf()[0]


def code():
    return siteconf.get("access_code") or _conf()[1]


def connect(on_message=None, on_up=None, on_down=None):
    """Connect and subscribe to reports. paho reconnects by itself after a drop but forgets the
    subscription, so subscribe again on every (re)connect. on_up/on_down: optional link callbacks."""
    import paho.mqtt.client as mqtt
    sn = serial()
    topic = f"device/{sn}/report"
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.username_pw_set("bblp", code())
    c.tls_set(cert_reqs=ssl.CERT_NONE); c.tls_insecure_set(True)
    if on_message:
        c.on_message = on_message

    def up(client, userdata, flags, reason, props):
        if not reason.is_failure:
            client.subscribe(topic)
            if on_up:
                on_up()
    c.on_connect = up
    if on_down:
        c.on_disconnect = lambda client, userdata, flags, reason, props: on_down()
    c.connect(IP, MQTT_PORT, 20)
    c.subscribe(topic)
    return c, sn


def sequence_id():
    """Fresh id for a command; the printer echoes it in its reply, which ties the reply to the command."""
    import time as _t
    return str(int(_t.time() * 1000) % 1000000000)


def request(c, sn, payload):
    import json as _j
    c.publish(f"device/{sn}/request", _j.dumps(payload))


def tray_clear(tray):
    """True for a clear/translucent spool: colour alpha below FF (clear PETG reports 00000000) or a name saying so."""
    col = (tray.get("tray_color") or "").upper()
    name = (tray.get("tray_sub_brands") or "").lower()
    return (len(col) == 8 and col[6:] != "FF") or any(w in name for w in ("translucent", "clear", "transparent"))
