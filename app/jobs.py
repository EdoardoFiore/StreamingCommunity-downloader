import asyncio
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.config import VIDEOS_DIR, TMP_DIR
from app.progress import DownloadCancelledError, WebProgressBar

logger = logging.getLogger(__name__)

MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_DOWNLOADS", "3"))
SCHEDULER_INTERVAL = 30  # seconds


@dataclass
class DownloadJob:
    job_id: str
    title: str
    type: str  # "film" | "episode" | "anime"
    status: str  # "scheduled" | "queued" | "running" | "done" | "error" | "cancelled"
    created_at: datetime
    scheduled_at: Optional[datetime] = None
    schedule_id: Optional[str] = None
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
        self._schedule_store = None  # set via set_schedule_store()

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        loop.create_task(self._scheduler_loop())

    def set_schedule_store(self, store):
        from app.schedule import ScheduleStore
        self._schedule_store: Optional[ScheduleStore] = store

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
            "scheduled_at": job.scheduled_at.isoformat() if job.scheduled_at else None,
            "schedule_id": job.schedule_id,
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

    # ── Scheduler loop ─────────────────────────────────────────────────────────

    async def _scheduler_loop(self):
        while True:
            await asyncio.sleep(SCHEDULER_INTERVAL)
            now = datetime.now(timezone.utc)
            for job in list(self._jobs.values()):
                if job.status != "scheduled" or job.scheduled_at is None or job.schedule_id is None:
                    continue
                sa = job.scheduled_at
                if sa.tzinfo is None:
                    sa = sa.replace(tzinfo=timezone.utc)
                if sa > now:
                    continue
                if self._schedule_store is None:
                    continue
                entry = self._schedule_store.get_by_schedule_id(job.schedule_id)
                if entry is None:
                    continue
                logger.info("Scheduler firing job_id=%s schedule_id=%s", job.job_id, job.schedule_id)
                try:
                    self._fire_job(job, entry["type"], entry["params"])
                except Exception as e:
                    logger.error("Failed to fire scheduled job %s: %s", job.job_id, e)

    def _fire_job(self, job: DownloadJob, type_: str, params: dict):
        fn, args, kwargs = self._build_call(type_, params, job)
        job.status = "queued"
        if self._schedule_store is not None and job.schedule_id:
            self._schedule_store.mark_fired(job.schedule_id)
        self._broadcast({"type": "job_status", "job_id": job.job_id, "status": "queued"})
        self._executor.submit(self._run_download, job, fn, *args, **kwargs)

    def _build_call(self, type_: str, params: dict, job: DownloadJob):
        pf = self._make_progress_factory(job)
        td = str(TMP_DIR / job.job_id)
        if type_ == "film":
            from app.core.film import download_film
            return download_film, (params["id"], params["title"], params["domain"]), dict(
                output_dir=str(VIDEOS_DIR), temp_dir=td, progress_factory=pf,
                year=params.get("year"), cancel_event=job.cancel_event,
            )
        if type_ == "episode":
            from app.core.tv import download_episode
            return download_episode, (
                params["tv_id"], params["eps"], params["ep_index"],
                params["domain"], params["token"], params["tv_name"], params["season"],
            ), dict(
                output_dir=str(VIDEOS_DIR), temp_dir=td, progress_factory=pf,
                cancel_event=job.cancel_event, year=params.get("year"),
            )
        if type_ == "anime":
            from app.core.animeunity import download_anime_episode
            return download_anime_episode, (
                params["anime_id"], params["episode"],
                params["anime_name"], params.get("anime_type", "tv"),
            ), dict(
                output_dir=str(VIDEOS_DIR), temp_dir=td, progress_factory=pf,
                cancel_event=job.cancel_event, year=params.get("year"),
            )
        raise ValueError(f"Unknown schedule type: {type_!r}")

    # ── Job lifecycle ──────────────────────────────────────────────────────────

    def fire_now(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status != "scheduled" or job.schedule_id is None:
            return False
        entry = self._schedule_store.get_by_schedule_id(job.schedule_id) if self._schedule_store else None
        if entry is None:
            return False
        self._fire_job(job, entry["type"], entry["params"])
        return True

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status not in ("scheduled", "queued", "running"):
            return False
        job.cancel_event.set()
        job.status = "cancelled"
        event = {"type": "error", "message": "Annullato"}
        asyncio.run_coroutine_threadsafe(job.progress_queue.put(event), self._loop)
        self._broadcast({**event, "job_id": job_id})
        return True

    def dismiss(self, job_id: str) -> bool:
        """Remove a finished/cancelled job and clean it from the schedule store."""
        job = self._jobs.get(job_id)
        if not job or job.status not in ("done", "error", "cancelled"):
            return False
        del self._jobs[job_id]
        if self._schedule_store is not None:
            self._schedule_store.remove_by_job_id(job_id)
        self._broadcast({"type": "job_dismissed", "job_id": job_id})
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

    def _make_job(self, title: str, type_: str, scheduled_at: Optional[datetime] = None,
                  schedule_id: Optional[str] = None) -> DownloadJob:
        now = datetime.now(timezone.utc)
        sa = scheduled_at.replace(tzinfo=timezone.utc) if scheduled_at and scheduled_at.tzinfo is None else scheduled_at
        status = "scheduled" if sa and sa > now else "queued"
        return DownloadJob(
            job_id=str(uuid.uuid4()),
            title=title,
            type=type_,
            status=status,
            created_at=datetime.utcnow(),
            scheduled_at=scheduled_at,
            schedule_id=schedule_id,
        )

    # ── Submit (immediate) ─────────────────────────────────────────────────────

    def submit_film(self, id_film: int, title: str, domain: str, year: str = None,
                    schedule_id: str = None) -> str:
        from app.core.film import download_film

        job = self._make_job(title, "film", schedule_id=schedule_id)
        return self._submit_job(
            job, download_film,
            id_film, title, domain,
            output_dir=str(VIDEOS_DIR),
            temp_dir=str(TMP_DIR / job.job_id),
            progress_factory=self._make_progress_factory(job),
            year=year,
            cancel_event=job.cancel_event,
        )

    def submit_episode(self, tv_id: int, eps: list[dict], ep_index: int, domain: str,
                       token: str, tv_name: str, season: int, year: str = None,
                       schedule_id: str = None) -> str:
        from app.core.tv import download_episode

        ep = eps[ep_index]
        title = f"{tv_name} S{season:02d}E{ep['n']:02d}"
        job = self._make_job(title, "episode", schedule_id=schedule_id)
        return self._submit_job(
            job, download_episode,
            tv_id, eps, ep_index, domain, token, tv_name, season,
            output_dir=str(VIDEOS_DIR),
            temp_dir=str(TMP_DIR / job.job_id),
            progress_factory=self._make_progress_factory(job),
            cancel_event=job.cancel_event,
            year=year,
        )

    def submit_anime_episode(self, anime_id: str, episode: dict, anime_name: str,
                             anime_type: str = "tv", year: str = None,
                             schedule_id: str = None) -> str:
        from app.core.animeunity import download_anime_episode

        ep_num = episode.get("number", "?")
        title = f"{anime_name} E{ep_num}"
        job = self._make_job(title, "anime", schedule_id=schedule_id)
        return self._submit_job(
            job, download_anime_episode,
            anime_id, episode, anime_name, anime_type,
            output_dir=str(VIDEOS_DIR),
            temp_dir=str(TMP_DIR / job.job_id),
            progress_factory=self._make_progress_factory(job),
            cancel_event=job.cancel_event,
            year=year,
        )

    # ── Schedule (future) ──────────────────────────────────────────────────────

    def schedule_film(self, id_film: int, title: str, domain: str,
                      scheduled_at: datetime, year: str = None) -> str:
        params = {"id": id_film, "title": title, "domain": domain, "year": year}
        return self._add_schedule("film", scheduled_at, params, title)

    def schedule_episode(self, tv_id: int, eps: list[dict], ep_index: int, domain: str,
                         token: str, tv_name: str, season: int,
                         scheduled_at: datetime, year: str = None) -> str:
        ep = eps[ep_index]
        title = f"{tv_name} S{season:02d}E{ep['n']:02d}"
        params = {
            "tv_id": tv_id, "eps": eps, "ep_index": ep_index,
            "domain": domain, "token": token, "tv_name": tv_name,
            "season": season, "year": year,
        }
        return self._add_schedule("episode", scheduled_at, params, title)

    def schedule_anime_episode(self, anime_id: str, episode: dict, anime_name: str,
                               scheduled_at: datetime, anime_type: str = "tv",
                               year: str = None) -> str:
        ep_num = episode.get("number", "?")
        title = f"{anime_name} E{ep_num}"
        params = {
            "anime_id": anime_id, "episode": episode, "anime_name": anime_name,
            "anime_type": anime_type, "year": year,
        }
        return self._add_schedule("anime", scheduled_at, params, title)

    def _add_schedule(self, type_: str, scheduled_at: datetime, params: dict, title: str) -> str:
        if self._schedule_store is None:
            raise RuntimeError("ScheduleStore not configured")
        schedule_id = self._schedule_store.add(type_, scheduled_at, params)
        job = self._make_job(title, type_, scheduled_at=scheduled_at, schedule_id=schedule_id)
        self._jobs[job.job_id] = job
        self._schedule_store.set_job_id(schedule_id, job.job_id)
        self._broadcast({"type": "job_created", "job": self._job_to_dict(job)})
        return job.job_id

    def load_scheduled_from_store(self):
        """Re-hydrate pending scheduled entries from the JSON store on startup."""
        if self._schedule_store is None:
            return
        for entry in self._schedule_store.list_all():
            if entry.get("fired"):
                continue  # already dispatched in a previous session — skip
            sid = entry["schedule_id"]
            type_ = entry["type"]
            params = entry["params"]
            sa_raw = datetime.fromisoformat(entry["scheduled_at"])
            scheduled_at = sa_raw if sa_raw.tzinfo else sa_raw.replace(tzinfo=timezone.utc)
            title = params.get("title") or params.get("tv_name") or params.get("anime_name", "?")
            job = self._make_job(title, type_, scheduled_at=scheduled_at, schedule_id=sid)
            self._jobs[job.job_id] = job
            self._schedule_store.set_job_id(sid, job.job_id)
            logger.info("Restored scheduled job %s (schedule_id=%s) for %s", job.job_id, sid, scheduled_at)


job_manager = JobManager()
