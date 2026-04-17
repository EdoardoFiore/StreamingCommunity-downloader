import asyncio
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.config import VIDEOS_DIR, TMP_DIR
from app.progress import DownloadCancelledError, WebProgressBar

logger = logging.getLogger(__name__)

MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_DOWNLOADS", "3"))


@dataclass
class DownloadJob:
    job_id: str
    title: str
    type: str  # "film" | "episode"
    status: str  # "queued" | "running" | "done" | "error" | "cancelled"
    created_at: datetime
    error: Optional[str] = None
    output_path: Optional[str] = None
    progress: dict = field(default_factory=lambda: {"current": 0, "total": 0, "pct": 0, "speed": 0, "eta": None})
    progress_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    cancel_event: threading.Event = field(default_factory=threading.Event)


class JobManager:
    def __init__(self):
        self._jobs: dict[str, DownloadJob] = {}
        self._executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscribers: list[asyncio.Queue] = []

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def get(self, job_id: str) -> Optional[DownloadJob]:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict]:
        return [self._job_to_dict(j) for j in self._jobs.values()]

    def _job_to_dict(self, job: DownloadJob) -> dict:
        return {
            "job_id": job.job_id,
            "title": job.title,
            "type": job.type,
            "status": job.status,
            "created_at": job.created_at.isoformat(),
            "error": job.error,
            "output_path": job.output_path,
            "progress": job.progress,
        }

    # ── Global pub/sub ─────────────────────────────────────────────────────────

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def _broadcast(self, event: dict):
        """Thread-safe push to all global SSE subscribers."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._fanout(event), self._loop)

    async def _fanout(self, event: dict):
        dead = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.unsubscribe(q)

    # ── Progress factory ───────────────────────────────────────────────────────

    def _make_progress_factory(self, job: DownloadJob):
        loop = self._loop
        manager = self

        def on_event(ev: dict):
            if ev.get("type") == "progress":
                job.progress = {
                    "current": ev["current"],
                    "total": ev["total"],
                    "pct": ev["pct"],
                    "speed": ev.get("speed", 0),
                    "eta": ev.get("eta"),
                }
            manager._broadcast({**ev, "job_id": job.job_id})

        def factory(**kwargs):
            total = kwargs.get("total", 0)
            phase = kwargs.get("phase")
            return WebProgressBar(total, job.progress_queue, loop, phase=phase, on_event=on_event)

        return factory

    # ── Job lifecycle ──────────────────────────────────────────────────────────

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return False
        job.cancel_event.set()
        job.status = "cancelled"
        event = {"type": "error", "message": "Annullato"}
        asyncio.run_coroutine_threadsafe(job.progress_queue.put(event), self._loop)
        self._broadcast({**event, "job_id": job_id})
        return True

    def _run_download(self, job: DownloadJob, fn, *args, **kwargs):
        if job.cancel_event.is_set():
            job.status = "cancelled"
            ev = {"type": "error", "message": "Annullato"}
            asyncio.run_coroutine_threadsafe(job.progress_queue.put(ev), self._loop)
            self._broadcast({**ev, "job_id": job.job_id})
            return

        job.status = "running"
        self._broadcast({"type": "job_status", "job_id": job.job_id, "status": "running"})

        try:
            result = fn(*args, **kwargs)
            job.status = "done"
            job.output_path = result
            ev = {"type": "done", "output_path": result}
            asyncio.run_coroutine_threadsafe(job.progress_queue.put(ev), self._loop)
            self._broadcast({**ev, "job_id": job.job_id})
        except DownloadCancelledError:
            job.status = "cancelled"
            ev = {"type": "error", "message": "Annullato"}
            asyncio.run_coroutine_threadsafe(job.progress_queue.put(ev), self._loop)
            self._broadcast({**ev, "job_id": job.job_id})
        except Exception as e:
            logger.exception(f"Job {job.job_id} failed: {e}")
            job.status = "error"
            job.error = str(e)
            ev = {"type": "error", "message": str(e)}
            asyncio.run_coroutine_threadsafe(job.progress_queue.put(ev), self._loop)
            self._broadcast({**ev, "job_id": job.job_id})

    def _submit_job(self, job: DownloadJob, fn, *args, **kwargs) -> str:
        self._jobs[job.job_id] = job
        self._broadcast({"type": "job_created", "job": self._job_to_dict(job)})
        self._executor.submit(self._run_download, job, fn, *args, **kwargs)
        return job.job_id

    def submit_film(self, id_film: int, title: str, domain: str, year: str = None) -> str:
        from app.core.film import download_film

        job_id = str(uuid.uuid4())
        job = DownloadJob(
            job_id=job_id, title=title, type="film",
            status="queued", created_at=datetime.utcnow(),
        )
        return self._submit_job(
            job, download_film,
            id_film, title, domain,
            output_dir=str(VIDEOS_DIR),
            temp_dir=str(TMP_DIR / job_id),
            progress_factory=self._make_progress_factory(job),
            year=year,
            cancel_event=job.cancel_event,
        )

    def submit_episode(
        self,
        tv_id: int,
        eps: list[dict],
        ep_index: int,
        domain: str,
        token: str,
        tv_name: str,
        season: int,
        year: str = None,
    ) -> str:
        from app.core.tv import download_episode

        ep = eps[ep_index]
        title = f"{tv_name} S{season:02d}E{ep['n']:02d}"
        job_id = str(uuid.uuid4())
        job = DownloadJob(
            job_id=job_id, title=title, type="episode",
            status="queued", created_at=datetime.utcnow(),
        )
        return self._submit_job(
            job, download_episode,
            tv_id, eps, ep_index, domain, token, tv_name, season,
            output_dir=str(VIDEOS_DIR),
            temp_dir=str(TMP_DIR / job_id),
            progress_factory=self._make_progress_factory(job),
            cancel_event=job.cancel_event,
            year=year,
        )


    def submit_anime_episode(
        self,
        anime_id: str,
        episode: dict,
        anime_name: str,
        anime_type: str = "tv",
        year: str = None,
    ) -> str:
        from app.core.animeunity import download_anime_episode

        ep_num = episode.get("number", "?")
        title = f"{anime_name} E{ep_num}"
        job_id = str(uuid.uuid4())
        job = DownloadJob(
            job_id=job_id, title=title, type="anime",
            status="queued", created_at=datetime.utcnow(),
        )
        return self._submit_job(
            job, download_anime_episode,
            anime_id, episode, anime_name, anime_type,
            output_dir=str(VIDEOS_DIR),
            temp_dir=str(TMP_DIR / job_id),
            progress_factory=self._make_progress_factory(job),
            cancel_event=job.cancel_event,
            year=year,
        )


job_manager = JobManager()
