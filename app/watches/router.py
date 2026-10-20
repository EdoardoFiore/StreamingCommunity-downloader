import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request as HttpRequest
from pydantic import BaseModel, Field, StringConstraints

from app.auth.deps import OPEN_MODE_USER, current_user, require
from app.auth.permissions import Permission
from app.core import paths
from app.requests import models as request_models
from app.watches import models, poller

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/watches", tags=["watches"])


def acting_user_id(http_request: HttpRequest) -> int | None:
    """Who owns the watch, or None when the panel runs without accounts.

    The check is on the user the middleware resolved rather than on the
    ``auth_mode`` setting: open mode ends at this same sentinel user, which has
    no row in ``jf_user``, so asking the middleware's answer cannot disagree
    with it — and it costs no second query.
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
# Every watch on the panel, with who follows each. Approvers only: DOWNLOAD
# lets someone start downloads, not see what other people follow. Without
# accounts the panel's watches are /mine already, so nothing is locked out.
CAN_SEE_ALL = [Depends(require(Permission.MANAGE_REQUESTS))]


class WatchCreate(BaseModel):
    source: str
    media_type: str  # tv | anime
    external_id: str
    title: str
    slug: str | None = None
    year: str | None = None
    poster: str | None = None
    # The source's own word (TV, ONA, "TV Short", …); stored, so bounded.
    anime_type: str | None = Field(default=None, max_length=32)
    audio_languages: list[str] = Field(default_factory=list)
    subtitle_languages: list[str] = Field(default_factory=list)


def _public(watch: models.Watch, http_request: HttpRequest) -> dict:
    """A watch as the caller may see it.

    Who else follows a series is an approver's business: to anyone else the
    names are withheld — the follower list, and who asked for which extra
    language. The owner still learns *that* extra languages were asked for,
    since merging them is theirs to decide.
    """
    missing = models.missing_languages(watch)
    body = watch.to_public(
        owner=request_models.username(watch.created_by),
        followers=models.follower_names(watch.id),
    )
    if not _sees_names(http_request):
        body["followers"] = []
        body["created_by_username"] = None
        missing = {**missing, "by": []}
    return {**body, "missing_languages": missing, "automatic": _is_automatic(watch)}


def _is_automatic(watch: models.Watch) -> bool:
    """Whether a new episode downloads without anyone approving it.

    Decided here, with the poller's own rule, because the browser only knows
    the viewer's permissions: an approver looking at someone else's series
    could not tell that its owner holds DOWNLOAD, and was shown "passa dalla
    coda" and an arming button that would change nothing. An ownerless watch
    (no accounts) always downloads — the poller submits it directly.
    """
    return watch.created_by is None or poller.may_auto_download(watch)


def _sees_names(http_request: HttpRequest) -> bool:
    user = current_user(http_request)
    return user is OPEN_MODE_USER or user.has(Permission.MANAGE_REQUESTS)


@router.get("/mine", dependencies=CAN_FOLLOW)
def list_my_watches(http_request: HttpRequest):
    return {"watches": [_public(w, http_request) for w in models.list_for_user(acting_user_id(http_request))]}


@router.get("", dependencies=CAN_SEE_ALL)
def list_watches(http_request: HttpRequest):
    return {"watches": [_public(w, http_request) for w in models.list_all()]}


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
        "audio_languages": watch.audio_languages,
        "subtitle_languages": watch.subtitle_languages,
    }


@router.post("", status_code=201, dependencies=CAN_FOLLOW)
async def follow_series(body: WatchCreate, http_request: HttpRequest):
    if body.media_type not in (models.TV, models.ANIME):
        raise HTTPException(status_code=400, detail="Si possono seguire solo serie e anime")
    # An anime film has no next episode, and every request a watch made for it
    # would land on the same single file.
    if body.media_type == models.ANIME and not paths.is_anime_series(body.anime_type):
        raise HTTPException(status_code=400, detail="Un film non si può seguire")
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
    elif user_id is not None:
        await asyncio.to_thread(_report_language_gap, watch, user_id)

    # Said to the follower directly: the gap in the body names nobody unless
    # the caller is an approver, so it cannot tell them it is about them.
    mine = models.missing_languages(watch, user_id) if user_id is not None else None
    return {
        **_public(watch, http_request),
        "languages_differ": bool(mine and (mine["audio"] or mine["subtitles"])),
    }


def _langs(codes: list[str]) -> str:
    return ", ".join(code.upper() for code in codes)


def _report_language_gap(watch: models.Watch, user_id: int) -> None:
    """Tell approvers when a follower joins asking for tracks the watch skips.

    The watch keeps one set of languages, the first follower's, and a join used
    to leave it at that without a word — the second follower then got episodes
    missing the language they had picked. Nothing is changed on their behalf:
    adding a track makes every future episode bigger for everyone, so it is an
    approver's call, taken with «Unisci lingue».
    """
    from app.requests import notify

    gap = models.missing_languages(watch, user_id)
    if not gap["audio"] and not gap["subtitles"]:
        return
    parts = []
    if gap["audio"]:
        parts.append(f"audio {_langs(gap['audio'])}")
    if gap["subtitles"]:
        parts.append(f"sottotitoli {_langs(gap['subtitles'])}")
    who = request_models.username(user_id) or "un utente"
    recipients = [uid for uid in notify.approver_ids() if uid != user_id]
    notify.notify(
        notify.WATCH_LANGUAGES_DIFFER,
        f"{who} segue «{watch.title}» chiedendo anche {' e '.join(parts)}, che la serie "
        f"non scarica (ora: audio {_langs(watch.audio_languages) or 'originale'}). "
        f"Puoi unire le lingue da «Serie seguite».",
        recipients,
    )


def _may_edit_languages(http_request: HttpRequest, watch: models.Watch) -> bool:
    """One watch serves every follower, so its languages are the owner's call
    or an approver's — a second follower changing them would change them for
    the first. Without accounts the watch is the panel's."""
    user_id = acting_user_id(http_request)
    return user_id is None or watch.created_by == user_id \
        or current_user(http_request).has(Permission.MANAGE_REQUESTS)


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


