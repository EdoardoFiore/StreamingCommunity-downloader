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


# ── Presentation ───────────────────────────────────────────────────────────────

_asset_cache = None


def _asset():
    """Branding shared by every delivery.

    Apprise otherwise signs messages as itself: its own name on the embed author
    line, its own footer, and its megaphone as the Discord avatar. Blanking the
    image masks is what removes the avatar — Discord only receives ``avatar_url``
    when an image resolves, so with none it falls back to whatever avatar the
    webhook itself is configured with.
    """
    global _asset_cache
    if _asset_cache is None:
        import apprise

        _asset_cache = apprise.AppriseAsset(
            app_id="StreamingCommunity",
            app_desc="Pannello StreamingCommunity",
            app_url="https://github.com/EdoardoFiore/StreamingCommunity-downloader",
            image_url_mask=None,
            image_url_logo=None,
        )
    return _asset_cache


# Discord only builds a rich embed — title, coloured stripe, formatted body —
# when the URL asks for markdown; left alone it posts a flat line of text.
_DISCORD_SCHEMES = ("discord", "discords")
_DISCORD_HOSTS = ("discord.com", "discordapp.com", "www.discord.com")


def _decorated_url(apprise_url: str) -> str:
    """Add our Discord defaults to a URL without touching the stored value.

    The settings page keeps showing exactly what the user typed; only the copy
    handed to Apprise carries the extra parameters. Anything the user set
    explicitly is left alone — they made a choice.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        parts = urlsplit(apprise_url)
    except ValueError:
        return apprise_url

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    is_discord = scheme in _DISCORD_SCHEMES or (
        scheme in ("http", "https") and host in _DISCORD_HOSTS
    )
    if not is_discord:
        # Telegram already defaults to HTML and would need mdv=v2 for correct
        # escaping; ntfy and email have no embeds. Nothing to gain, plenty to
        # break.
        return apprise_url

    query = parse_qsl(parts.query, keep_blank_values=True)
    keys = {key.lower() for key, _ in query}
    if "format" not in keys:
        query.append(("format", "markdown"))
    if "fields" not in keys:
        # Without this Discord splits markdown headings into embed fields and
        # wraps each in a code block.
        query.append(("fields", "no"))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _notify_type(name: str | None):
    """Map our own outcome vocabulary onto Apprise's, which drives embed colour."""
    import apprise

    return {
        "success": apprise.NotifyType.SUCCESS,
        "failure": apprise.NotifyType.FAILURE,
        "warning": apprise.NotifyType.WARNING,
    }.get(name, apprise.NotifyType.INFO)


# ── Delivery ───────────────────────────────────────────────────────────────────

def send(
    apprise_url: str,
    message: str,
    title: str = TEST_TITLE,
    notify_type: str | None = None,
) -> bool:
    """Push one message to one target. Returns whether Apprise accepted it."""
    import apprise

    client = apprise.Apprise(asset=_asset())
    if not client.add(_decorated_url(apprise_url)):
        return False
    return bool(client.notify(
        body=message,
        title=title,
        notify_type=_notify_type(notify_type),
        body_format=apprise.NotifyFormat.MARKDOWN,
    ))


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

    def deliver(self, event: str, user_ids: list[int], message: str, request_id: int | None,
                title: str | None = None, notify_type: str | None = None):
        # Deliberately no early return on an empty recipient list: external
        # channels are configured per panel, not per user, and a direct download
        # in open mode has no account to notify in-app while still being exactly
        # the thing the Discord webhook exists to report.
        for channel in list_enabled_for_event(event):
            try:
                if not send(channel["apprise_url"], message,
                            title=title or TEST_TITLE, notify_type=notify_type):
                    logger.warning("Apprise channel %r rejected event %s", channel["name"], event)
            except Exception:
                # One dead webhook must not stop the others, and never the download.
                logger.exception("Apprise channel %r failed for event %s", channel["name"], event)
