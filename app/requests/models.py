"""Request records: identity, deduplication and state transitions."""

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from app import db

logger = logging.getLogger(__name__)


# ── States ─────────────────────────────────────────────────────────────────────

PENDING = "pending"
APPROVED = "approved"
DOWNLOADING = "downloading"
COMPLETED = "completed"
DENIED = "denied"
FAILED = "failed"
CANCELLED = "cancelled"
NEEDS_ATTENTION = "needs_attention"
AVAILABLE = "available"

# While a request is in one of these it holds the content key, so nobody can
# queue the same thing twice.
OPEN_STATUSES = (PENDING, APPROVED, DOWNLOADING, NEEDS_ATTENTION)

TERMINAL_STATUSES = (COMPLETED, DENIED, FAILED, CANCELLED, AVAILABLE)

ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    # available is reached straight from pending when the library check at
    # creation time finds the file already there.
    PENDING: (APPROVED, DENIED, NEEDS_ATTENTION, CANCELLED, AVAILABLE),
    # approved is a short-lived claim state: resolution runs, then the request
    # either starts downloading or comes back for a human.
    APPROVED: (DOWNLOADING, NEEDS_ATTENTION, FAILED, COMPLETED),
    DOWNLOADING: (COMPLETED, FAILED, CANCELLED),
    NEEDS_ATTENTION: (APPROVED, DENIED, CANCELLED),
    COMPLETED: (),
    DENIED: (),
    FAILED: (),
    CANCELLED: (),
    AVAILABLE: (),
}


class InvalidTransition(Exception):
    def __init__(self, current: str, target: str):
        self.current = current
        self.target = target
        super().__init__(f"Transizione non ammessa: {current} → {target}")


def sources_for(target: str) -> tuple[str, ...]:
    """States from which ``target`` may be entered."""
    return tuple(src for src, allowed in ALLOWED_TRANSITIONS.items() if target in allowed)


# ── Identity ───────────────────────────────────────────────────────────────────

