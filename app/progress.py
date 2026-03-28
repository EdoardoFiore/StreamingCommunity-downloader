import asyncio


class DownloadCancelledError(Exception):
    pass


class WebProgressBar:
    """Drop-in tqdm replacement that pushes progress events onto an asyncio.Queue."""

    def __init__(self, total: int, job_queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        self.total = total
        self.n = 0
        self._queue = job_queue
        self._loop = loop

    def update(self, n=1):
        self.n += n
        pct = round(self.n / self.total * 100, 1) if self.total else 0
        msg = {"type": "progress", "current": self.n, "total": self.total, "pct": pct}
        asyncio.run_coroutine_threadsafe(self._queue.put(msg), self._loop)

    def emit_status(self, phase: str):
        asyncio.run_coroutine_threadsafe(
            self._queue.put({"type": "status", "phase": phase}), self._loop
        )

    def close(self):
        pass

    def refresh(self):
        pass
