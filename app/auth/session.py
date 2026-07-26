"""Server-side sessions.

A signed token (JWT and friends) would be simpler but cannot be revoked before
it expires, and disabling a user has to take effect on the next request. So the
session is a row: deleting it ends access instantly.

Only the SHA-256 of the token is stored. The raw value exists in the cookie and
nowhere else, so a copy of panel.db does not hand out working sessions.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from app import db
from app.auth.models import User, _row_to_user

logger = logging.getLogger(__name__)

SESSION_COOKIE = "sc_session"
CSRF_HEADER = "X-CSRF-Token"
SESSION_TTL = timedelta(days=30)
# Sliding expiry is refreshed at most this often, to avoid a write per request.
TOUCH_INTERVAL = timedelta(minutes=5)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def create_session(user_id: int) -> tuple[str, str]:
    """Create a session, returning ``(raw_token, csrf_token)``."""
    raw_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = _now()
    db.execute(
        "INSERT INTO jf_session(token_hash, user_id, csrf_token, created_at, expires_at, "
        "last_seen_at) VALUES(?, ?, ?, ?, ?, ?)",
        (
            _hash(raw_token),
            user_id,
            csrf_token,
            now.isoformat(),
            (now + SESSION_TTL).isoformat(),
            now.isoformat(),
        ),
    )
    return raw_token, csrf_token


def resolve_session(raw_token: str) -> tuple[User, str] | None:
    """Return ``(user, csrf_token)`` for a valid session, or None.

    A session is valid only while it is unexpired *and* its user is still
    enabled; a disabled user's rows are deleted on disable, and the join here is
    a second line of defence.
    """
    if not raw_token:
        return None
    row = db.query_one(
        "SELECT s.token_hash, s.csrf_token, s.expires_at, s.last_seen_at, u.* "
        "FROM jf_session s JOIN jf_user u ON u.id = s.user_id "
        "WHERE s.token_hash = ?",
        (_hash(raw_token),),
    )
    if row is None:
        return None

    now = _now()
    if _parse(row["expires_at"]) <= now:
        db.execute("DELETE FROM jf_session WHERE token_hash = ?", (row["token_hash"],))
        return None
    if not row["enabled"]:
        db.execute("DELETE FROM jf_session WHERE token_hash = ?", (row["token_hash"],))
        return None

    if now - _parse(row["last_seen_at"]) > TOUCH_INTERVAL:
        db.execute(
            "UPDATE jf_session SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
            (now.isoformat(), (now + SESSION_TTL).isoformat(), row["token_hash"]),
        )

    return _row_to_user(row), row["csrf_token"]


def delete_session(raw_token: str):
    if raw_token:
        db.execute("DELETE FROM jf_session WHERE token_hash = ?", (_hash(raw_token),))


def delete_user_sessions(user_id: int):
    db.execute("DELETE FROM jf_session WHERE user_id = ?", (user_id,))


def purge_expired():
    db.execute("DELETE FROM jf_session WHERE expires_at <= ?", (_now().isoformat(),))
