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


_V2_REQUESTS = [
    """
    CREATE TABLE jf_request (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        content_key        TEXT    NOT NULL,
        source             TEXT    NOT NULL,   -- streamingcommunity | animeunity
        media_type         TEXT    NOT NULL,   -- film | episode | anime
        external_id        TEXT    NOT NULL,
        slug               TEXT,
        title              TEXT    NOT NULL,
        year               TEXT,
        poster             TEXT,
        season             INTEGER,
        episode_number     TEXT,
        anime_type         TEXT,
        audio_languages    TEXT    NOT NULL,   -- JSON array, sorted
        subtitle_languages TEXT    NOT NULL,   -- JSON array, sorted
        available_snapshot TEXT,               -- tracks on offer when it was asked for
        status             TEXT    NOT NULL,
        problem            TEXT,               -- why it needs attention
        denial_reason      TEXT,
        requested_by       INTEGER NOT NULL REFERENCES jf_user(id),
        decided_by         INTEGER REFERENCES jf_user(id),
        decided_at         TEXT,
        job_id             TEXT,
        output_path        TEXT,
        created_at         TEXT    NOT NULL,
        updated_at         TEXT    NOT NULL
    )
    """,
    # One download per distinct piece of content, where "distinct" includes the
    # chosen tracks: the same film in Italian and in English are two requests.
    # Partial index, so a closed request never blocks asking for it again.
    """
    CREATE UNIQUE INDEX jf_request_open_key ON jf_request(content_key)
        WHERE status IN ('pending', 'approved', 'downloading', 'needs_attention')
    """,
    "CREATE INDEX jf_request_status ON jf_request(status)",
    "CREATE INDEX jf_request_job ON jf_request(job_id)",
    # Everyone who asked for this content, so a deduplicated request still
    # notifies each of them.
    """
    CREATE TABLE jf_request_subscriber (
        request_id INTEGER NOT NULL REFERENCES jf_request(id) ON DELETE CASCADE,
        user_id    INTEGER NOT NULL REFERENCES jf_user(id) ON DELETE CASCADE,
        created_at TEXT    NOT NULL,
        PRIMARY KEY (request_id, user_id)
    )
    """,
    """
    CREATE TABLE jf_notification (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL REFERENCES jf_user(id) ON DELETE CASCADE,
        request_id INTEGER REFERENCES jf_request(id) ON DELETE CASCADE,
        event      TEXT    NOT NULL,
        message    TEXT    NOT NULL,
        read_at    TEXT,
        created_at TEXT    NOT NULL
    )
    """,
    "CREATE INDEX jf_notification_user ON jf_notification(user_id, read_at)",
]


_V3_NOTIFICATION_CHANNELS = [
    # One row per Apprise target. The URL carries the secret (bot token, webhook
    # id), so it lives here rather than in data.json alongside the source domain.
    """
    CREATE TABLE jf_notification_channel (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT    NOT NULL,
        apprise_url TEXT    NOT NULL,
        events      TEXT    NOT NULL DEFAULT '[]',  -- JSON array; [] means every event
        enabled     INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT    NOT NULL,
        updated_at  TEXT    NOT NULL
    )
    """,
    "CREATE INDEX jf_notification_channel_enabled ON jf_notification_channel(enabled)",
]


_V4_SERIES_WATCH = [
    # A followed series. Films are excluded by construction: there is no "next
    # episode" to wait for.
    """
    CREATE TABLE jf_series_watch (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        source             TEXT    NOT NULL,   -- streamingcommunity | animeunity
        media_type         TEXT    NOT NULL,   -- tv | anime
        external_id        TEXT    NOT NULL,
        slug               TEXT,
        title              TEXT    NOT NULL,
        year               TEXT,
        poster             TEXT,
        anime_type         TEXT,
        audio_languages    TEXT    NOT NULL,   -- JSON array, sorted
        subtitle_languages TEXT    NOT NULL,   -- JSON array, sorted
        -- Whose DOWNLOAD permission decides whether new episodes skip the queue.
        created_by         INTEGER NOT NULL REFERENCES jf_user(id),
        auto_approve       INTEGER NOT NULL DEFAULT 0,
        enabled            INTEGER NOT NULL DEFAULT 1,
        last_checked_at    TEXT,
        created_at         TEXT    NOT NULL,
        updated_at         TEXT    NOT NULL
    )
    """,
    # One active watch per series whoever asked for it: a second interested user
    # subscribes instead. Partial, so an unfollowed series can be followed again.
    """
    CREATE UNIQUE INDEX jf_series_watch_open ON jf_series_watch(source, media_type, external_id)
        WHERE enabled = 1
    """,
    "CREATE INDEX jf_series_watch_enabled ON jf_series_watch(enabled)",
    """
    CREATE TABLE jf_series_watch_subscriber (
        watch_id   INTEGER NOT NULL REFERENCES jf_series_watch(id) ON DELETE CASCADE,
        user_id    INTEGER NOT NULL REFERENCES jf_user(id) ON DELETE CASCADE,
        created_at TEXT    NOT NULL,
        PRIMARY KEY (watch_id, user_id)
    )
    """,
    # One row per episode already accounted for. A "highest episode seen" counter
    # would be wrong here: both sources publish out of order (specials, OVAs
    # numbered 0, dubs landing after later subs), and anything below the
    # watermark would be lost for good.
    """
    CREATE TABLE jf_watch_seen_episode (
        watch_id    INTEGER NOT NULL REFERENCES jf_series_watch(id) ON DELETE CASCADE,
        episode_key TEXT    NOT NULL,   -- S01E03 (tv) | E12 (anime)
        created_at  TEXT    NOT NULL,
        PRIMARY KEY (watch_id, episode_key)
    )
    """,
    # Links a request back to the watch that spawned it: drives the "approve once,
    # automatic from then on" flow and tells the queue where the request came from.
    "ALTER TABLE jf_request ADD COLUMN watch_id INTEGER REFERENCES jf_series_watch(id) ON DELETE SET NULL",
]


