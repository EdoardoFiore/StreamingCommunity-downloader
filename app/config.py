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

# Jellyfin authentication is opt-in: set AUTH_ENABLED=1 to get the login screen,
# the setup wizard, the request queue and user management. Left unset, the panel
# runs open — no login, every request gets the same implicit permissions
# (download, settings, file manager, library) — which is what the panel did
# before SSO existed.
#
# The default is 0 deliberately, and it is the upgrade path: an existing
# deployment's compose file predates this variable, so pulling a newer image
# keeps behaving exactly as before instead of suddenly demanding a Jellyfin
# server. Nothing here can distinguish an upgrade from a fresh install — there
# is no persistent state that says so — hence a safe default rather than a
# guess. The shipped compose file sets AUTH_ENABLED=1 explicitly, so new
# deployments do get authentication.
#
# There is also a runtime, DB-backed equivalent, used when AUTH_ENABLED=1: an
# admin can choose "Continua senza Jellyfin" in the setup wizard
# (POST /api/auth/skip), reaching the same open mode without an env var or a
# restart, and can connect Jellyfin later from Settings
# (POST /api/auth/jellyfin-connect). See app.auth.models.SETTING_AUTH_MODE /
# runtime_open_mode().
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "0") == "1"

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
