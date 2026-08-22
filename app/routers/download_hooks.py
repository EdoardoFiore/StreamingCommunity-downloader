"""CRUD for post-download webhooks.

Shaped after the notification-channel router, which solves the same problem: a
list of outbound targets, each with an event filter and a test button.

The test endpoint returns a status code and nothing else. A hook may legitimately
point at a private address — reaching Jellyfin on the LAN is the entire use case
— so handing the response body back to whoever pressed the button would turn
this into a way of reading services they cannot otherwise reach.
"""

import asyncio
import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app import downloads_hooks
from app.auth.deps import require
from app.auth.permissions import Permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/download-hooks", tags=["download-hooks"])

CAN_MANAGE = [Depends(require(Permission.MANAGE_SETTINGS))]


def _validate_events(events: list[str] | None) -> list[str] | None:
    if events is None:
        return None
    unknown = [e for e in events if e not in downloads_hooks.HOOK_EVENTS]
    if unknown:
        raise ValueError(
            f"Eventi non validi: {', '.join(unknown)}. "
            f"Ammessi: {', '.join(downloads_hooks.HOOK_EVENTS)}"
        )
    # An empty list means every event, which is the same convention the
    # notification channels use. Deduplicated so the picker cannot store noise.
    return list(dict.fromkeys(events))


def _validate_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("L'URL deve iniziare con http:// o https://")
    return url.strip()


class HookCreate(BaseModel):
    name: str
    url: str
    method: str = "POST"
    headers: dict[str, str] = {}
    body_template: str = ""
    events: list[str] = []

    @field_validator("url")
    @classmethod
    def _check_url(cls, value):
        return _validate_url(value)

    @field_validator("method")
    @classmethod
    def _check_method(cls, value):
        method = (value or "POST").upper()
        if method not in downloads_hooks.ALLOWED_METHODS:
            raise ValueError(f"Metodo non ammesso: {', '.join(downloads_hooks.ALLOWED_METHODS)}")
        return method

    @field_validator("events")
    @classmethod
    def _check_events(cls, value):
        return _validate_events(value)


class HookUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    method: str | None = None
    headers: dict[str, str] | None = None
    body_template: str | None = None
    events: list[str] | None = None
    enabled: bool | None = None

    @field_validator("url")
    @classmethod
    def _check_url(cls, value):
        return _validate_url(value) if value is not None else None

    @field_validator("method")
    @classmethod
    def _check_method(cls, value):
        if value is None:
            return None
        method = value.upper()
        if method not in downloads_hooks.ALLOWED_METHODS:
            raise ValueError(f"Metodo non ammesso: {', '.join(downloads_hooks.ALLOWED_METHODS)}")
        return method

    @field_validator("events")
    @classmethod
    def _check_events(cls, value):
        return _validate_events(value)


def _public(hook: dict) -> dict:
    """A hook as the frontend sees it. The URL is masked: it carries the token."""
    parsed = urlparse(hook["url"])
    tail = hook["url"][-4:] if len(hook["url"]) > 4 else ""
    return {**hook, "url_masked": f"{parsed.scheme}://{parsed.hostname or '…'}/…{tail}"}


@router.get("", dependencies=CAN_MANAGE)
def list_hooks():
    return {"hooks": [_public(h) for h in downloads_hooks.list_hooks()]}


@router.post("", dependencies=CAN_MANAGE)
def create_hook(body: HookCreate):
    hook = downloads_hooks.create_hook(
        name=body.name.strip() or "Hook",
        url=body.url,
        method=body.method,
        headers=body.headers,
        body_template=body.body_template,
        events=body.events,
    )
    return {"hook": _public(hook)}


@router.patch("/{hook_id}", dependencies=CAN_MANAGE)
def update_hook(hook_id: int, body: HookUpdate):
    if downloads_hooks.get_hook(hook_id) is None:
        raise HTTPException(status_code=404, detail="Hook non trovato")
    hook = downloads_hooks.update_hook(hook_id, **body.model_dump(exclude_unset=True))
    return {"hook": _public(hook)}


@router.delete("/{hook_id}", dependencies=CAN_MANAGE)
def delete_hook(hook_id: int):
    if downloads_hooks.get_hook(hook_id) is None:
        raise HTTPException(status_code=404, detail="Hook non trovato")
    downloads_hooks.delete_hook(hook_id)
    return {"ok": True}


@router.post("/{hook_id}/test", dependencies=CAN_MANAGE)
async def test_hook(hook_id: int):
    """Fire the hook with a made-up job. Reports a status code, never a body."""
    hook = downloads_hooks.get_hook(hook_id)
    if hook is None:
        raise HTTPException(status_code=404, detail="Hook non trovato")

    tokens = {
        "title": "Titolo di prova", "path": "/libreria/Titolo di prova.mkv",
        "status": "done", "type": "film", "season": "", "episode": "",
        "year": "2024", "error": "",
    }
    ok, status = await asyncio.to_thread(downloads_hooks.fire, hook, tokens)
    return {"ok": ok, "status": status}
