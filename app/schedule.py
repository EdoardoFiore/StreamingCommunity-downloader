import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ScheduleStore:
    """JSON-backed store for scheduled download entries.

    Each entry:
      {
        "schedule_id": str,
        "type": "film" | "episode" | "anime",
        "scheduled_at": ISO-8601 str,
        "job_id": str | null,   # set once the job is submitted
        "params": { ... }       # type-specific download params
      }
    """

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self._load()

    # ── persistence ────────────────────────────────────────────────────────────

    def _load(self):
        if self._path.exists():
            try:
                self._entries = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("Could not load schedule file: %s", e)
                self._entries = []

    def _save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._entries, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("Could not save schedule file: %s", e)

    # ── public API ─────────────────────────────────────────────────────────────

    def add(self, type_: str, scheduled_at: datetime, params: dict) -> str:
        schedule_id = str(uuid.uuid4())
        entry = {
            "schedule_id": schedule_id,
            "type": type_,
            "scheduled_at": scheduled_at.isoformat(),
            "job_id": None,
            "fired": False,
            "params": params,
        }
        with self._lock:
            self._entries.append(entry)
            self._save()
        return schedule_id

    def set_job_id(self, schedule_id: str, job_id: str):
        with self._lock:
            for entry in self._entries:
                if entry["schedule_id"] == schedule_id:
                    entry["job_id"] = job_id
                    self._save()
                    return

    def mark_fired(self, schedule_id: str):
        with self._lock:
            for entry in self._entries:
                if entry["schedule_id"] == schedule_id:
                    entry["fired"] = True
                    self._save()
                    return

    def remove_by_schedule_id(self, schedule_id: str) -> bool:
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e["schedule_id"] != schedule_id]
            if len(self._entries) < before:
                self._save()
                return True
        return False

    def remove_by_job_id(self, job_id: str) -> bool:
        """Called when a completed/cancelled job is dismissed from the UI."""
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.get("job_id") != job_id]
            if len(self._entries) < before:
                self._save()
                return True
        return False

    def list_all(self) -> list[dict]:
        with self._lock:
            return list(self._entries)

    def due(self) -> list[dict]:
        """Return entries whose scheduled_at is in the past and have no job_id yet."""
        from datetime import timezone
        now = datetime.now(timezone.utc)
        with self._lock:
            result = []
            for e in self._entries:
                if e["job_id"] is not None:
                    continue
                sa = datetime.fromisoformat(e["scheduled_at"])
                if sa.tzinfo is None:
                    sa = sa.replace(tzinfo=timezone.utc)
                if sa <= now:
                    result.append(e)
            return result

    def get_by_schedule_id(self, schedule_id: str) -> Optional[dict]:
        with self._lock:
            return next(
                (e for e in self._entries if e["schedule_id"] == schedule_id), None
            )
