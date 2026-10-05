"""Followed series records: who follows what, and which episodes are accounted for."""

import json
import logging
import sqlite3
from dataclasses import dataclass

from app import db
from app.requests.models import now_iso

logger = logging.getLogger(__name__)

TV = "tv"
ANIME = "anime"


@dataclass(frozen=True)
class Watch:
    id: int
    source: str
    media_type: str
    external_id: str
    slug: str | None
    title: str
    year: str | None
    poster: str | None
    anime_type: str | None
    audio_languages: list[str]
    subtitle_languages: list[str]
    created_by: int
    auto_approve: bool
    enabled: bool
    last_checked_at: str | None
    created_at: str
    updated_at: str

    def to_public(self, owner: str = None, followers: list[str] = None) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "media_type": self.media_type,
            "external_id": self.external_id,
            "slug": self.slug,
            "title": self.title,
            "year": self.year,
            "poster": self.poster,
            "anime_type": self.anime_type,
            "audio_languages": self.audio_languages,
            "subtitle_languages": self.subtitle_languages,
            "created_by": self.created_by,
            "created_by_username": owner,
            "followers": followers or [],
            "auto_approve": self.auto_approve,
            "enabled": self.enabled,
            "last_checked_at": self.last_checked_at,
            "created_at": self.created_at,
        }


