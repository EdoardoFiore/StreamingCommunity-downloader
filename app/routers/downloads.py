import asyncio
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request as HttpRequest
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import downloads_notify
from app.auth.deps import OPEN_MODE_USER, current_user, require
from app.auth.permissions import Permission
from app.config import configured_domain
from app.jobs import job_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/download", tags=["downloads"])

# One request can queue a whole series. The cap keeps a mis-click from filling
# the executor and the event stream with thousands of jobs.
MAX_BATCH_JOBS = 500

# Starting a download directly is the privilege this whole feature exists to
# gate: without it a user goes through the request queue instead.
CAN_DOWNLOAD = [Depends(require(Permission.DOWNLOAD))]

# Job control is also reachable by approvers, who must be able to stop a
# download they approved without being allowed to start one themselves.
CAN_CONTROL_JOBS = [
    Depends(require(Permission.DOWNLOAD, Permission.MANAGE_REQUESTS, mode="or"))
]


def _domain() -> str:
    """The configured source host, never one supplied by the caller."""
    domain = configured_domain()
    if not domain:
        raise HTTPException(status_code=409, detail="Nessun dominio configurato")
    return domain


def _tmdb_id(media_type: str, title_id) -> int | None:
    """The TMDB id for this title, if the panel already happens to know it.

    Resolved server-side rather than accepted from the request body: it decides
    which third-party host a failed download falls back to, which is not a
    choice a client gets to make. A pure cache read, so a download that is about
    to work pays nothing — the detail modal has usually just filled it in, and a
    miss simply means no fallback, which is what there was before.
    """
    from app.core import metadata

    try:
        return metadata.cached_tmdb_id(media_type, title_id)
    except Exception:
        return None


class FilmDownloadRequest(BaseModel):
    id: int
    title: str
    year: str | None = None
    audio_languages: list[str] = ["ita"]
    subtitle_languages: list[str] = ["ita", "eng"]


class EpisodeDownloadRequest(BaseModel):
    tv_id: int
    eps: list[dict]
    ep_index: int
    token: str
    tv_name: str
    season: int
    year: str | None = None
    audio_languages: list[str] = ["ita"]
    subtitle_languages: list[str] = ["ita", "eng"]


class EpisodeInfo(BaseModel):
    id: int
    number: str | int


class AnimeDownloadRequest(BaseModel):
    anime_id: str
    episode: EpisodeInfo
    anime_name: str
    anime_type: str = "tv"
    year: str | None = None
    audio_languages: list[str] = ["ita"]
    subtitle_languages: list[str] = ["ita", "eng"]


class SeasonDownloadRequest(BaseModel):
    """A whole season, enumerated server-side.

    Deliberately no episode list, token or domain: the browser used to send all
    three back on every episode, and the token was stale by the last one. The
    client only says which season of what.
    """
    tv_id: int
    slug: str
    tv_name: str
    season: int
    year: str | None = None
    audio_languages: list[str] = ["ita"]
    subtitle_languages: list[str] = ["ita", "eng"]
    scheduled_at: datetime | None = None


class SeriesDownloadRequest(BaseModel):
    tv_id: int
    slug: str
    tv_name: str
    year: str | None = None
    audio_languages: list[str] = ["ita"]
    subtitle_languages: list[str] = ["ita", "eng"]
    scheduled_at: datetime | None = None


class AnimeAllDownloadRequest(BaseModel):
    anime_id: str
    anime_name: str
    anime_type: str = "tv"
    year: str | None = None
    audio_languages: list[str] = ["ita"]
    subtitle_languages: list[str] = ["ita", "eng"]
    scheduled_at: datetime | None = None


class FilmScheduleRequest(FilmDownloadRequest):
    scheduled_at: datetime


class EpisodeScheduleRequest(EpisodeDownloadRequest):
    scheduled_at: datetime


class AnimeScheduleRequest(AnimeDownloadRequest):
    scheduled_at: datetime


# ── Batch downloads ────────────────────────────────────────────────────────────
#
# A season or a series is enumerated here rather than by the browser looping one
# POST per episode. The point is closure: knowing the expected total before the
# first job exists is what lets the notification layer say "season finished"
# instead of guessing with a timer.

def _acting_user_id(http_request: HttpRequest) -> int | None:
    """The in-app recipient. None in open mode, which has no account to notify."""
    user = current_user(http_request)
    return None if user is OPEN_MODE_USER else user.id


def _season_episodes(tv_id: int, slug: str, season: int, domain: str, version: str, token: str):
    from app.core.tv import get_info_season

    return get_info_season(tv_id, slug, domain, version, token, season) or []


def _tv_context(tv_id: int):
    from app.core.page import get_domain_version
    from app.core.tv import get_token

    domain = _domain()
    version = get_domain_version(domain) or ""
    return domain, version, get_token(tv_id, domain)


