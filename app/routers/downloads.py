import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.jobs import job_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/download", tags=["downloads"])


class FilmDownloadRequest(BaseModel):
    id: int
    title: str
    domain: str
    year: str | None = None


class EpisodeDownloadRequest(BaseModel):
    tv_id: int
    eps: list[dict]
    ep_index: int
    domain: str
    token: str
    tv_name: str
    season: int
    year: str | None = None


class AnimeDownloadRequest(BaseModel):
    anime_id: str
    episode: dict   # {"id": <ep_id>, "number": "<ep_number>"}
    anime_name: str
    anime_type: str = "tv"  # "tv", "movie", "film", etc.
    year: str | None = None


class FilmScheduleRequest(FilmDownloadRequest):
    scheduled_at: datetime


class EpisodeScheduleRequest(EpisodeDownloadRequest):
    scheduled_at: datetime


class AnimeScheduleRequest(AnimeDownloadRequest):
    scheduled_at: datetime


# ── Immediate downloads ────────────────────────────────────────────────────────

@router.post("/film", status_code=202)
def download_film(body: FilmDownloadRequest):
    job_id = job_manager.submit_film(body.id, body.title, body.domain, year=body.year)
    return {"job_id": job_id, "status": "queued"}


@router.post("/episode", status_code=202)
def download_episode(body: EpisodeDownloadRequest):
    if body.ep_index < 0 or body.ep_index >= len(body.eps):
        raise HTTPException(status_code=400, detail="ep_index out of range")
    job_id = job_manager.submit_episode(
        body.tv_id, body.eps, body.ep_index,
        body.domain, body.token, body.tv_name, body.season,
        year=body.year,
    )
    return {"job_id": job_id, "status": "queued"}


@router.post("/anime", status_code=202)
def download_anime(body: AnimeDownloadRequest):
    job_id = job_manager.submit_anime_episode(
        body.anime_id, body.episode, body.anime_name, body.anime_type, year=body.year,
    )
    return {"job_id": job_id, "status": "queued"}


# ── Scheduled downloads ────────────────────────────────────────────────────────

@router.post("/schedule/film", status_code=202)
def schedule_film(body: FilmScheduleRequest):
    job_id = job_manager.schedule_film(
        body.id, body.title, body.domain, body.scheduled_at, year=body.year,
    )
    return {"job_id": job_id, "status": "scheduled", "scheduled_at": body.scheduled_at.isoformat()}


@router.post("/schedule/episode", status_code=202)
def schedule_episode(body: EpisodeScheduleRequest):
    if body.ep_index < 0 or body.ep_index >= len(body.eps):
        raise HTTPException(status_code=400, detail="ep_index out of range")
    job_id = job_manager.schedule_episode(
        body.tv_id, body.eps, body.ep_index,
        body.domain, body.token, body.tv_name, body.season,
        body.scheduled_at, year=body.year,
    )
    return {"job_id": job_id, "status": "scheduled", "scheduled_at": body.scheduled_at.isoformat()}


@router.post("/schedule/anime", status_code=202)
def schedule_anime(body: AnimeScheduleRequest):
    job_id = job_manager.schedule_anime_episode(
        body.anime_id, body.episode, body.anime_name, body.scheduled_at,
        anime_type=body.anime_type, year=body.year,
    )
    return {"job_id": job_id, "status": "scheduled", "scheduled_at": body.scheduled_at.isoformat()}


# ── Job management ─────────────────────────────────────────────────────────────

@router.post("/{job_id}/fire", status_code=200)
def fire_now(job_id: str):
    if job_manager.fire_now(job_id):
        return {"job_id": job_id, "status": "queued"}
    raise HTTPException(status_code=404, detail="Job non trovato o non in stato programmato")


@router.delete("/{job_id}", status_code=200)
def cancel_or_dismiss(job_id: str):
    """Cancel a running/queued/scheduled job, or dismiss a finished one (also cleans schedule store)."""
    if job_manager.dismiss(job_id):
        return {"job_id": job_id, "status": "dismissed"}
    if job_manager.cancel(job_id):
        return {"job_id": job_id, "status": "cancelled"}
    raise HTTPException(status_code=404, detail="Job non trovato")
