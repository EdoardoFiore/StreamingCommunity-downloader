"""Notification dispatch.

The point of the indirection is that adding a channel means appending to
``CHANNELS`` — business logic calls ``notify()`` and knows nothing about how a
message gets delivered.
"""

import logging

from app import db
from app.auth.permissions import Permission
from app.requests import models
from app.requests.apprise_channel import AppriseChannel

logger = logging.getLogger(__name__)

# Events, as stored on the notification row and used by the frontend for icons.
REQUEST_CREATED = "request_created"
REQUEST_JOINED = "request_joined"
REQUEST_APPROVED = "request_approved"
REQUEST_DENIED = "request_denied"
REQUEST_DOWNLOADING = "request_downloading"
REQUEST_COMPLETED = "request_completed"
REQUEST_FAILED = "request_failed"
REQUEST_NEEDS_ATTENTION = "request_needs_attention"
REQUEST_AVAILABLE = "request_available"

# Direct downloads: the path that skips the request queue entirely. A season or
# a series asked for in one go reports once, not once per episode.
DOWNLOAD_COMPLETED = "download_completed"
DOWNLOAD_FAILED = "download_failed"
DOWNLOAD_BATCH_COMPLETED = "download_batch_completed"
DOWNLOAD_BATCH_FAILED = "download_batch_failed"

# The single vocabulary: the per-channel event picker, the server-side validator
# and the bell's icon table all key off this.
ALL_EVENTS = (
    REQUEST_CREATED,
    REQUEST_JOINED,
    REQUEST_APPROVED,
    REQUEST_DENIED,
    REQUEST_DOWNLOADING,
    REQUEST_COMPLETED,
    REQUEST_FAILED,
    REQUEST_NEEDS_ATTENTION,
    REQUEST_AVAILABLE,
    DOWNLOAD_COMPLETED,
    DOWNLOAD_FAILED,
    DOWNLOAD_BATCH_COMPLETED,
    DOWNLOAD_BATCH_FAILED,
)

# Outcome vocabulary, kept as plain strings so this module never imports apprise.
# External channels turn it into the colour of the message.
SUCCESS = "success"
FAILURE = "failure"
WARNING = "warning"
INFO = "info"

EVENT_NOTIFY_TYPE = {
    REQUEST_CREATED: INFO,
    REQUEST_JOINED: INFO,
    REQUEST_APPROVED: SUCCESS,
    REQUEST_DENIED: WARNING,
    REQUEST_DOWNLOADING: INFO,
    REQUEST_COMPLETED: SUCCESS,
    REQUEST_AVAILABLE: SUCCESS,
    REQUEST_FAILED: FAILURE,
    REQUEST_NEEDS_ATTENTION: WARNING,
    DOWNLOAD_COMPLETED: SUCCESS,
    DOWNLOAD_FAILED: FAILURE,
    DOWNLOAD_BATCH_COMPLETED: SUCCESS,
    DOWNLOAD_BATCH_FAILED: FAILURE,
}


class InAppChannel:
    """Stores the notification and pushes it over the existing SSE stream."""

    name = "in_app"

    def deliver(self, event: str, user_ids: list[int], message: str, request_id: int | None,
                title: str | None = None, notify_type: str | None = None):
        # title and notify_type are presentation for external channels; the bell
        # shows the message and picks its own icon from the event.
        if not user_ids:
            return
        timestamp = models.now_iso()
        with db.tx() as conn:
            for user_id in user_ids:
                conn.execute(
                    "INSERT INTO jf_notification(user_id, request_id, event, message, created_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (user_id, request_id, event, message, timestamp),
                )

        # The SSE stream is shared by everyone connected to it, so the payload is
        # only a signal to re-fetch: the message text and the recipient list stay
        # behind GET /api/notifications, which is scoped to the caller.
        from app.jobs import job_manager
        job_manager.broadcast({"type": "notification"})


CHANNELS = [InAppChannel(), AppriseChannel()]


def notify(event: str, message: str, user_ids: list[int], request_id: int | None = None,
           *, markdown_message: str | None = None, title: str | None = None,
           notify_type: str | None = None):
    """Deliver one event to every channel.

    ``message`` is the plain text stored on the bell; ``markdown_message`` is
    what external channels render, defaulting to the same text. ``notify_type``
    is resolved from the event unless a caller overrides it — a partly-failed
    batch is neither a success nor a failure and says so explicitly.
    """
    unique = list(dict.fromkeys(user_ids))
    outcome = notify_type or EVENT_NOTIFY_TYPE.get(event, INFO)
    external = markdown_message if markdown_message is not None else message
    for channel in CHANNELS:
        try:
            body = external if channel.name != "in_app" else message
            channel.deliver(event, unique, body, request_id,
                            title=title, notify_type=outcome)
        except Exception:
            logger.exception("Notification channel %s failed for %s", channel.name, event)


def notify_subscribers(event: str, message: str, request: models.Request):
    notify(event, message, models.subscribers(request.id), request.id)


def approver_ids() -> list[int]:
    rows = db.query(
        "SELECT id FROM jf_user WHERE enabled = 1 AND (permissions & ?) != 0",
        (int(Permission.MANAGE_REQUESTS),),
    )
    return [r["id"] for r in rows]


def notify_approvers(event: str, message: str, request: models.Request):
    notify(event, message, approver_ids(), request.id)


# ── Reading ────────────────────────────────────────────────────────────────────

def list_for_user(user_id: int, limit: int = 50) -> list[dict]:
    rows = db.query(
        "SELECT id, request_id, event, message, read_at, created_at FROM jf_notification "
        "WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (user_id, limit),
    )
    return [dict(r) for r in rows]


def unread_count(user_id: int) -> int:
    return db.query_one(
        "SELECT COUNT(*) AS n FROM jf_notification WHERE user_id = ? AND read_at IS NULL",
        (user_id,),
    )["n"]


def mark_read(user_id: int, notification_ids: list[int] | None = None):
    """Mark notifications read. Always scoped to the caller's own rows."""
    timestamp = models.now_iso()
    if notification_ids:
        placeholders = ",".join("?" * len(notification_ids))
        db.execute(
            f"UPDATE jf_notification SET read_at = ? WHERE user_id = ? AND read_at IS NULL "
            f"AND id IN ({placeholders})",
            (timestamp, user_id, *notification_ids),
        )
    else:
        db.execute(
            "UPDATE jf_notification SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
            (timestamp, user_id),
        )
