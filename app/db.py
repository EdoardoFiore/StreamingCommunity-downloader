"""SQLite store for panel users, sessions, requests and notifications.

Deliberately stdlib-only: the panel is self-hosted by non-experts, so adding an
ORM or a separate database service would be a maintenance cost with no payoff at
this scale. What SQLite does buy us over the existing JSON files is transactions,
UNIQUE constraints (request deduplication) and atomic compare-and-set (state
transitions, work claiming).

Schema versioning uses ``PRAGMA user_version`` and the ordered ``MIGRATIONS``
list below — every migration runs exactly once, in order, inside a transaction.
"""

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from app.config import DB_FILE

logger = logging.getLogger(__name__)

_local = threading.local()
_path: Path = DB_FILE
_path_lock = threading.Lock()


# ── Connection handling ────────────────────────────────────────────────────────

def configure(path: Path):
    """Point the store at a different file. Used by tests; call before any query."""
    global _path
    with _path_lock:
        _path = Path(path)
    close_all()


def current_path() -> Path:
    return _path


def _new_connection() -> sqlite3.Connection:
    _path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_conn() -> sqlite3.Connection:
    """Return this thread's connection, opening it on first use.

    Downloads run in a 64-thread pool, so connections are per-thread rather than
    global; SQLite connections are not safe to share across threads.
    """
    conn = getattr(_local, "conn", None)
    key = getattr(_local, "path", None)
    if conn is None or key != str(_path):
        if conn is not None:
            conn.close()
        conn = _new_connection()
        _local.conn = conn
        _local.path = str(_path)
    return conn


def close_all():
    """Close this thread's connection. Other threads reopen lazily on next use."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.path = None


@contextmanager
def tx():
    """Run a block inside a single write transaction.

    ``BEGIN IMMEDIATE`` takes the write lock up front so that read-then-write
    sequences (bootstrap counting users before inserting one, deduplicating a
    request before creating it) cannot interleave with another writer.
    """
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return get_conn().execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    return get_conn().execute(sql, params)


# ── Migrations ─────────────────────────────────────────────────────────────────

# Each migration is a list of statements. They are run one by one rather than
# through executescript(), which commits any open transaction implicitly and
# would take the user_version bump out of the migration's transaction.
_V1_AUTH = [
    """
    CREATE TABLE jf_setting (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # jellyfin_user_id is the identity key, not the username: Jellyfin usernames
    # can be renamed, ids cannot.
    """
    CREATE TABLE jf_user (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        jellyfin_user_id  TEXT    NOT NULL UNIQUE,
        username          TEXT    NOT NULL,
        is_jellyfin_admin INTEGER NOT NULL DEFAULT 0,
        permissions       INTEGER NOT NULL DEFAULT 0,
        enabled           INTEGER NOT NULL DEFAULT 1,
        device_id         TEXT    NOT NULL,
        created_at        TEXT    NOT NULL,
        last_login_at     TEXT
    )
    """,
    # Only the SHA-256 of the session token is stored, so a database leak does
    # not hand out usable sessions. Rows are deleted on logout and when a user is
    # disabled, which is what makes revocation immediate.
    """
    CREATE TABLE jf_session (
        token_hash   TEXT PRIMARY KEY,
        user_id      INTEGER NOT NULL REFERENCES jf_user(id) ON DELETE CASCADE,
        csrf_token   TEXT    NOT NULL,
        created_at   TEXT    NOT NULL,
        expires_at   TEXT    NOT NULL,
        last_seen_at TEXT    NOT NULL
    )
    """,
    "CREATE INDEX jf_session_user ON jf_session(user_id)",
]


MIGRATIONS: list[list[str]] = [
    _V1_AUTH,
]


def run_migrations():
    conn = get_conn()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= len(MIGRATIONS):
        return
    for index in range(version, len(MIGRATIONS)):
        logger.info("Applying database migration %d", index + 1)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in MIGRATIONS[index]:
                conn.execute(statement)
            # PRAGMA cannot be parameterised.
            conn.execute(f"PRAGMA user_version = {index + 1}")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    logger.info("Database ready at %s (schema v%d)", _path, len(MIGRATIONS))
