"""External notification delivery through Apprise.

One library covers Discord, Telegram, ntfy, Pushover, Slack, email and the rest:
a channel is just a URL (``discord://…``, ``tgram://…``), so adding a service
never means adding code here.
"""

import json
import logging

from app import db
from app.requests import models

logger = logging.getLogger(__name__)

TEST_TITLE = "Pannello StreamingCommunity"


# ── CRUD ───────────────────────────────────────────────────────────────────────

def _row_to_channel(row) -> dict:
    channel = dict(row)
    channel["enabled"] = bool(channel["enabled"])
    channel["events"] = json.loads(channel["events"])
    return channel


def list_channels() -> list[dict]:
    rows = db.query("SELECT * FROM jf_notification_channel ORDER BY id")
    return [_row_to_channel(r) for r in rows]


def get_channel(channel_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM jf_notification_channel WHERE id = ?", (channel_id,))
    return _row_to_channel(row) if row else None


def create_channel(*, name: str, apprise_url: str, events: list[str], enabled: bool = True) -> dict:
    timestamp = models.now_iso()
    cursor = db.execute(
        "INSERT INTO jf_notification_channel(name, apprise_url, events, enabled, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        (name, apprise_url, json.dumps(events), int(enabled), timestamp, timestamp),
    )
    return get_channel(cursor.lastrowid)


def update_channel(channel_id: int, **fields) -> dict | None:
    columns, values = [], []
    for key in ("name", "apprise_url", "events", "enabled"):
        if key not in fields or fields[key] is None:
            continue
        value = fields[key]
        if key == "events":
            value = json.dumps(value)
        elif key == "enabled":
            value = int(value)
        columns.append(f"{key} = ?")
        values.append(value)
    if not columns:
        return get_channel(channel_id)
    columns.append("updated_at = ?")
    values.extend([models.now_iso(), channel_id])
    db.execute(
        f"UPDATE jf_notification_channel SET {', '.join(columns)} WHERE id = ?",
        tuple(values),
    )
    return get_channel(channel_id)


def delete_channel(channel_id: int):
    db.execute("DELETE FROM jf_notification_channel WHERE id = ?", (channel_id,))


def list_enabled_for_event(event: str) -> list[dict]:
    """Enabled channels subscribed to this event. An empty filter means all events.

    The filter is applied in Python rather than SQL: a self-hosted panel has a
    handful of channels, and matching inside a JSON column would tie us to the
    SQLite JSON1 extension being compiled in.
    """
    rows = db.query("SELECT * FROM jf_notification_channel WHERE enabled = 1 ORDER BY id")
    channels = [_row_to_channel(r) for r in rows]
    return [c for c in channels if not c["events"] or event in c["events"]]


# ── Delivery ───────────────────────────────────────────────────────────────────

def send(apprise_url: str, message: str, title: str = TEST_TITLE) -> bool:
    """Push one message to one target. Returns whether Apprise accepted it."""
    import apprise

    client = apprise.Apprise()
    if not client.add(apprise_url):
        return False
    return bool(client.notify(body=message, title=title))


def url_is_valid(apprise_url: str) -> bool:
    import apprise

    return bool(apprise.Apprise().add(apprise_url))


class AppriseChannel:
    """Fans an event out to every enabled external target.

    ``deliver`` blocks on network I/O on purpose: every caller of ``notify()``
    already runs off the event loop — sync routes in Starlette's threadpool,
    ``create_request`` behind ``asyncio.to_thread``, ``approve`` in the request
    pool, ``on_job_finished`` in the job executor. Wrapping it again would need a
    loop reference this module has no business holding.
    """

    name = "apprise"

    def deliver(self, event: str, user_ids: list[int], message: str, request_id: int | None):
        if not user_ids:
            return
        for channel in list_enabled_for_event(event):
            try:
                if not send(channel["apprise_url"], message):
                    logger.warning("Apprise channel %r rejected event %s", channel["name"], event)
            except Exception:
                # One dead webhook must not stop the others, and never the download.
                logger.exception("Apprise channel %r failed for event %s", channel["name"], event)
