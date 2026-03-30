import asyncio
import time


class DownloadCancelledError(Exception):
    pass


class WebProgressBar:
    """Drop-in tqdm replacement that pushes progress events onto an asyncio.Queue."""

    _EMIT_INTERVAL = 0.5  # seconds between progress events

    def __init__(self, total: int, job_queue: asyncio.Queue, loop: asyncio.AbstractEventLoop, phase: str = None):
        self.total = total
        self.n = 0
        self._queue = job_queue
        self._loop = loop
        self._last_emit = 0.0
        self._phase = phase

    def _push(self):
        pct = round(self.n / self.total * 100, 1) if self.total else 0
        msg = {"type": "progress", "current": self.n, "total": self.total, "pct": pct}
        if self._phase:
            msg["phase"] = self._phase
        asyncio.run_coroutine_threadsafe(self._queue.put(msg), self._loop)

    def update(self, n=1):
        self.n += n
        now = time.monotonic()
        # Always emit on completion; otherwise throttle to _EMIT_INTERVAL
        if self.n >= self.total or now - self._last_emit >= self._EMIT_INTERVAL:
            self._last_emit = now
            self._push()

    def emit_status(self, phase: str):
        asyncio.run_coroutine_threadsafe(
            self._queue.put({"type": "status", "phase": phase}), self._loop
        )

    def close(self):
        pass

    def refresh(self):
        pass