def _enumerate(work):
    """Run a server-side enumeration, turning an unreachable source into a 502.

    The domain rotates and links die; that is an expected answer, not a crash,
    and the caller gets a message it can show instead of a bare 500.
    """
    try:
        return work()
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Batch enumeration failed: %s", exc)
        raise HTTPException(
            status_code=502, detail="Impossibile leggere gli episodi dalla fonte"
        )


def _check_size(count: int):
    if count == 0:
        raise HTTPException(status_code=409, detail="Nessun episodio disponibile sulla fonte")
    if count > MAX_BATCH_JOBS:
        raise HTTPException(
            status_code=400,
            detail=f"Troppi episodi in un'unica richiesta ({count}, massimo {MAX_BATCH_JOBS})",
        )


def _run_batch(kind: str, label: str, user_id: int | None, submits: list) -> dict:
    """Register the batch, then submit. Never the other way round.

    A job can fail in the instant it is created, and a result arriving before
    its batch exists would be counted against nothing.
    """
    batch_id = uuid.uuid4().hex
    downloads_notify.register(batch_id, kind=kind, label=label,
                              expected=len(submits), user_id=user_id)
    job_ids = []
    for index, submit in enumerate(submits):
        try:
            job_ids.append(submit(batch_id))
        except Exception:
            logger.exception("Batch %s: submit failed for item %s", batch_id, index)
            # Write off everything still unsubmitted, so the batch can close on
            # the jobs that did start.
            downloads_notify.abandon(batch_id, len(submits) - index)
            break
    return {"batch_id": batch_id, "job_ids": job_ids, "count": len(job_ids)}


def _episode_submits(body, tv_id, slug, tv_name, season, domain, version, token, user_id, kind, label):
    episodes = _season_episodes(tv_id, slug, season, domain, version, token)

    def make(index):
        def submit(batch_id):
            common = dict(
                year=body.year,
                audio_languages=body.audio_languages,
                subtitle_languages=body.subtitle_languages,
                user_id=user_id, batch_id=batch_id, batch_kind=kind, batch_label=label,
                tmdb_id=_tmdb_id("tv", tv_id),
            )
            if body.scheduled_at:
                return job_manager.schedule_episode(
                    tv_id, episodes, index, domain, token, tv_name, season,
                    body.scheduled_at, **common,
                )
            return job_manager.submit_episode(
                tv_id, episodes, index, domain, token, tv_name, season, **common,
            )
        return submit

    return [make(i) for i in range(len(episodes))]


@router.post("/season", status_code=202, dependencies=CAN_DOWNLOAD)
async def download_season(body: SeasonDownloadRequest, http_request: HttpRequest):
    user_id = _acting_user_id(http_request)
    label = f"{body.tv_name} — Stagione {body.season}"

    def work():
        domain, version, token = _tv_context(body.tv_id)
        submits = _episode_submits(body, body.tv_id, body.slug, body.tv_name, body.season,
                                   domain, version, token, user_id, "season", label)
        _check_size(len(submits))
        return _run_batch("season", label, user_id, submits)

    result = await asyncio.to_thread(_enumerate, work)
    return {**result, "status": "scheduled" if body.scheduled_at else "queued"}


@router.post("/series", status_code=202, dependencies=CAN_DOWNLOAD)
async def download_series(body: SeriesDownloadRequest, http_request: HttpRequest):
    user_id = _acting_user_id(http_request)

    def work():
        from app.core.tv import get_info_tv

        domain, version, token = _tv_context(body.tv_id)
        # The season count comes from the source, never from the client.
        seasons = get_info_tv(body.tv_id, body.slug, version, domain) or 0
        submits = []
        for season in range(1, seasons + 1):
            try:
                submits += _episode_submits(body, body.tv_id, body.slug, body.tv_name, season,
                                            domain, version, token, user_id, "series", body.tv_name)
            except Exception:
                # One unavailable season must not sink the whole series.
                logger.exception("Series %s: season %s could not be listed", body.tv_id, season)
        _check_size(len(submits))
        return _run_batch("series", body.tv_name, user_id, submits)

    result = await asyncio.to_thread(_enumerate, work)
    return {**result, "status": "scheduled" if body.scheduled_at else "queued"}


@router.post("/anime-all", status_code=202, dependencies=CAN_DOWNLOAD)
async def download_anime_all(body: AnimeAllDownloadRequest, http_request: HttpRequest):
    user_id = _acting_user_id(http_request)

    def work():
        from app.core.animeunity import get_episodes

        episodes = get_episodes(body.anime_id) or []
        _check_size(len(episodes))

        def make(episode):
            def submit(batch_id):
                common = dict(
                    anime_type=body.anime_type, year=body.year,
                    audio_languages=body.audio_languages,
                    subtitle_languages=body.subtitle_languages,
                    user_id=user_id, batch_id=batch_id, batch_kind="anime_all",
                    batch_label=body.anime_name,
                )
                if body.scheduled_at:
                    return job_manager.schedule_anime_episode(
                        body.anime_id, episode, body.anime_name, body.scheduled_at, **common,
                    )
                return job_manager.submit_anime_episode(
                    body.anime_id, episode, body.anime_name, **common,
                )
            return submit

        return _run_batch("anime_all", body.anime_name, user_id,
                          [make(ep) for ep in episodes])

    result = await asyncio.to_thread(_enumerate, work)
    return {**result, "status": "scheduled" if body.scheduled_at else "queued"}


