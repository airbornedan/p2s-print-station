"""Site settings. Order: environment variable, then $PRINT_STATION_CONFIG
(default ~/.config/print_station/config.toml), then the placeholders below.
See config.example.toml."""
import os

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

# key: (default, environment variable or None)
DEFAULTS = {
    "printer_ip": ("192.0.2.10", "P2S_IP"),
    "access_code": ("", "P2S_ACCESS_CODE"),
    "bambu_studio_conf": ("~/.config/BambuStudio/BambuStudio.conf", None),
    "port": (8078, "PRINT_STATION_PORT"),
    "remote_port": (8079, "PRINT_STATION_REMOTE_PORT"),
    "inbox": ("~/print_inbox", "PRINT_INBOX"),
    "work_dir": ("~/.local/share/print_station/work", None),
    "state_dir": ("~/.local/state/print_station", None),
    "venv_python": ("~/.local/share/print_station/venv/bin/python", None),
    "appimage": ("~/applications/Bambu_Studio.AppImage", "PRINT_STATION_APPIMAGE"),
    "profiles_dir": ("~/.local/share/print_station/bbl/squashfs-root/resources/profiles/BBL", None),
    "webcam_url": ("http://192.0.2.20:8081", "PRINT_STATION_WEBCAM"),
    "camera_width": (1280, "PRINT_STATION_CAM_W"),
    "vibration_every": (12, None),
    "centre_keepout_mm": (30, None),
    "ntfy_topic_file": ("~/.config/ntfy/topic", None),   # phone alerts (ntfy.sh topic); missing file = off
    "stall_minutes": (20, None),
    "remote_admin": ("root@203.0.113.10", None),
    "tunnel_target": ("tunnel@example.com", None),
    "tunnel_key": ("~/.ssh/id_tunnel", None),
    "status_target": ("printer-status@203.0.113.10", None),
    "status_key": ("~/.ssh/printer_status", None),
    "mobile_enabled": (False, "PRINT_MOBILE_ENABLED"),
    "mobile_origin": ("https://print.example.com", "PRINT_MOBILE_ORIGIN"),
    "mail_config": ("~/.config/print_station/mail.toml", None),
    "filament_overrides": ({}, None),   # {filament profile: {slicer key: value}}, applied on every slice
}

_file = None


def _load():
    global _file
    if _file is None:
        path = os.path.expanduser(os.environ.get("PRINT_STATION_CONFIG", "~/.config/print_station/config.toml"))
        _file = {}
        if tomllib and os.path.isfile(path):
            with open(path, "rb") as f:
                _file = tomllib.load(f)
    return _file


def get(key):
    default, env = DEFAULTS[key]
    raw = os.environ.get(env) if env else None
    value = raw if raw not in (None, "") else _load().get(key, default)
    if isinstance(default, bool) and isinstance(value, str):
        value = value.strip().lower() in ("1", "true", "yes", "on")
    elif isinstance(default, int) and not isinstance(default, bool):
        value = int(value)
    return value


def path(key):
    """A setting that names a file or directory, with ~ expanded."""
    return os.path.expanduser(str(get(key)))
