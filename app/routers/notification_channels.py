import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app.auth.deps import require
from app.auth.permissions import Permission
from app.requests import apprise_channel, notify

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/notification-channels", tags=["notification-channels"])

CAN_MANAGE = [Depends(require(Permission.MANAGE_SETTINGS))]


def _validate_events(events: list[str] | None) -> list[str] | None:
    """Reject unknown event names, then de-duplicate and order them.

    Nothing checked these before, so a typo became a filter that silently never
    matched — the channel would simply go quiet. An empty list keeps its
    meaning: every event.
    """
    if events is None:
        return None
    unknown = [e for e in events if e not in notify.ALL_EVENTS]
    if unknown:
        raise ValueError(f"Evento sconosciuto: {', '.join(sorted(set(unknown)))}")
    return [e for e in notify.ALL_EVENTS if e in set(events)]


class ChannelCreate(BaseModel):
    name: str
    apprise_url: str
    events: list[str] = []
    enabled: bool = True

    @field_validator("events")
    @classmethod
    def _check_events(cls, value):
        return _validate_events(value)


class ChannelUpdate(BaseModel):
    name: str | None = None
    apprise_url: str | None = None
    events: list[str] | None = None
    enabled: bool | None = None

    @field_validator("events")
    @classmethod
    def _check_events(cls, value):
        return _validate_events(value)


@router.get("", dependencies=CAN_MANAGE)
def list_channels():
    return {"channels": apprise_channel.list_channels()}


@router.post("", dependencies=CAN_MANAGE)
async def create_channel(body: ChannelCreate):
    name = body.name.strip()
    url = body.apprise_url.strip()
    if not name or not url:
        raise HTTPException(status_code=400, detail="Nome e URL sono obbligatori")
    if not await asyncio.to_thread(apprise_channel.url_is_valid, url):
        raise HTTPException(status_code=400, detail="URL Apprise non riconosciuta")
    return apprise_channel.create_channel(
        name=name, apprise_url=url, events=body.events, enabled=body.enabled
    )


@router.patch("/{channel_id}", dependencies=CAN_MANAGE)
async def update_channel(channel_id: int, body: ChannelUpdate):
    if apprise_channel.get_channel(channel_id) is None:
        raise HTTPException(status_code=404, detail="Canale non trovato")
    url = body.apprise_url.strip() if body.apprise_url is not None else None
    if url and not await asyncio.to_thread(apprise_channel.url_is_valid, url):
        raise HTTPException(status_code=400, detail="URL Apprise non riconosciuta")
    return apprise_channel.update_channel(
        channel_id,
        name=body.name.strip() if body.name is not None else None,
        apprise_url=url,
        events=body.events,
        enabled=body.enabled,
    )


@router.delete("/{channel_id}", dependencies=CAN_MANAGE)
def delete_channel(channel_id: int):
    apprise_channel.delete_channel(channel_id)
    return {"ok": True}


@router.post("/{channel_id}/test", dependencies=CAN_MANAGE)
async def test_channel(channel_id: int):
    channel = apprise_channel.get_channel(channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Canale non trovato")
    try:
        ok = await asyncio.to_thread(
            apprise_channel.send,
            channel["apprise_url"],
            "Notifica di test dal pannello.",
        )
    except Exception:
        logger.exception("Test notification failed for channel %s", channel_id)
        ok = False
    return {"ok": ok}
