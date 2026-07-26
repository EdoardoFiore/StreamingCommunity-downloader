import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth.deps import require
from app.auth.permissions import Permission
from app.config import configured_domain
from app.jobs import job_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/download", tags=["downloads"])

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


class FilmScheduleRequest(FilmDownloadRequest):
    scheduled_at: datetime


class EpisodeScheduleRequest(EpisodeDownloadRequest):
    scheduled_at: datetime


class AnimeScheduleRequest(AnimeDownloadRequest):
    scheduled_at: datetime


# ── Immediate downloads ────────────────────────────────────────────────────────

@router.post("/film", status_code=202, dependencies=CAN_DOWNLOAD)
def download_film(body: FilmDownloadRequest):
    job_id = job_manager.submit_film(
        body.id, body.title, _domain(), year=body.year,
        audio_languages=body.audio_languages,
        subtitle_languages=body.subtitle_languages,
    )
    return {"job_id": job_id, "status": "queued"}


@router.post("/episode", status_code=202, dependencies=CAN_DOWNLOAD)
def download_episode(body: EpisodeDownloadRequest):
    if body.ep_index < 0 or body.ep_index >= len(body.eps):
        raise HTTPException(status_code=400, detail="ep_index out of range")
    job_id = job_manager.submit_episode(
        body.tv_id, body.eps, body.ep_index,
        _domain(), body.token, body.tv_name, body.season,
        year=body.year,
        audio_languages=body.audio_languages,
        subtitle_languages=body.subtitle_languages,
    )
    return {"job_id": job_id, "status": "queued"}


@router.post("/anime", status_code=202, dependencies=CAN_DOWNLOAD)
def download_anime(body: AnimeDownloadRequest):
    job_id = job_manager.submit_anime_episode(
        body.anime_id, body.episode.model_dump(), body.anime_name, body.anime_type, year=body.year,
        audio_languages=body.audio_languages,
        subtitle_languages=body.subtitle_languages,
    )
    return {"job_id": job_id, "status": "queued"}


# ── Scheduled downloads ────────────────────────────────────────────────────────

@router.post("/schedule/film", status_code=202, dependencies=CAN_DOWNLOAD)
def schedule_film(body: FilmScheduleRequest):
    job_id = job_manager.schedule_film(
        body.id, body.title, _domain(), body.scheduled_at, year=body.year,
        audio_languages=body.audio_languages,
        subtitle_languages=body.subtitle_languages,
    )
    return {"job_id": job_id, "status": "scheduled", "scheduled_at": body.scheduled_at.isoformat()}


@router.post("/schedule/episode", status_code=202, dependencies=CAN_DOWNLOAD)
def schedule_episode(body: EpisodeScheduleRequest):
    if body.ep_index < 0 or body.ep_index >= len(body.eps):
        raise HTTPException(status_code=400, detail="ep_index out of range")
    job_id = job_manager.schedule_episode(
        body.tv_id, body.eps, body.ep_index,
        _domain(), body.token, body.tv_name, body.season,
        body.scheduled_at, year=body.year,
        audio_languages=body.audio_languages,
        subtitle_languages=body.subtitle_languages,
    )
    return {"job_id": job_id, "status": "scheduled", "scheduled_at": body.scheduled_at.isoformat()}


@router.post("/schedule/anime", status_code=202, dependencies=CAN_DOWNLOAD)
def schedule_anime(body: AnimeScheduleRequest):
    job_id = job_manager.schedule_anime_episode(
        body.anime_id, body.episode.model_dump(), body.anime_name, body.scheduled_at,
        anime_type=body.anime_type, year=body.year,
        audio_languages=body.audio_languages,
        subtitle_languages=body.subtitle_languages,
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