def _row_to_watch(row: sqlite3.Row) -> Watch:
    return Watch(
        id=row["id"],
        source=row["source"],
        media_type=row["media_type"],
        external_id=row["external_id"],
        slug=row["slug"],
        title=row["title"],
        year=row["year"],
        poster=row["poster"],
        anime_type=row["anime_type"],
        audio_languages=json.loads(row["audio_languages"]),
        subtitle_languages=json.loads(row["subtitle_languages"]),
        created_by=row["created_by"],
        auto_approve=bool(row["auto_approve"]),
        enabled=bool(row["enabled"]),
        last_checked_at=row["last_checked_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Queries ────────────────────────────────────────────────────────────────────

def get(watch_id: int) -> Watch | None:
    row = db.query_one("SELECT * FROM jf_series_watch WHERE id = ?", (watch_id,))
    return _row_to_watch(row) if row else None


def find_open(source: str, media_type: str, external_id: str) -> Watch | None:
    row = db.query_one(
        "SELECT * FROM jf_series_watch WHERE source = ? AND media_type = ? AND external_id = ? "
        "AND enabled = 1",
        (source, media_type, str(external_id)),
    )
    return _row_to_watch(row) if row else None


def list_all(enabled_only: bool = True) -> list[Watch]:
    sql = "SELECT * FROM jf_series_watch"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY title COLLATE NOCASE"
    return [_row_to_watch(r) for r in db.query(sql)]


def list_for_user(user_id: int | None) -> list[Watch]:
    """Every active watch the user follows, whether or not they started it.

    Without accounts there is nobody to scope to and only one audience, so the
    panel's own watches are the list.
    """
    if user_id is None:
        return list_all()
    rows = db.query(
        "SELECT w.* FROM jf_series_watch w "
        "JOIN jf_series_watch_subscriber s ON s.watch_id = w.id "
        "WHERE s.user_id = ? AND w.enabled = 1 ORDER BY w.title COLLATE NOCASE",
        (user_id,),
    )
    return [_row_to_watch(r) for r in rows]


def followers(watch_id: int) -> list[int]:
    rows = db.query(
        "SELECT user_id FROM jf_series_watch_subscriber WHERE watch_id = ? ORDER BY created_at",
        (watch_id,),
    )
    return [r["user_id"] for r in rows]


def follower_names(watch_id: int) -> list[str]:
    rows = db.query(
        "SELECT u.username FROM jf_series_watch_subscriber s JOIN jf_user u ON u.id = s.user_id "
        "WHERE s.watch_id = ? ORDER BY s.created_at",
        (watch_id,),
    )
    return [r["username"] for r in rows]


def owner_permissions(watch_id: int) -> int | None:
    """The owner's current permission bits, or None for an ownerless watch.

    Read live on every poll rather than frozen on the watch: revoking someone's
    DOWNLOAD permission has to send their followed series back through the queue.
    """
    row = db.query_one(
        "SELECT u.permissions FROM jf_series_watch w JOIN jf_user u ON u.id = w.created_by "
        "WHERE w.id = ? AND u.enabled = 1",
        (watch_id,),
    )
    return row["permissions"] if row else None


# ── Writes ─────────────────────────────────────────────────────────────────────

def create(
    *,
    source: str,
    media_type: str,
    external_id: str,
    title: str,
    audio_languages: list[str],
    subtitle_languages: list[str],
    created_by: int | None,
    slug: str = None,
    year: str = None,
    poster: str = None,
    anime_type: str = None,
) -> tuple[Watch, bool]:
    """Follow a series, or join the existing watch for it.

    Returns ``(watch, created)``. A second user asking for the same series does
    not get a second poller: they subscribe to the one that already exists, and
    the original follower stays the owner whose permission decides auto-download.

    ``created_by`` is None when the panel runs without accounts: the watch then
    belongs to the panel, and there is no subscriber row to write.
    """
    timestamp = now_iso()
    with db.tx() as conn:
        existing = conn.execute(
            "SELECT * FROM jf_series_watch WHERE source = ? AND media_type = ? "
            "AND external_id = ? AND enabled = 1",
            (source, media_type, str(external_id)),
        ).fetchone()
        wanted = (json.dumps(sorted(set(audio_languages))),
                  json.dumps(sorted(set(subtitle_languages))))
        if existing is not None:
            if created_by is not None:
                # Following again restates what this follower wants.
                conn.execute(
                    "INSERT INTO jf_series_watch_subscriber(watch_id, user_id, created_at, "
                    "audio_languages, subtitle_languages) VALUES(?, ?, ?, ?, ?) "
                    "ON CONFLICT(watch_id, user_id) DO UPDATE SET "
                    "audio_languages = excluded.audio_languages, "
                    "subtitle_languages = excluded.subtitle_languages",
                    (existing["id"], created_by, timestamp, *wanted),
                )
            return _row_to_watch(existing), False

        cursor = conn.execute(
            "INSERT INTO jf_series_watch(source, media_type, external_id, slug, title, year, "
            "poster, anime_type, audio_languages, subtitle_languages, created_by, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                source, media_type, str(external_id), slug, title, year, poster, anime_type,
                json.dumps(sorted(audio_languages)), json.dumps(sorted(subtitle_languages)),
                created_by, timestamp, timestamp,
            ),
        )
        watch_id = cursor.lastrowid
        if created_by is not None:
            conn.execute(
                "INSERT INTO jf_series_watch_subscriber(watch_id, user_id, created_at, "
                "audio_languages, subtitle_languages) VALUES(?,?,?,?,?)",
                (watch_id, created_by, timestamp, *wanted),
            )
        row = conn.execute("SELECT * FROM jf_series_watch WHERE id = ?", (watch_id,)).fetchone()
    return _row_to_watch(row), True


def unfollow(watch_id: int, user_id: int | None) -> bool:
    """Drop one follower; disable the watch once the last one leaves.

    Kept as a soft delete so the seen-episode ledger survives: re-following later
    would otherwise re-seed from scratch, which is harmless, but the history of
    what the panel already handled is worth keeping.

    Without accounts there are no followers to drop: unfollowing is simply
    stopping the watch.
    """
    if user_id is None:
        disable(watch_id)
        return True
    with db.tx() as conn:
        conn.execute(
            "DELETE FROM jf_series_watch_subscriber WHERE watch_id = ? AND user_id = ?",
            (watch_id, user_id),
        )
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM jf_series_watch_subscriber WHERE watch_id = ?",
            (watch_id,),
        ).fetchone()["n"]
        if remaining == 0:
            conn.execute(
                "UPDATE jf_series_watch SET enabled = 0, updated_at = ? WHERE id = ?",
                (now_iso(), watch_id),
            )
            return True
        # The owner left but others still follow: hand ownership to the oldest
        # remaining follower, otherwise auto-download would key off a user who is
        # no longer involved.
        conn.execute(
            "UPDATE jf_series_watch SET created_by = ("
            "  SELECT user_id FROM jf_series_watch_subscriber WHERE watch_id = ? "
            "  ORDER BY created_at LIMIT 1"
            "), updated_at = ? WHERE id = ? AND created_by NOT IN ("
            "  SELECT user_id FROM jf_series_watch_subscriber WHERE watch_id = ?"
            ")",
            (watch_id, now_iso(), watch_id, watch_id),
        )
    return False


