import json
import os
from pathlib import Path

from filelock import FileLock

VIDEOS_DIR = Path(os.getenv("VIDEOS_DIR", "videos"))
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
DATA_FILE = Path(os.getenv("DATA_FILE", "data.json"))
SCHEDULE_FILE = Path(os.getenv("SCHEDULE_FILE", "schedule.json"))
TMP_DIR = Path(os.getenv("TMP_DIR", "tmp"))

# ── Panel database (users, sessions, requests, notifications) ─────────────────
# Must live on a persistent volume when running in Docker, and must not be
# served to clients: it holds the Jellyfin service API key.
DB_FILE = Path(os.getenv("DB_FILE", "panel.db"))

# Set COOKIE_SECURE=1 when the panel is served over HTTPS.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"

# Trust X-Forwarded-For / X-Real-IP for the client address. Only enable when the
# panel sits behind a reverse proxy you control — otherwise the header is
# attacker-controlled and would poison the IP reported to Jellyfin.
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "0") == "1"

SETTINGS_DEFAULTS = {
    "max_concurrent_downloads": 3,
    "max_segment_workers": 16,
}


def read_data() -> dict:
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def configured_domain() -> str:
    """The source domain, as configured by an administrator.

    Every outbound request to the source resolves its host through here. The
    endpoints used to accept a `domain` from the client, which let any signed-in
    user make the server issue HTTP requests to a host of their choosing.
    """
    return (read_data().get("domain") or "").strip()


def get_settings() -> dict:
    try:
        with open(DATA_FILE) as f:
            data = json.load(f)
        return {**SETTINGS_DEFAULTS, **data.get("settings", {})}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(SETTINGS_DEFAULTS)


def save_settings(new_settings: dict):
    lock = FileLock(str(DATA_FILE) + ".lock")
    with lock:
        try:
            with open(DATA_FILE) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        data["settings"] = new_settings
        with open(DATA_FILE, "w") as f:
            json.dump(data, f)
