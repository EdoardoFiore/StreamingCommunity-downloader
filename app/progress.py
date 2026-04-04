import asyncio
import time
from collections import deque
from typing import Callable, Optional


class DownloadCancelledError(Exception):
    pass


class WebProgressBar:
    """Drop-in tqdm replacement that pushes progress events onto an asyncio.Queue."""

    _EMIT_INTERVAL = 0.5  # seconds between progress events
    _SPEED_WINDOW = 5.0   # seconds for rolling speed average

    def __init__(
        self,
        total: int,
        job_queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        phase: str = None,
        on_event: Optional[Callable[[dict], None]] = None,
    ):
        self.total = total
        self.n = 0
        self._queue = job_queue
        self._loop = loop
        self._last_emit = 0.0
        self._phase = phase
        self._on_event = on_event
        self._start_time = time.monotonic()
        self._samples: deque = deque()  # (timestamp, n) for rolling speed

    def _speed_and_eta(self) -> tuple[float, Optional[float]]:
        now = time.monotonic()
        cutoff = now - self._SPEED_WINDOW
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

        if len(self._samples) < 2:
            elapsed = now - self._start_time
            speed = self.n / elapsed if elapsed > 0 else 0.0
        else:
            dt = now - self._samples[0][0]
            dn = self.n - self._samples[0][1]
            speed = dn / dt if dt > 0 else 0.0

        remaining = self.total - self.n
        eta = (remaining / speed) if speed > 0 and remaining > 0 else None
        return round(speed, 1), (round(eta) if eta is not None else None)

    def _push(self):
        speed, eta = self._speed_and_eta()
        pct = round(self.n / self.total * 100, 1) if self.total else 0
        msg = {
            "type": "progress",
            "current": self.n,
            "total": self.total,
            "pct": pct,
            "speed": speed,
            "eta": eta,
        }
        if self._phase:
            msg["phase"] = self._phase
        asyncio.run_coroutine_threadsafe(self._queue.put(msg), self._loop)
        if self._on_event:
            self._on_event(msg)

    def update(self, n=1):
        self.n += n
        now = time.monotonic()
        self._samples.append((now, self.n))
        if self.n >= self.total or now - self._last_emit >= self._EMIT_INTERVAL:
            self._last_emit = now
            self._push()

    def emit_status(self, phase: str):
        msg = {"type": "status", "phase": phase}
        asyncio.run_coroutine_threadsafe(self._queue.put(msg), self._loop)
        if self._on_event:
            self._on_event(msg)

    def close(self):
        pass

    def refresh(self):
        pass