def disable(watch_id: int) -> None:
    """Stop a watch outright, whoever follows it. Used by approvers."""
    with db.tx() as conn:
        conn.execute("DELETE FROM jf_series_watch_subscriber WHERE watch_id = ?", (watch_id,))
        conn.execute(
            "UPDATE jf_series_watch SET enabled = 0, updated_at = ? WHERE id = ?",
            (now_iso(), watch_id),
        )


def set_auto_approve(watch_id: int, value: bool) -> Watch | None:
    db.execute(
        "UPDATE jf_series_watch SET auto_approve = ?, updated_at = ? WHERE id = ?",
        (int(value), now_iso(), watch_id),
    )
    return get(watch_id)


def set_year(watch_id: int, year: str) -> None:
    db.execute(
        "UPDATE jf_series_watch SET year = ?, updated_at = ? WHERE id = ?",
        (year, now_iso(), watch_id),
    )


def set_languages(watch_id: int, audio_languages: list[str],
                  subtitle_languages: list[str]) -> Watch | None:
    """Change what future episodes are downloaded with. Episodes already in the
    library are not touched."""
    db.execute(
        "UPDATE jf_series_watch SET audio_languages = ?, subtitle_languages = ?, "
        "updated_at = ? WHERE id = ?",
        (json.dumps(sorted(set(audio_languages))), json.dumps(sorted(set(subtitle_languages))),
         now_iso(), watch_id),
    )
    return get(watch_id)


def missing_languages(watch: Watch, user_id: int | None = None) -> dict:
    """Languages followers asked for that the watch does not download.

    ``{"audio": [...], "subtitles": [...], "by": [usernames]}``, empty lists
    when everyone is covered. Asking for fewer languages than the watch has is
    not a conflict: that follower gets more than they asked, never less.
    Limited to one follower with ``user_id``.
    """
    sql = ("SELECT u.username, s.audio_languages, s.subtitle_languages "
           "FROM jf_series_watch_subscriber s JOIN jf_user u ON u.id = s.user_id "
           "WHERE s.watch_id = ?")
    params: tuple = (watch.id,)
    if user_id is not None:
        sql += " AND s.user_id = ?"
        params += (user_id,)
    audio, subtitles, by = set(), set(), []
    for row in db.query(sql + " ORDER BY s.created_at", params):
        extra_audio = set(json.loads(row["audio_languages"] or "[]")) - set(watch.audio_languages)
        extra_subs = set(json.loads(row["subtitle_languages"] or "[]")) - set(watch.subtitle_languages)
        if extra_audio or extra_subs:
            audio |= extra_audio
            subtitles |= extra_subs
            by.append(row["username"])
    return {"audio": sorted(audio), "subtitles": sorted(subtitles), "by": by}


def merge_languages(watch_id: int) -> Watch | None:
    """Download every language any follower asked for, from the next episode on."""
    watch = get(watch_id)
    if watch is None:
        return None
    missing = missing_languages(watch)
    return set_languages(
        watch_id,
        watch.audio_languages + missing["audio"],
        watch.subtitle_languages + missing["subtitles"],
    )


def touch_checked(watch_id: int) -> None:
    db.execute(
        "UPDATE jf_series_watch SET last_checked_at = ? WHERE id = ?", (now_iso(), watch_id)
    )


# ── Seen-episode ledger ────────────────────────────────────────────────────────

def seen_keys(watch_id: int) -> set[str]:
    rows = db.query("SELECT episode_key FROM jf_watch_seen_episode WHERE watch_id = ?", (watch_id,))
    return {r["episode_key"] for r in rows}


def seed_seen(watch_id: int, episode_keys: list[str]) -> None:
    """Mark everything currently published as already handled.

    This is what makes "follow" mean "from here on": without it, following a
    finished series would queue its entire back catalogue.
    """
    if not episode_keys:
        return
    timestamp = now_iso()
    with db.tx() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO jf_watch_seen_episode(watch_id, episode_key, created_at) "
            "VALUES(?, ?, ?)",
            [(watch_id, key, timestamp) for key in episode_keys],
        )


def mark_seen(watch_id: int, episode_key: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO jf_watch_seen_episode(watch_id, episode_key, created_at) "
        "VALUES(?, ?, ?)",
        (watch_id, episode_key, now_iso()),
    )
