"""Request endpoints.

Two rules run through this module: the requester is always taken from the
session, and the chosen tracks are always validated against what the source
actually offers — both arrive from a POST body otherwise.
"""

import asyncio
import logging
import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Request as HttpRequest
from pydantic import BaseModel, Field, field_validator

from app.auth.deps import current_user, require
from app.auth.permissions import Permission
from app.requests import models, notify, resolver, service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/requests", tags=["requests"])

CAN_REQUEST = [Depends(require(Permission.REQUEST))]
CAN_MANAGE = [Depends(require(Permission.MANAGE_REQUESTS))]
# Seeing your own requests only needs the ability to have made one.
CAN_SEE_OWN = [Depends(require(Permission.REQUEST, Permission.DOWNLOAD, mode="or"))]

# Language codes end up in filenames and in query strings.
LANGUAGE_CODE = re.compile(r"^[a-z]{2,3}$")
MAX_LANGUAGES = 10


def _validate_languages(value: list[str]) -> list[str]:
    if len(value) > MAX_LANGUAGES:
        raise ValueError("troppe lingue selezionate")
    for code in value:
        if not LANGUAGE_CODE.match(code):
            raise ValueError(f"codice lingua non valido: {code!r}")
    return sorted(set(value))


class CreateRequest(BaseModel):
    source: str = Field(pattern="^(streamingcommunity|animeunity)$")
    media_type: str = Field(pattern="^(film|episode|anime)$")
    external_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=300)
    slug: str | None = Field(default=None, max_length=300)
    year: str | None = Field(default=None, max_length=10)
    poster: str | None = Field(default=None, max_length=500)
    season: int | None = Field(default=None, ge=0, le=200)
    episode_number: str | None = Field(default=None, max_length=16)
    anime_type: str | None = Field(default=None, pattern="^(tv|movie|serie|series|anime)$")
    audio_languages: list[str] = Field(default_factory=lambda: ["ita"])
    subtitle_languages: list[str] = Field(default_factory=list)

    @field_validator("audio_languages", "subtitle_languages")
    @classmethod
    def _languages(cls, value):
        return _validate_languages(value)


class DenyRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class EditRequest(BaseModel):
    """What an approver may correct on a request that needs attention.

    Deliberately not a free-form source URL: that would let a form point the
    downloader at any host. Re-binding to a different item on the same source
    covers the real case, which is content that moved.
    """
    external_id: str | None = Field(default=None, min_length=1, max_length=64)
    slug: str | None = Field(default=None, max_length=300)
    season: int | None = Field(default=None, ge=0, le=200)
    episode_number: str | None = Field(default=None, max_length=16)
    audio_languages: list[str] | None = None
    subtitle_languages: list[str] | None = None

    @field_validator("audio_languages", "subtitle_languages")
    @classmethod
    def _languages(cls, value):
        return None if value is None else _validate_languages(value)


class StatusQuery(BaseModel):
    source: str = Field(pattern="^(streamingcommunity|animeunity)$")
    external_ids: list[str] = Field(max_length=100)


class MarkReadRequest(BaseModel):
    ids: list[int] | None = None


def _public(request: models.Request) -> dict:
    return request.to_public(
        requester=models.username(request.requested_by),
        decider=models.username(request.decided_by),
        subscribers=models.subscriber_names(request.id),
    )


# ── Creating ───────────────────────────────────────────────────────────────────

@router.post("", status_code=201, dependencies=CAN_REQUEST)
async def create_request(body: CreateRequest, http_request: HttpRequest):
    user = current_user(http_request)

    if body.media_type == "episode" and (body.season is None or body.episode_number is None):
        raise HTTPException(status_code=400, detail="Stagione ed episodio sono obbligatori")
    if body.media_type == "anime" and body.episode_number is None:
        raise HTTPException(status_code=400, detail="Numero episodio obbligatorio")

    draft = models.Request(
        id=0, content_key="", source=body.source, media_type=body.media_type,
        external_id=body.external_id, slug=body.slug, title=body.title, year=body.year,
        poster=body.poster, season=body.season, episode_number=body.episode_number,
        anime_type=body.anime_type, audio_languages=body.audio_languages,
        subtitle_languages=body.subtitle_languages, available_snapshot=None,
        status=models.PENDING, problem=None, denial_reason=None, requested_by=user.id,
        decided_by=None, decided_at=None, job_id=None, output_path=None,
        created_at="", updated_at="",
    )

    # The tracks are checked against the source, not against what the client
    # claims is on offer. This is also what gives the requester a real snapshot.
    try:
        available = await asyncio.to_thread(resolver.available_tracks, draft)
    except resolver.ResolutionError as exc:
        raise HTTPException(status_code=502, detail=exc.message)
    except Exception as exc:
        logger.warning("Could not resolve tracks for a new request: %s", exc)
        raise HTTPException(status_code=502, detail=f"Fonte non raggiungibile: {exc}")

    offered = {str(c).lower() for c in (available.get("audio") or [])}
    if offered:
        unavailable = [l for l in body.audio_languages if l.lower() not in offered]
        if unavailable:
            raise HTTPException(
                status_code=400,
                detail=f"Audio non disponibile: {', '.join(unavailable)}. "
                       f"Disponibili: {', '.join(sorted(offered))}",
            )

    request, created = await asyncio.to_thread(
        service.create_request,
        requested_by=user.id,
        source=body.source,
        media_type=body.media_type,
        external_id=body.external_id,
        title=body.title,
        slug=body.slug,
        year=body.year,
        poster=body.poster,
        season=body.season,
        episode_number=body.episode_number,
        anime_type=body.anime_type,
        audio_languages=body.audio_languages,
        subtitle_languages=body.subtitle_languages,
        available_snapshot=available,
    )
    return {"request": _public(request), "created": created}


