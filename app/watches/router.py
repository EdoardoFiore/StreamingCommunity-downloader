import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request as HttpRequest
from pydantic import BaseModel, Field

from app.auth.deps import OPEN_MODE_USER, current_user, require
from app.auth.permissions import Permission
from app.requests import models as request_models
from app.watches import models, poller

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/watches", tags=["watches"])


def require_accounts(http_request: HttpRequest):
    """Refuse in open mode, where there is no account to own a watch.

    A watch is stored against a user: they own it, they are notified about it,
    and their DOWNLOAD permission decides whether new episodes skip the queue.
    Open mode has one implicit user with no row in ``jf_user``, so following a
    series would fail on the foreign key — the same reasoning that keeps the
    request queue out of open mode.

    The check is on the user the middleware resolved rather than on
    ``AUTH_ENABLED``: that constant is read at import time, and binding it in a
    fourth module would be a fourth place to patch. Both routes into open mode
    end at this same sentinel user anyway.
    """
    if current_user(http_request) is OPEN_MODE_USER:
        raise HTTPException(
            status_code=400,
            detail="Le serie seguite richiedono il collegamento a Jellyfin",
        )
    return current_user(http_request)


# Following a series only ever produces requests, so whoever may ask for content
# may follow it; the permission check that matters happens later, when the poller
# decides whether a new episode skips the queue.
CAN_FOLLOW = [
    Depends(require(Permission.REQUEST, Permission.DOWNLOAD, mode="or")),
    Depends(require_accounts),
]
CAN_MANAGE = [Depends(require(Permission.MANAGE_REQUESTS)), Depends(require_accounts)]


class WatchCreate(BaseModel):
    source: str
    media_type: str  # tv | anime
    external_id: str
    title: str
    slug: str | None = None
    year: str | None = None
    poster: str | None = None
    anime_type: str | None = None
    audio_languages: list[str] = Field(default_factory=list)
    subtitle_languages: list[str] = Field(default_factory=list)


def _public(watch: models.Watch) -> dict:
    return watch.to_public(
        owner=request_models.username(watch.created_by),
        followers=models.follower_names(watch.id),
    )


@router.get("/mine", dependencies=CAN_FOLLOW)
def list_my_watches(http_request: HttpRequest):
    user = current_user(http_request)
    return {"watches": [_public(w) for w in models.list_for_user(user.id)]}


@router.get("", dependencies=CAN_MANAGE)
def list_watches():
    return {"watches": [_public(w) for w in models.list_all()]}


@router.get("/status", dependencies=CAN_FOLLOW)
def watch_status(source: str, media_type: str, external_id: str, http_request: HttpRequest):
    """Whether this series is followed, for the toggle's initial state."""
    user = current_user(http_request)
    watch = models.find_open(source, media_type, external_id)
    if watch is None:
        return {"following": False, "watch_id": None, "followed_by_me": False}
    return {
        "following": True,
        "watch_id": watch.id,
        "followed_by_me": user.id in models.followers(watch.id),
        "auto_approve": watch.auto_approve,
    }


@router.post("", status_code=201, dependencies=CAN_FOLLOW)
async def follow_series(body: WatchCreate, http_request: HttpRequest):
    if body.media_type not in (models.TV, models.ANIME):
        raise HTTPException(status_code=400, detail="Si possono seguire solo serie e anime")
    user = current_user(http_request)

    watch, created = await asyncio.to_thread(
        models.create,
        source=body.source,
        media_type=body.media_type,
        external_id=body.external_id,
        title=body.title,
        slug=body.slug,
        year=body.year,
        poster=body.poster,
        anime_type=body.anime_type,
        audio_languages=body.audio_languages,
        subtitle_languages=body.subtitle_languages,
        created_by=user.id,
    )

    if created:
        # Everything already published counts as handled: following means "tell
        # me about what comes next", not "download the back catalogue".
        try:
            episodes = await asyncio.to_thread(poller.current_episodes, watch)
            if not episodes:
                # A followable series always has at least one episode, so an
                # empty list means the source could not be read — not that there
                # is nothing to watch. Arming on it would treat the whole back
                # catalogue as new once the source recovers.
                raise RuntimeError("nessun episodio letto dalla fonte")
            await asyncio.to_thread(models.seed_seen, watch.id, [key for key, _ in episodes])
        except Exception:
            # Without a baseline the next cycle would queue the whole series, so
            # the watch is rolled back rather than left armed.
            logger.exception("Could not seed watch %s (%s)", watch.id, watch.title)
            await asyncio.to_thread(models.disable, watch.id)
            raise HTTPException(
                status_code=502,
                detail="Impossibile leggere gli episodi dalla fonte: serie non seguita",
            )

    return _public(watch)


@router.delete("/{watch_id}", dependencies=CAN_FOLLOW)
async def unfollow_series(watch_id: int, http_request: HttpRequest):
    user = current_user(http_request)
    watch = models.get(watch_id)
    if watch is None or not watch.enabled:
        raise HTTPException(status_code=404, detail="Serie non trovata")

    if user.has(Permission.MANAGE_REQUESTS) and user.id not in models.followers(watch_id):
        # An approver stopping someone else's watch stops it for everyone.
        await asyncio.to_thread(models.disable, watch_id)
        return {"ok": True, "stopped": True}

    stopped = await asyncio.to_thread(models.unfollow, watch_id, user.id)
    return {"ok": True, "stopped": stopped}


@router.post("/{watch_id}/check", dependencies=CAN_MANAGE)
async def check_now(watch_id: int):
    """Run this series' check immediately instead of waiting for the next cycle."""
    watch = models.get(watch_id)
    if watch is None or not watch.enabled:
        raise HTTPException(status_code=404, detail="Serie non trovata")
    try:
        result = await asyncio.to_thread(poller.poll_watch, watch)
    except Exception as exc:
        logger.exception("Manual check failed for watch %s", watch_id)
        raise HTTPException(status_code=502, detail=f"Controllo fallito: {exc}")
    finally:
        await asyncio.to_thread(models.touch_checked, watch_id)
    return result
