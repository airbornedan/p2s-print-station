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


def connect(on_message=None):
    import paho.mqtt.client as mqtt
    sn = serial()
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.username_pw_set("bblp", code())
    c.tls_set(cert_reqs=ssl.CERT_NONE); c.tls_insecure_set(True)
    if on_message:
        c.on_message = on_message
    c.connect(IP, MQTT_PORT, 20)
    c.subscribe(f"device/{sn}/report")
    return c, sn


def sequence_id():
    """Fresh id for a command; the printer echoes it in its reply, which ties the reply to the command."""
    import time as _t
    return str(int(_t.time() * 1000) % 1000000000)


def request(c, sn, payload):
    import json as _j
    c.publish(f"device/{sn}/request", _j.dumps(payload))