# ── Reading ────────────────────────────────────────────────────────────────────

@router.get("", dependencies=CAN_MANAGE)
def list_requests(status: str = Query(default=None)):
    statuses = tuple(s for s in (status or "").split(",") if s) or None
    return [_public(r) for r in models.list_all(statuses)]


@router.get("/mine", dependencies=CAN_SEE_OWN)
def list_my_requests(http_request: HttpRequest):
    user = current_user(http_request)
    return [_public(r) for r in models.list_for_user(user.id)]


@router.post("/status", dependencies=CAN_SEE_OWN)
def request_status(body: StatusQuery, http_request: HttpRequest):
    """Status per external id, for the badges on the search result cards."""
    user = current_user(http_request)
    can_see_all = user.has(Permission.MANAGE_REQUESTS)
    wanted = set(body.external_ids)

    result: dict[str, dict] = {}
    for request in models.list_all():
        if request.source != body.source or request.external_id not in wanted:
            continue
        if not can_see_all and user.id not in models.subscribers(request.id):
            continue
        # An open or completed state beats an older closed one on the same card.
        current = result.get(request.external_id)
        if current is None or request.status in models.OPEN_STATUSES:
            result[request.external_id] = {
                "id": request.id,
                "status": request.status,
                "media_type": request.media_type,
                "season": request.season,
                "episode_number": request.episode_number,
            }
    return result


@router.get("/{request_id}", dependencies=CAN_SEE_OWN)
def get_request(request_id: int, http_request: HttpRequest):
    user = current_user(http_request)
    request = models.get(request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Richiesta non trovata")
    if not user.has(Permission.MANAGE_REQUESTS) and user.id not in models.subscribers(request_id):
        raise HTTPException(status_code=404, detail="Richiesta non trovata")
    return _public(request)


# ── Deciding ───────────────────────────────────────────────────────────────────

@router.post("/{request_id}/approve", status_code=202, dependencies=CAN_MANAGE)
def approve_request(request_id: int, http_request: HttpRequest):
    """Approve and return at once — resolution and download run in the background."""
    user = current_user(http_request)
    try:
        request = service.approve(request_id, user.id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Richiesta non trovata")
    except models.InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _public(request)


@router.post("/{request_id}/deny", dependencies=CAN_MANAGE)
def deny_request(request_id: int, body: DenyRequest, http_request: HttpRequest):
    user = current_user(http_request)
    try:
        request = service.deny(request_id, user.id, body.reason)
    except LookupError:
        raise HTTPException(status_code=404, detail="Richiesta non trovata")
    except models.InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _public(request)


@router.patch("/{request_id}", dependencies=CAN_MANAGE)
def edit_request(request_id: int, body: EditRequest):
    """Correct a request that needs attention, before re-approving it."""
    request = models.get(request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Richiesta non trovata")
    if request.status != models.NEEDS_ATTENTION:
        raise HTTPException(
            status_code=409,
            detail="Solo una richiesta in stato 'needs_attention' può essere corretta",
        )

    changes = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not changes:
        return _public(request)

    import json
    stored = dict(changes)
    for key in ("audio_languages", "subtitle_languages"):
        if key in stored:
            stored[key] = json.dumps(stored[key])

    try:
        updated = models.update_fields(request_id, **stored)
        models.rekey(request_id, updated)
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail="Esiste già una richiesta aperta identica con queste correzioni",
        )
    return _public(models.get(request_id))


@router.delete("/{request_id}", dependencies=CAN_SEE_OWN)
def cancel_request(request_id: int, http_request: HttpRequest):
    """Withdraw a request. The requester may cancel their own; approvers, any."""
    user = current_user(http_request)
    request = models.get(request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Richiesta non trovata")

    is_subscriber = user.id in models.subscribers(request_id)
    if not user.has(Permission.MANAGE_REQUESTS) and not is_subscriber:
        raise HTTPException(status_code=404, detail="Richiesta non trovata")
    if not user.has(Permission.MANAGE_REQUESTS) and request.status != models.PENDING:
        raise HTTPException(
            status_code=409, detail="Solo una richiesta ancora in attesa può essere annullata"
        )

    try:
        return _public(service.cancel(request_id, user.id))
    except models.InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ── Notifications ──────────────────────────────────────────────────────────────

notifications_router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@notifications_router.get("", dependencies=CAN_SEE_OWN)
def list_notifications(http_request: HttpRequest):
    user = current_user(http_request)
    return {
        "items": notify.list_for_user(user.id),
        "unread": notify.unread_count(user.id),
    }


@notifications_router.post("/read", dependencies=CAN_SEE_OWN)
def mark_notifications_read(body: MarkReadRequest, http_request: HttpRequest):
    user = current_user(http_request)
    notify.mark_read(user.id, body.ids)
    return {"unread": notify.unread_count(user.id)}
