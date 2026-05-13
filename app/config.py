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

SETTINGS_DEFAULTS = {
    "max_concurrent_downloads": 3,
    "max_segment_workers": 16,
}


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
