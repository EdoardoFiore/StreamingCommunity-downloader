import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.jobs import job_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["progress"])


@router.get("/jobs")
def list_jobs():
    return job_manager.list_jobs()


@router.get("/progress/stream")
async def stream_all_progress():
    """Global SSE stream: sends a snapshot of all jobs, then streams all events."""
    q = job_manager.subscribe()

    async def event_generator():
        try:
            snapshot = {"type": "snapshot", "jobs": job_manager.list_jobs()}
            yield f"data: {json.dumps(snapshot)}\n\n"

            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            job_manager.unsubscribe(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/progress/{job_id}")
async def stream_progress(job_id: str):
    """Per-job SSE (kept for backward compatibility)."""
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        if job.status in ("done", "error", "cancelled"):
            msg: dict = {"type": job.status if job.status != "cancelled" else "error"}
            if job.output_path:
                msg["output_path"] = job.output_path
            if job.error:
                msg["message"] = job.error
            yield f"data: {json.dumps(msg)}\n\n"
            return

        while True:
            try:
                msg = await asyncio.wait_for(job.progress_queue.get(), timeout=30)
                if msg.get("type") == "progress":
                    job.progress = {
                        "current": msg["current"],
                        "total": msg["total"],
                        "pct": msg["pct"],
                        "speed": msg.get("speed", 0),
                        "eta": msg.get("eta"),
                    }
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("type") in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
