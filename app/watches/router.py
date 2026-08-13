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


def acting_user_id(http_request: HttpRequest) -> int | None:
    """Who owns the watch, or None when the panel runs without accounts.

    The check is on the user the middleware resolved rather than on
    ``AUTH_ENABLED``: that constant is read at import time, and binding it in a
    fourth module would be a fourth place to patch. Both routes into open mode
    end at this same sentinel user, which has no row in ``jf_user``.
    """
    user = current_user(http_request)
    return None if user is OPEN_MODE_USER else user.id


# Following a series only ever produces downloads the caller could start anyway,
# so whoever may ask for content may follow it. What that follow then does — go
# through the queue or download straight away — is decided by the poller.
CAN_FOLLOW = [Depends(require(Permission.REQUEST, Permission.DOWNLOAD, mode="or"))]
# Without accounts there are no approvers, and the single implicit user holds
# DOWNLOAD: the admin-side views stay reachable rather than locking everyone out.
CAN_MANAGE = [
    Depends(require(Permission.MANAGE_REQUESTS, Permission.DOWNLOAD, mode="or"))
]


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
    return {"watches": [_public(w) for w in models.list_for_user(acting_user_id(http_request))]}


@router.get("", dependencies=CAN_MANAGE)
def list_watches():
    return {"watches": [_public(w) for w in models.list_all()]}


@router.get("/status", dependencies=CAN_FOLLOW)
def watch_status(source: str, media_type: str, external_id: str, http_request: HttpRequest):
    """Whether this series is followed, for the toggle's initial state."""
    user_id = acting_user_id(http_request)
    watch = models.find_open(source, media_type, external_id)
    if watch is None:
        return {"following": False, "watch_id": None, "followed_by_me": False}
    return {
        "following": True,
        "watch_id": watch.id,
        # Without accounts there is one audience, so a followed series is
        # followed by whoever is looking.
        "followed_by_me": user_id is None or user_id in models.followers(watch.id),
        "auto_approve": watch.auto_approve,
    }


@router.post("", status_code=201, dependencies=CAN_FOLLOW)
async def follow_series(body: WatchCreate, http_request: HttpRequest):
    if body.media_type not in (models.TV, models.ANIME):
        raise HTTPException(status_code=400, detail="Si possono seguire solo serie e anime")
    user_id = acting_user_id(http_request)

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
        created_by=user_id,
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

        # Asked once per series, not once per follower: a second person joining
        # an unarmed watch does not create a second decision.
        await asyncio.to_thread(_ask_for_arming, watch, user_id)

    return _public(watch)


def _ask_for_arming(watch: models.Watch, user_id: int | None) -> None:
    """Tell approvers about a follow that will need them, at the moment it is made.

    Without this the question was only asked when the source published, because
    that is when the first request appears — weeks later for a series between
    seasons, and invisible until then. Arming the series in advance means the
    first new episode downloads on its own instead of waiting in a queue.

    Nothing is sent when the series already downloads by itself: the owner can
    start downloads, or an approver has armed it before. There is no decision to
    ask for.
    """
    from app.requests import notify
    from app.watches import poller

    if user_id is None or poller.may_auto_download(watch):
        return
    who = request_models.username(user_id) or "un utente"
    notify.notify(
        notify.WATCH_NEEDS_APPROVAL,
        f"{who} segue «{watch.title}»: approva la serie per scaricare i nuovi "
        f"episodi automaticamente, altrimenti ognuno passerà dalla coda.",
        notify.approver_ids(),
    )


class AutoApproveBody(BaseModel):
    enabled: bool = True


@router.post("/{watch_id}/auto-approve", dependencies=CAN_MANAGE)
async def set_auto_approve(watch_id: int, body: AutoApproveBody):
    """Arm or disarm a followed series.

    The same flag the "Auto i prossimi" checkbox sets when approving a request,
    reachable without waiting for a request to exist.
    """
    watch = models.get(watch_id)
    if watch is None or not watch.enabled:
        raise HTTPException(status_code=404, detail="Serie non trovata")

    updated = await asyncio.to_thread(models.set_auto_approve, watch_id, body.enabled)
    if body.enabled and not watch.auto_approve:
        from app.requests import notify

        await asyncio.to_thread(
            notify.notify,
            notify.WATCH_AUTO_APPROVED,
            f"«{watch.title}»: i nuovi episodi verranno scaricati automaticamente.",
            models.followers(watch_id),
        )
    return _public(updated)


@router.delete("/{watch_id}", dependencies=CAN_FOLLOW)
async def unfollow_series(watch_id: int, http_request: HttpRequest):
    user = current_user(http_request)
    user_id = acting_user_id(http_request)
    watch = models.get(watch_id)
    if watch is None or not watch.enabled:
        raise HTTPException(status_code=404, detail="Serie non trovata")

    if user_id is not None and user.has(Permission.MANAGE_REQUESTS) \
            and user_id not in models.followers(watch_id):
        # An approver stopping someone else's watch stops it for everyone.
        await asyncio.to_thread(models.disable, watch_id)
        return {"ok": True, "stopped": True}

    stopped = await asyncio.to_thread(models.unfollow, watch_id, user_id)
    return {"ok": True, "stopped": stopped}


@router.post("/{watch_id}/check", dependencies=CAN_FOLLOW)
async def check_now(watch_id: int, http_request: HttpRequest):
    """Run this series' check immediately instead of waiting for the next cycle.

    Open to whoever follows it, not only to approvers. A follower without
    DOWNLOAD produces exactly what the automatic cycle would — a pending request
    for an approver — and until this was allowed they had no way to see their
    watch do anything at all: following seeds every published episode, so
    nothing happens until the source releases the next one, which can be weeks.
    Checking someone else's watch still takes MANAGE_REQUESTS or DOWNLOAD.
    """
    user = current_user(http_request)
    user_id = acting_user_id(http_request)
    watch = models.get(watch_id)
    if watch is None or not watch.enabled:
        raise HTTPException(status_code=404, detail="Serie non trovata")

    manages = user.has(Permission.MANAGE_REQUESTS) or user.has(Permission.DOWNLOAD)
    if not manages and user_id not in models.followers(watch_id):
        raise HTTPException(status_code=403, detail="Non segui questa serie")
    try:
        result = await asyncio.to_thread(poller.poll_watch, watch)
    except Exception as exc:
        logger.exception("Manual check failed for watch %s", watch_id)
        raise HTTPException(status_code=502, detail=f"Controllo fallito: {exc}")
    finally:
        await asyncio.to_thread(models.touch_checked, watch_id)
    return result
