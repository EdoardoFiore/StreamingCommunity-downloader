"""Data access for panel settings and users."""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from app import db
from app.auth import permissions as perms
from app.auth.jellyfin import device_id_for

logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Settings ───────────────────────────────────────────────────────────────────

SETTING_JELLYFIN_URL = "jellyfin_url"
SETTING_JELLYFIN_API_KEY = "jellyfin_api_key"
SETTING_JELLYFIN_SERVER_ID = "jellyfin_server_id"
SETTING_ALLOW_NEW_LOGIN = "allow_new_jellyfin_login"
SETTING_DEFAULT_PERMISSIONS = "default_permissions"

_SETTING_DEFAULTS = {
    SETTING_ALLOW_NEW_LOGIN: "0",  # off by default: users are imported explicitly
    SETTING_DEFAULT_PERMISSIONS: str(int(perms.DEFAULT_PERMISSIONS)),
}


def get_setting(key: str, default: str | None = None) -> str | None:
    row = db.query_one("SELECT value FROM jf_setting WHERE key = ?", (key,))
    if row is not None:
        return row["value"]
    return _SETTING_DEFAULTS.get(key, default)


def set_setting(key: str, value: str, conn: sqlite3.Connection | None = None):
    (conn or db.get_conn()).execute(
        "INSERT INTO jf_setting(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def allow_new_jellyfin_login() -> bool:
    return get_setting(SETTING_ALLOW_NEW_LOGIN) == "1"


def default_permissions() -> int:
    try:
        return perms.sanitize(int(get_setting(SETTING_DEFAULT_PERMISSIONS)))
    except (TypeError, ValueError):
        return int(perms.DEFAULT_PERMISSIONS)


def jellyfin_config() -> tuple[str | None, str | None]:
    """(base_url, service_api_key) — both None until setup has run."""
    return get_setting(SETTING_JELLYFIN_URL), get_setting(SETTING_JELLYFIN_API_KEY)


def setup_done() -> bool:
    """True once the first administrator exists and Jellyfin is configured."""
    row = db.query_one("SELECT COUNT(*) AS n FROM jf_user")
    return bool(row["n"]) and get_setting(SETTING_JELLYFIN_URL) is not None


# ── Users ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class User:
    id: int
    jellyfin_user_id: str
    username: str
    is_jellyfin_admin: bool
    permissions: int
    enabled: bool
    device_id: str
    created_at: str
    last_login_at: str | None

    @property
    def permission_names(self) -> list[str]:
        return perms.to_names(self.permissions)

    def has(self, *required, mode: str = "and") -> bool:
        return perms.has(self.permissions, *required, mode=mode)

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "jellyfin_user_id": self.jellyfin_user_id,
            "username": self.username,
            "is_jellyfin_admin": self.is_jellyfin_admin,
            "permissions": self.permissions,
            "permission_names": self.permission_names,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
        }


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        jellyfin_user_id=row["jellyfin_user_id"],
        username=row["username"],
        is_jellyfin_admin=bool(row["is_jellyfin_admin"]),
        permissions=row["permissions"],
        enabled=bool(row["enabled"]),
        device_id=row["device_id"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
    )


def get_user(user_id: int) -> User | None:
    row = db.query_one("SELECT * FROM jf_user WHERE id = ?", (user_id,))
    return _row_to_user(row) if row else None


def get_user_by_jellyfin_id(jellyfin_user_id: str) -> User | None:
    row = db.query_one(
        "SELECT * FROM jf_user WHERE jellyfin_user_id = ?", (jellyfin_user_id,)
    )
    return _row_to_user(row) if row else None


def list_users() -> list[User]:
    rows = db.query("SELECT * FROM jf_user ORDER BY username COLLATE NOCASE")
    return [_row_to_user(r) for r in rows]


def count_users() -> int:
    return db.query_one("SELECT COUNT(*) AS n FROM jf_user")["n"]


def create_user(
    jellyfin_user_id: str,
    username: str,
    permissions: int,
    is_jellyfin_admin: bool = False,
    enabled: bool = True,
    conn: sqlite3.Connection | None = None,
) -> User:
    executor = conn or db.get_conn()
    executor.execute(
        "INSERT INTO jf_user(jellyfin_user_id, username, is_jellyfin_admin, permissions, "
        "enabled, device_id, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
        (
            jellyfin_user_id,
            username,
            int(is_jellyfin_admin),
            perms.sanitize(permissions),
            int(enabled),
            device_id_for(username),
            now_iso(),
        ),
    )
    row = executor.execute(
        "SELECT * FROM jf_user WHERE jellyfin_user_id = ?", (jellyfin_user_id,)
    ).fetchone()
    return _row_to_user(row)


def touch_login(user_id: int, username: str, is_jellyfin_admin: bool):
    """Refresh the cached display name and admin flag after a successful login.

    The username is only a cache — Jellyfin allows renaming, so the stored value
    would otherwise drift away from reality. The device id follows the username,
    so a rename produces one new Jellyfin device and then stays stable again.
    """
    db.execute(
        "UPDATE jf_user SET username = ?, is_jellyfin_admin = ?, device_id = ?, "
        "last_login_at = ? WHERE id = ?",
        (username, int(is_jellyfin_admin), device_id_for(username), now_iso(), user_id),
    )


def set_permissions(user_id: int, permissions: int):
    db.execute(
        "UPDATE jf_user SET permissions = ? WHERE id = ?",
        (perms.sanitize(permissions), user_id),
    )


def set_enabled(user_id: int, enabled: bool):
    """Enable or disable a user.

    Disabling drops the user's sessions in the same transaction, which is what
    makes revocation immediate rather than "immediate once the token expires".
    """
    with db.tx() as conn:
        conn.execute("UPDATE jf_user SET enabled = ? WHERE id = ?", (int(enabled), user_id))
        if not enabled:
            conn.execute("DELETE FROM jf_session WHERE user_id = ?", (user_id,))


def count_users_with(permission) -> int:
    """How many enabled users hold a permission bit (used to prevent lockout)."""
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM jf_user WHERE enabled = 1 AND (permissions & ?) != 0",
        (int(permission),),
    )
    return row["n"]


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