def content_key(
    source: str,
    media_type: str,
    external_id: str,
    season,
    episode_number,
    audio_languages: list[str],
    subtitle_languages: list[str],
) -> str:
    """Identity of a piece of content *as requested*.

    The chosen tracks are part of it on purpose: two users asking for the same
    film with different audio are making two different requests, and merging
    them would give one of them the wrong file.
    """
    parts = [
        source,
        media_type,
        str(external_id),
        "" if season is None else str(season),
        "" if episode_number is None else str(episode_number),
        ",".join(sorted(audio_languages)),
        ",".join(sorted(subtitle_languages)),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Record ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Request:
    id: int
    content_key: str
    source: str
    media_type: str
    external_id: str
    slug: str | None
    title: str
    year: str | None
    poster: str | None
    season: int | None
    episode_number: str | None
    anime_type: str | None
    audio_languages: list[str]
    subtitle_languages: list[str]
    available_snapshot: dict | None
    status: str
    problem: str | None
    denial_reason: str | None
    requested_by: int
    decided_by: int | None
    decided_at: str | None
    job_id: str | None
    output_path: str | None
    created_at: str
    updated_at: str
    # Set when a followed series produced this request; None for manual ones.
    watch_id: int | None = None

    def to_public(self, requester: str = None, decider: str = None,
                  subscribers: list[str] = None) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "media_type": self.media_type,
            "external_id": self.external_id,
            "slug": self.slug,
            "title": self.title,
            "year": self.year,
            "poster": self.poster,
            "season": self.season,
            "episode_number": self.episode_number,
            "anime_type": self.anime_type,
            "audio_languages": self.audio_languages,
            "subtitle_languages": self.subtitle_languages,
            "available_snapshot": self.available_snapshot,
            "status": self.status,
            "problem": self.problem,
            "denial_reason": self.denial_reason,
            "requested_by": self.requested_by,
            "requested_by_username": requester,
            "decided_by_username": decider,
            "decided_at": self.decided_at,
            "subscribers": subscribers or [],
            "job_id": self.job_id,
            "output_path": self.output_path,
            "watch_id": self.watch_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _row_to_request(row: sqlite3.Row) -> Request:
    return Request(
        id=row["id"],
        content_key=row["content_key"],
        source=row["source"],
        media_type=row["media_type"],
        external_id=row["external_id"],
        slug=row["slug"],
        title=row["title"],
        year=row["year"],
        poster=row["poster"],
        season=row["season"],
        episode_number=row["episode_number"],
        anime_type=row["anime_type"],
        audio_languages=json.loads(row["audio_languages"]),
        subtitle_languages=json.loads(row["subtitle_languages"]),
        available_snapshot=json.loads(row["available_snapshot"]) if row["available_snapshot"] else None,
        status=row["status"],
        problem=row["problem"],
        denial_reason=row["denial_reason"],
        requested_by=row["requested_by"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        job_id=row["job_id"],
        output_path=row["output_path"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        watch_id=row["watch_id"],
    )


# ── Queries ────────────────────────────────────────────────────────────────────

def get(request_id: int) -> Request | None:
    row = db.query_one("SELECT * FROM jf_request WHERE id = ?", (request_id,))
    return _row_to_request(row) if row else None


def get_by_job_id(job_id: str) -> Request | None:
    row = db.query_one("SELECT * FROM jf_request WHERE job_id = ?", (job_id,))
    return _row_to_request(row) if row else None


def count_by_status(*statuses: str) -> int:
    """Cheap count for badges — no need to materialise full rows."""
    placeholders = ",".join("?" * len(statuses))
    row = db.query_one(
        f"SELECT COUNT(*) AS n FROM jf_request WHERE status IN ({placeholders})", statuses
    )
    return row["n"]


def find_open_by_key(key: str) -> Request | None:
    placeholders = ",".join("?" * len(OPEN_STATUSES))
    row = db.query_one(
        f"SELECT * FROM jf_request WHERE content_key = ? AND status IN ({placeholders})",
        (key, *OPEN_STATUSES),
    )
    return _row_to_request(row) if row else None


def list_all(statuses: tuple[str, ...] | None = None) -> list[Request]:
    if statuses:
        placeholders = ",".join("?" * len(statuses))
        rows = db.query(
            f"SELECT * FROM jf_request WHERE status IN ({placeholders}) ORDER BY created_at DESC",
            statuses,
        )
    else:
        rows = db.query("SELECT * FROM jf_request ORDER BY created_at DESC")
    return [_row_to_request(r) for r in rows]


def list_for_user(user_id: int) -> list[Request]:
    """Every request the user asked for, including ones they joined."""
    rows = db.query(
        "SELECT r.* FROM jf_request r "
        "JOIN jf_request_subscriber s ON s.request_id = r.id "
        "WHERE s.user_id = ? ORDER BY r.created_at DESC",
        (user_id,),
    )
    return [_row_to_request(r) for r in rows]


def subscribers(request_id: int) -> list[int]:
    rows = db.query(
        "SELECT user_id FROM jf_request_subscriber WHERE request_id = ? ORDER BY created_at",
        (request_id,),
    )
    return [r["user_id"] for r in rows]


def subscriber_names(request_id: int) -> list[str]:
    rows = db.query(
        "SELECT u.username FROM jf_request_subscriber s JOIN jf_user u ON u.id = s.user_id "
        "WHERE s.request_id = ? ORDER BY s.created_at",
        (request_id,),
    )
    return [r["username"] for r in rows]


def username(user_id: int | None) -> str | None:
    if user_id is None:
        return None
    row = db.query_one("SELECT username FROM jf_user WHERE id = ?", (user_id,))
    return row["username"] if row else None


# ── Writes ─────────────────────────────────────────────────────────────────────

def create(
    *,
    source: str,
    media_type: str,
    external_id: str,
    title: str,
    audio_languages: list[str],
    subtitle_languages: list[str],
    requested_by: int,
    slug: str = None,
    year: str = None,
    poster: str = None,
    season: int = None,
    episode_number: str = None,
    anime_type: str = None,
    available_snapshot: dict = None,
    status: str = PENDING,
    watch_id: int = None,
) -> tuple[Request, bool]:
    """Create a request, or join the open one with the same content key.

    Returns ``(request, created)``. The unique partial index is what actually
    decides — checking first and inserting after would race with a concurrent
    identical request.
    """
    audio = sorted(audio_languages)
    subtitles = sorted(subtitle_languages)
    key = content_key(source, media_type, external_id, season, episode_number, audio, subtitles)
    timestamp = now_iso()

    with db.tx() as conn:
        existing = conn.execute(
            "SELECT * FROM jf_request WHERE content_key = ? AND status IN "
            f"({','.join('?' * len(OPEN_STATUSES))})",
            (key, *OPEN_STATUSES),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "INSERT OR IGNORE INTO jf_request_subscriber(request_id, user_id, created_at) "
                "VALUES(?, ?, ?)",
                (existing["id"], requested_by, timestamp),
            )
            return _row_to_request(existing), False

        cursor = conn.execute(
            "INSERT INTO jf_request(content_key, source, media_type, external_id, slug, title, "
            "year, poster, season, episode_number, anime_type, audio_languages, "
            "subtitle_languages, available_snapshot, status, requested_by, watch_id, "
            "created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                key, source, media_type, str(external_id), slug, title, year, poster,
                season, episode_number, anime_type,
                json.dumps(audio), json.dumps(subtitles),
                json.dumps(available_snapshot) if available_snapshot else None,
                status, requested_by, watch_id, timestamp, timestamp,
            ),
        )
        request_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO jf_request_subscriber(request_id, user_id, created_at) VALUES(?, ?, ?)",
            (request_id, requested_by, timestamp),
        )
        row = conn.execute("SELECT * FROM jf_request WHERE id = ?", (request_id,)).fetchone()
    return _row_to_request(row), True


def transition(request_id: int, target: str, **fields) -> Request | None:
    """Move a request to ``target`` if the transition is allowed, atomically.

    The status guard lives in the UPDATE's WHERE clause, so two approvers
    clicking at the same moment produce one winner and one None — this is also
    what stops a second worker process from starting the same download.
    """
    if target not in ALLOWED_TRANSITIONS:
        raise ValueError(f"Stato sconosciuto: {target!r}")
    sources = sources_for(target)
    if not sources:
        # Nothing may enter this state — pending is only ever set at creation.
        return None

    columns = ["status = ?", "updated_at = ?"]
    values: list = [target, now_iso()]
    for column, value in fields.items():
        columns.append(f"{column} = ?")
        values.append(value)

    placeholders = ",".join("?" * len(sources))
    cursor = db.execute(
        f"UPDATE jf_request SET {', '.join(columns)} "
        f"WHERE id = ? AND status IN ({placeholders})",
        (*values, request_id, *sources),
    )
    if cursor.rowcount == 0:
        return None
    return get(request_id)


def require_transition(request_id: int, target: str) -> Request:
    """Like ``transition`` but raises with the current state when refused."""
    current = get(request_id)
    if current is None:
        raise LookupError("Richiesta non trovata")
    updated = transition(request_id, target)
    if updated is None:
        raise InvalidTransition(current.status, target)
    return updated


def update_fields(request_id: int, **fields) -> Request | None:
    if not fields:
        return get(request_id)
    columns = ", ".join(f"{k} = ?" for k in fields)
    db.execute(
        f"UPDATE jf_request SET {columns}, updated_at = ? WHERE id = ?",
        (*fields.values(), now_iso(), request_id),
    )
    return get(request_id)


def rekey(request_id: int, request: Request) -> str:
    """Recompute and store the content key after an approver edited a request."""
    key = content_key(
        request.source, request.media_type, request.external_id,
        request.season, request.episode_number,
        request.audio_languages, request.subtitle_languages,
    )
    db.execute("UPDATE jf_request SET content_key = ? WHERE id = ?", (key, request_id))
    return key
