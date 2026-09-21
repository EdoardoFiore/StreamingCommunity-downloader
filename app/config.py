import json
import logging
import os
from pathlib import Path

from filelock import FileLock

logger = logging.getLogger(__name__)

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

_SAMESITE_VALUES = {"lax", "strict", "none"}


def _resolve_samesite(raw_value: str, secure: bool) -> str:
    """Validate COOKIE_SAMESITE and warn about the one way it can misfire.

    "lax" (default) is the safe choice for every normal deployment. Embedding
    the panel in an iframe on a different site or scheme — e.g. a Jellyfin
    custom tab — needs "none": browsers do not send a Lax cookie from within a
    cross-site frame at all, which looks like an infinite login loop (each
    request inside the frame arrives with no session, so it bounces straight
    back to the login page). CSRF protection does not depend on SameSite here
    — every state-changing request is already checked against a double-submit
    token an outside origin cannot read — so relaxing this is safe once the
    prerequisite below is met.

    A pure function so the validation and warning logic can be tested without
    reimporting or reloading this module.
    """
    value = (raw_value or "").strip().lower()
    if value not in _SAMESITE_VALUES:
        logger.warning("Invalid COOKIE_SAMESITE=%r, falling back to 'lax'", raw_value)
        value = "lax"
    if value == "none" and not secure:
        # Mandatory per spec: browsers silently drop a SameSite=None cookie
        # that isn't also Secure, which would make login look broken in a
        # different, more confusing way than the problem this setting exists
        # to fix.
        logger.warning(
            "COOKIE_SAMESITE=none requires COOKIE_SECURE=1 (HTTPS) or browsers "
            "will reject the session cookie outright — set COOKIE_SECURE=1 as well."
        )
    return value


COOKIE_SAMESITE = _resolve_samesite(os.getenv("COOKIE_SAMESITE", "lax"), COOKIE_SECURE)

# Trust X-Forwarded-For / X-Real-IP for the client address. Only enable when the
# panel sits behind a reverse proxy you control — otherwise the header is
# attacker-controlled and would poison the IP reported to Jellyfin.
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "0") == "1"

# Whether the panel asks for a Jellyfin login is not configured here, and there
# is no environment variable for it. It is chosen once, by a human, in the setup
# wizard — "Collega a Jellyfin" (POST /api/auth/setup) or "Continua senza
# Jellyfin" (POST /api/auth/skip) — and stored in the database as the
# ``auth_mode`` setting. Open mode can be left later from Settings
# (POST /api/auth/jellyfin-connect), without a restart. See
# app.auth.models.SETTING_AUTH_MODE / runtime_open_mode().
#
# ``AUTH_ENABLED`` used to decide this at deploy time. It was removed because
# it could not be reconciled with the wizard: set to 0 it did not merely
# default to open mode, it made the wizard unreachable, so the GUI choice the
# panel advertised did not exist and Settings offered a "Collega a Jellyfin"
# button that answered 404. Databases predating the removal are carried over
# by migration v8 in app/db.py, which is the only place that still reads it.

SETTINGS_DEFAULTS = {
    "max_concurrent_downloads": 3,
    "max_segment_workers": 16,
    # How often followed series are checked for new episodes, in minutes.
    "series_watch_interval_minutes": 240,
    # Whether to look for a replacement when the source domain stops answering.
    "domain_auto_check_enabled": True,
    # Whether to adopt the replacement without asking. Off: a domain found on a
    # page we do not control is proposed to an administrator, never applied on
    # its own. See app.core.domain_recovery.
    "domain_auto_apply": False,
    "domain_check_interval_minutes": 360,
    # Ask Jellyfin to scan when a download lands, instead of waiting for its
    # own schedule. Inert unless Jellyfin is connected.
    "jellyfin_refresh_on_download": False,
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


def update_data(changes: dict):
    """Apply top-level changes to data.json under the shared lock.

    Read-modify-write inside the lock rather than "read it somewhere, mutate,
    write it back": the domain recovery loop writes ``domain`` from a background
    thread while an administrator may be saving libraries, and the last writer
    would otherwise drop the other's key wholesale.

    This lives here, not in the domain router, because ``app.core`` writes the
    domain too and importing a router from ``app.core`` would be a cycle.
    """
    lock = FileLock(str(DATA_FILE) + ".lock")
    with lock:
        try:
            with open(DATA_FILE) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        data.update(changes)
        with open(DATA_FILE, "w") as f:
            json.dump(data, f)


def save_settings(new_settings: dict):
    update_data({"settings": new_settings})
