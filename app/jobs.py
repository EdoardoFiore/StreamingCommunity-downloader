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


@dataclass
class DownloadJob:
    job_id: str
    title: str
    type: str  # "film" | "episode"
    status: str  # "queued" | "running" | "done" | "error"
    created_at: datetime
    error: Optional[str] = None
    output_path: Optional[str] = None
    progress: dict = field(default_factory=lambda: {"current": 0, "total": 0, "pct": 0})
    progress_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    cancel_event: threading.Event = field(default_factory=threading.Event)


class JobManager:
    def __init__(self):
        self._jobs: dict[str, DownloadJob] = {}
        self._executor = ThreadPoolExecutor(max_workers=3)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

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

    def _make_progress_factory(self, job: DownloadJob):
        loop = self._loop

        def factory(**kwargs):
            total = kwargs.get("total", 0)
            return WebProgressBar(total, job.progress_queue, loop)

        return factory

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return False
        job.cancel_event.set()
        if job.status == "queued":
            job.status = "cancelled"
            asyncio.run_coroutine_threadsafe(
                job.progress_queue.put({"type": "error", "message": "Annullato"}),
                self._loop,
            )
        return True

    def _run_download(self, job: DownloadJob, fn, *args, **kwargs):
        if job.cancel_event.is_set():
            job.status = "cancelled"
            asyncio.run_coroutine_threadsafe(
                job.progress_queue.put({"type": "error", "message": "Annullato"}), self._loop
            )
            return
        job.status = "running"
        try:
            result = fn(*args, **kwargs)
            job.status = "done"
            job.output_path = result
            asyncio.run_coroutine_threadsafe(
                job.progress_queue.put({"type": "done", "output_path": result}),
                self._loop,
            )
        except DownloadCancelledError:
            job.status = "cancelled"
            asyncio.run_coroutine_threadsafe(
                job.progress_queue.put({"type": "error", "message": "Annullato"}),
                self._loop,
            )
        except Exception as e:
            logger.exception(f"Job {job.job_id} failed: {e}")
            job.status = "error"
            job.error = str(e)
            asyncio.run_coroutine_threadsafe(
                job.progress_queue.put({"type": "error", "message": str(e)}),
                self._loop,
            )

    def submit_film(self, id_film: int, title: str, domain: str, year: str = None) -> str:
        from app.core.film import download_film

        job_id = str(uuid.uuid4())
        job = DownloadJob(
            job_id=job_id,
            title=title,
            type="film",
            status="queued",
            created_at=datetime.utcnow(),
        )
        self._jobs[job_id] = job

        temp_dir = str(TMP_DIR / job_id)
        output_dir = str(VIDEOS_DIR)
        progress_factory = self._make_progress_factory(job)

        self._executor.submit(
            self._run_download,
            job,
            download_film,
            id_film, title, domain,
            output_dir=output_dir,
            temp_dir=temp_dir,
            progress_factory=progress_factory,
            year=year,
            cancel_event=job.cancel_event,
        )
        return job_id

    def submit_episode(
        self,
        tv_id: int,
        eps: list[dict],
        ep_index: int,
        domain: str,
        token: str,
        tv_name: str,
        season: int,
    ) -> str:
        from app.core.tv import download_episode

        ep = eps[ep_index]
        title = f"{tv_name} S{season:02d}E{ep['n']:02d}"

        job_id = str(uuid.uuid4())
        job = DownloadJob(
            job_id=job_id,
            title=title,
            type="episode",
            status="queued",
            created_at=datetime.utcnow(),
        )
        self._jobs[job_id] = job

        temp_dir = str(TMP_DIR / job_id)
        output_dir = str(VIDEOS_DIR)
        progress_factory = self._make_progress_factory(job)

        self._executor.submit(
            self._run_download,
            job,
            download_episode,
            tv_id, eps, ep_index, domain, token, tv_name, season,
            output_dir=output_dir,
            temp_dir=temp_dir,
            progress_factory=progress_factory,
            cancel_event=job.cancel_event,
        )
        return job_id


job_manager = JobManager()