# A watch used to require an account to own it, which made the whole feature
# unavailable in open mode. It is now ownerless there: `created_by` is nullable
# and NULL means "the panel itself".
#
# SQLite cannot relax NOT NULL in place, so the table is rebuilt — and the two
# child tables with it, in this order. Dropping the old parent while children
# still pointed at it would cascade their rows away; by the time it is dropped,
# the copies already reference the new one and the originals are gone.
_V5_WATCHES_WITHOUT_ACCOUNTS = [
    """
    CREATE TABLE jf_series_watch_v5 (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        source             TEXT    NOT NULL,
        media_type         TEXT    NOT NULL,
        external_id        TEXT    NOT NULL,
        slug               TEXT,
        title              TEXT    NOT NULL,
        year               TEXT,
        poster             TEXT,
        anime_type         TEXT,
        audio_languages    TEXT    NOT NULL,
        subtitle_languages TEXT    NOT NULL,
        -- NULL when the panel runs without accounts. Otherwise the owner, whose
        -- DOWNLOAD permission decides if new episodes skip the queue.
        created_by         INTEGER REFERENCES jf_user(id),
        auto_approve       INTEGER NOT NULL DEFAULT 0,
        enabled            INTEGER NOT NULL DEFAULT 1,
        last_checked_at    TEXT,
        created_at         TEXT    NOT NULL,
        updated_at         TEXT    NOT NULL
    )
    """,
    """
    INSERT INTO jf_series_watch_v5
        SELECT id, source, media_type, external_id, slug, title, year, poster, anime_type,
               audio_languages, subtitle_languages, created_by, auto_approve, enabled,
               last_checked_at, created_at, updated_at
        FROM jf_series_watch
    """,
    """
    CREATE TABLE jf_series_watch_subscriber_v5 (
        watch_id   INTEGER NOT NULL REFERENCES jf_series_watch_v5(id) ON DELETE CASCADE,
        user_id    INTEGER NOT NULL REFERENCES jf_user(id) ON DELETE CASCADE,
        created_at TEXT    NOT NULL,
        PRIMARY KEY (watch_id, user_id)
    )
    """,
    "INSERT INTO jf_series_watch_subscriber_v5 SELECT * FROM jf_series_watch_subscriber",
    """
    CREATE TABLE jf_watch_seen_episode_v5 (
        watch_id    INTEGER NOT NULL REFERENCES jf_series_watch_v5(id) ON DELETE CASCADE,
        episode_key TEXT    NOT NULL,
        created_at  TEXT    NOT NULL,
        PRIMARY KEY (watch_id, episode_key)
    )
    """,
    "INSERT INTO jf_watch_seen_episode_v5 SELECT * FROM jf_watch_seen_episode",
    "DROP TABLE jf_series_watch_subscriber",
    "DROP TABLE jf_watch_seen_episode",
    "DROP TABLE jf_series_watch",
    "ALTER TABLE jf_series_watch_v5 RENAME TO jf_series_watch",
    "ALTER TABLE jf_series_watch_subscriber_v5 RENAME TO jf_series_watch_subscriber",
    "ALTER TABLE jf_watch_seen_episode_v5 RENAME TO jf_watch_seen_episode",
    """
    CREATE UNIQUE INDEX jf_series_watch_open ON jf_series_watch(source, media_type, external_id)
        WHERE enabled = 1
    """,
    "CREATE INDEX jf_series_watch_enabled ON jf_series_watch(enabled)",
]


# The bell was per-user only, so a panel with no accounts had no bell at all —
# and a download nobody requested had nowhere to be reported. A NULL user_id
# means the notification belongs to the panel, and is what the implicit user
# sees when there are no accounts.
#
# Rebuilt rather than altered: SQLite cannot relax NOT NULL in place. Nothing
# references jf_notification, so no child tables are involved this time.
_V6_PANEL_NOTIFICATIONS = [
    """
    CREATE TABLE jf_notification_v6 (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER REFERENCES jf_user(id) ON DELETE CASCADE,
        request_id INTEGER REFERENCES jf_request(id) ON DELETE CASCADE,
        event      TEXT    NOT NULL,
        message    TEXT    NOT NULL,
        read_at    TEXT,
        created_at TEXT    NOT NULL
    )
    """,
    "INSERT INTO jf_notification_v6 SELECT * FROM jf_notification",
    "DROP TABLE jf_notification",
    "ALTER TABLE jf_notification_v6 RENAME TO jf_notification",
    "CREATE INDEX jf_notification_user ON jf_notification(user_id, read_at)",
]


MIGRATIONS: list[list[str]] = [
    _V1_AUTH,
    _V2_REQUESTS,
    _V3_NOTIFICATION_CHANNELS,
    _V4_SERIES_WATCH,
    _V5_WATCHES_WITHOUT_ACCOUNTS,
    _V6_PANEL_NOTIFICATIONS,
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