# ── Immediate downloads ────────────────────────────────────────────────────────

@router.post("/film", status_code=202, dependencies=CAN_DOWNLOAD)
def download_film(body: FilmDownloadRequest, http_request: HttpRequest):
    job_id = job_manager.submit_film(
        body.id, body.title, _domain(), year=body.year,
        audio_languages=body.audio_languages,
        subtitle_languages=body.subtitle_languages,
        user_id=_acting_user_id(http_request),
        tmdb_id=_tmdb_id("movie", body.id),
    )
    return {"job_id": job_id, "status": "queued"}


@router.post("/episode", status_code=202, dependencies=CAN_DOWNLOAD)
def download_episode(body: EpisodeDownloadRequest, http_request: HttpRequest):
    if body.ep_index < 0 or body.ep_index >= len(body.eps):
        raise HTTPException(status_code=400, detail="ep_index out of range")
    job_id = job_manager.submit_episode(
        body.tv_id, body.eps, body.ep_index,
        _domain(), body.token, body.tv_name, body.season,
        year=body.year,
        audio_languages=body.audio_languages,
        subtitle_languages=body.subtitle_languages,
        user_id=_acting_user_id(http_request),
        tmdb_id=_tmdb_id("tv", body.tv_id),
    )
    return {"job_id": job_id, "status": "queued"}


@router.post("/anime", status_code=202, dependencies=CAN_DOWNLOAD)
def download_anime(body: AnimeDownloadRequest, http_request: HttpRequest):
    job_id = job_manager.submit_anime_episode(
        body.anime_id, body.episode.model_dump(), body.anime_name, body.anime_type, year=body.year,
        audio_languages=body.audio_languages,
        subtitle_languages=body.subtitle_languages,
        user_id=_acting_user_id(http_request),
    )
    return {"job_id": job_id, "status": "queued"}


# ── Scheduled downloads ────────────────────────────────────────────────────────

@router.post("/schedule/film", status_code=202, dependencies=CAN_DOWNLOAD)
def schedule_film(body: FilmScheduleRequest, http_request: HttpRequest):
    job_id = job_manager.schedule_film(
        body.id, body.title, _domain(), body.scheduled_at, year=body.year,
        audio_languages=body.audio_languages,
        subtitle_languages=body.subtitle_languages,
        user_id=_acting_user_id(http_request),
    )
    return {"job_id": job_id, "status": "scheduled", "scheduled_at": body.scheduled_at.isoformat()}


@router.post("/schedule/episode", status_code=202, dependencies=CAN_DOWNLOAD)
def schedule_episode(body: EpisodeScheduleRequest, http_request: HttpRequest):
    if body.ep_index < 0 or body.ep_index >= len(body.eps):
        raise HTTPException(status_code=400, detail="ep_index out of range")
    job_id = job_manager.schedule_episode(
        body.tv_id, body.eps, body.ep_index,
        _domain(), body.token, body.tv_name, body.season,
        body.scheduled_at, year=body.year,
        audio_languages=body.audio_languages,
        subtitle_languages=body.subtitle_languages,
        user_id=_acting_user_id(http_request),
    )
    return {"job_id": job_id, "status": "scheduled", "scheduled_at": body.scheduled_at.isoformat()}


@router.post("/schedule/anime", status_code=202, dependencies=CAN_DOWNLOAD)
def schedule_anime(body: AnimeScheduleRequest, http_request: HttpRequest):
    job_id = job_manager.schedule_anime_episode(
        body.anime_id, body.episode.model_dump(), body.anime_name, body.scheduled_at,
        anime_type=body.anime_type, year=body.year,
        audio_languages=body.audio_languages,
        subtitle_languages=body.subtitle_languages,
        user_id=_acting_user_id(http_request),
    )
    return {"job_id": job_id, "status": "scheduled", "scheduled_at": body.scheduled_at.isoformat()}


# ── Job management ─────────────────────────────────────────────────────────────

@router.post("/{job_id}/fire", status_code=200, dependencies=CAN_CONTROL_JOBS)
def fire_now(job_id: str):
    if job_manager.fire_now(job_id):
        return {"job_id": job_id, "status": "queued"}
    raise HTTPException(status_code=404, detail="Job non trovato o non in stato programmato")


@router.delete("/{job_id}", status_code=200, dependencies=CAN_CONTROL_JOBS)
def cancel_or_dismiss(job_id: str):
    """Cancel a running/queued/scheduled job, or dismiss a finished one (also cleans schedule store)."""
    if job_manager.dismiss(job_id):
        return {"job_id": job_id, "status": "dismissed"}
    if job_manager.cancel(job_id):
        return {"job_id": job_id, "status": "cancelled"}
    raise HTTPException(status_code=404, detail="Job non trovato")