# ISO 639-2 codes, the vocabulary the track pickers already use, and the
# source's "forced-<code>" for forced subtitles. Anything else
# would be stored, then fail every download as a missing track.
LANG_CODE = r"^(forced-)?[a-z]{2,3}$"


class LanguagesBody(BaseModel):
    audio_languages: list[Annotated[str, StringConstraints(pattern=LANG_CODE)]] = \
        Field(default_factory=list, max_length=16)
    subtitle_languages: list[Annotated[str, StringConstraints(pattern=LANG_CODE)]] = \
        Field(default_factory=list, max_length=16)


@router.get("/{watch_id}/tracks", dependencies=CAN_FOLLOW)
async def available_tracks(watch_id: int, http_request: HttpRequest):
    """The tracks the series really has, for the languages dialog to offer.

    Read from the source on demand, not stored: tracks change over a series'
    life, and a list saved at follow time would go stale exactly when it
    matters.
    """
    watch = models.get(watch_id)
    if watch is None or not watch.enabled:
        raise HTTPException(status_code=404, detail="Serie non trovata")
    if not _may_edit_languages(http_request, watch):
        raise HTTPException(status_code=403, detail="Solo chi segue la serie per primo può cambiarne le lingue")
    try:
        return await asyncio.to_thread(poller.available_tracks, watch)
    except Exception as exc:
        logger.warning("Track lookup failed for watch %s: %s", watch_id, exc)
        raise HTTPException(status_code=502, detail=f"Tracce non leggibili dalla fonte: {exc}")


@router.put("/{watch_id}/languages", dependencies=CAN_FOLLOW)
async def set_languages(watch_id: int, body: LanguagesBody, http_request: HttpRequest):
    """Change the tracks future episodes are downloaded with.

    The languages used to be fixed at the moment «Segui» was pressed, taken
    from whatever the title page's pickers held then — usually the default,
    Italian only — with no way to change them short of unfollowing. A series
    whose first episodes were downloaded in two languages then carried on in
    one.

    One watch serves every follower, so this is the owner's call (or an
    approver's), not any follower's: a second follower changing it would change
    it for the first.
    """
    watch = models.get(watch_id)
    if watch is None or not watch.enabled:
        raise HTTPException(status_code=404, detail="Serie non trovata")
    if not _may_edit_languages(http_request, watch):
        raise HTTPException(status_code=403, detail="Solo chi segue la serie per primo può cambiarne le lingue")

    updated = await asyncio.to_thread(
        models.set_languages, watch_id, body.audio_languages, body.subtitle_languages,
    )
    return _public(updated, http_request)


@router.post("/{watch_id}/merge-languages", dependencies=CAN_FOLLOW)
async def merge_languages(watch_id: int, http_request: HttpRequest):
    """Download every audio and subtitle language any follower asked for.

    The answer to a WATCH_LANGUAGES_DIFFER notification. Applies from the next
    episode: the ones already in the library keep the tracks they have.
    """
    watch = models.get(watch_id)
    if watch is None or not watch.enabled:
        raise HTTPException(status_code=404, detail="Serie non trovata")
    if not _may_edit_languages(http_request, watch):
        raise HTTPException(status_code=403, detail="Solo chi segue la serie per primo può cambiarne le lingue")

    updated = await asyncio.to_thread(models.merge_languages, watch_id)
    return _public(updated, http_request)


class AutoApproveBody(BaseModel):
    enabled: bool = True


@router.post("/{watch_id}/auto-approve", dependencies=CAN_MANAGE)
async def set_auto_approve(watch_id: int, body: AutoApproveBody, http_request: HttpRequest):
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
    return _public(updated, http_request)


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
