"""Announce the printer to Bambu Studio on this machine, so Studio finds it in LAN mode at every launch.

The printer's own discovery broadcasts don't reach Master Control, so Studio never rediscovers it and falls back to
the cloud (offline in LAN + Developer mode). This sends the same SSDP NOTIFY to 127.0.0.1:2021 every few seconds;
Studio then connects directly with the access code it already has. Local only: nothing leaves the machine.
    python3 ssdp_announce.py            (printer IP from print-station config, serial from Studio's config)
Runs as the user service bambu-ssdp.service."""
import json, os, socket, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import siteconf

IP = siteconf.get("printer_ip")
SERIAL = json.load(open(os.path.expanduser("~/.config/BambuStudio/BambuStudio.conf")))["app"]["user_last_selected_machine"]
MODEL, NAME, EVERY = "N7", "P2S", 5          # N7 = P2S in Bambu's profiles

MSG = ("NOTIFY * HTTP/1.1\r\n"
       "HOST: 239.255.255.250:1990\r\n"
       "Server: UPnP/1.0\r\n"
       f"Location: {IP}\r\n"
       "NT: urn:bambulab-com:device:3dprinter:1\r\n"
       f"USN: {SERIAL}\r\n"
       "Cache-Control: max-age=1800\r\n"
       f"DevModel.bambu.com: {MODEL}\r\n"
       f"DevName.bambu.com: {NAME}\r\n"
       "DevSignal.bambu.com: -40\r\n"
       "DevConnect.bambu.com: lan\r\n"
       "DevBind.bambu.com: free\r\n"
       "Devseclink.bambu.com: secure\r\n"
       "\r\n").encode()

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
while True:
    try:
        s.sendto(MSG, ("127.0.0.1", 2021))
    except OSError:
        pass
    time.sleep(EVERY)
