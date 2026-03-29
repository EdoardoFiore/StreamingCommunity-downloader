import logging

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


@router.delete("/{job_id}", status_code=200)
def cancel_download(job_id: str):
    cancelled = job_manager.cancel(job_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail="Job non trovato o già completato")
    return {"job_id": job_id, "status": "cancelled"}
