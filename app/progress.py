import asyncio
import threading
import time
from collections import deque
from typing import Callable, Optional


class DownloadCancelledError(Exception):
    pass


class WebProgressBar:
    """Drop-in tqdm replacement that pushes progress events onto an asyncio.Queue."""

    _EMIT_INTERVAL = 0.5  # seconds between progress events
    _SPEED_WINDOW = 5.0   # seconds for rolling speed average

    # Below this many segments the byte total is extrapolated from too small a
    # sample to mean anything, and a wrong size on screen is worse than none.
    _MIN_SAMPLE = 20

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
        self._bytes = 0
        self._queue = job_queue
        self._loop = loop
        self._last_emit = 0.0
        self._phase = phase
        self._on_event = on_event
        self._start_time = time.monotonic()
        self._samples: deque = deque()       # (timestamp, n_segs) for ETA
        self._byte_samples: deque = deque()  # (timestamp, n_bytes) for display speed

        # update() runs on every segment thread at once - up to
        # max_segment_workers of them. ``self.n += n`` is a read-modify-write
        # and loses updates under contention; a segment count off by one is
        # invisible, but a byte figure drifting below its own total is exactly
        # what a reader notices.
        self._lock = threading.Lock()

        # A bar covers one phase. The job spans several - video, then one per
        # audio track - and each gets a fresh bar. Without this the readout
        # would drop back to zero the moment the first audio track starts,
        # which reads as a download that lost its progress. Set by the factory
        # in app.jobs, which is per job and therefore the only place that can
        # see across phases.
        self.prior_bytes = 0

    def _speed_and_eta(self) -> tuple[float, Optional[float], float]:
        now = time.monotonic()
        cutoff = now - self._SPEED_WINDOW

        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        if len(self._samples) < 2:
            elapsed = now - self._start_time
            seg_speed = self.n / elapsed if elapsed > 0 else 0.0
        else:
            dt = now - self._samples[0][0]
            seg_speed = (self.n - self._samples[0][1]) / dt if dt > 0 else 0.0

        while self._byte_samples and self._byte_samples[0][0] < cutoff:
            self._byte_samples.popleft()
        if len(self._byte_samples) < 2:
            elapsed = now - self._start_time
            bytes_speed = self._bytes / elapsed if elapsed > 0 else 0.0
        else:
            dt = now - self._byte_samples[0][0]
            bytes_speed = (self._bytes - self._byte_samples[0][1]) / dt if dt > 0 else 0.0

        remaining = self.total - self.n
        eta = (remaining / seg_speed) if seg_speed > 0 and remaining > 0 else None
        return round(seg_speed, 1), (round(eta) if eta is not None else None), round(bytes_speed)

    @property
    def bytes_done(self) -> int:
        """Everything the job has pulled down: this phase and the ones before."""
        with self._lock:
            return self.prior_bytes + self._bytes

    def _estimated_total_bytes(self) -> Optional[int]:
        """What the whole phase will weigh, extrapolated from what it weighs so far.

        There is no honest exact answer: a playlist declares no size, and the
        only ways to get one are a HEAD per segment (thousands of extra
        requests against a source that already answers 503) or an
        EXT-X-BYTERANGE these playlists do not carry. So this is a running
        estimate, and it is reported as one - never as a number to compare
        against for completeness.
        """
        if not self.total or self.n < max(self._MIN_SAMPLE, self.total * 0.02):
            return None
        return int(self._bytes / self.n * self.total)

    def _build_message(self) -> dict:
        """The progress frame. Caller holds the lock: this trims the sample
        deques, and it must see n and _bytes agreeing with each other."""
        seg_speed, eta, bytes_speed = self._speed_and_eta()
        pct = round(self.n / self.total * 100, 1) if self.total else 0
        estimate = self._estimated_total_bytes()
        msg = {
            "type": "progress",
            "current": self.n,
            "total": self.total,
            "pct": pct,
            "speed": seg_speed,
            "bytes_speed": bytes_speed,
            "eta": eta,
            "bytes_done": self.prior_bytes + self._bytes,
            "bytes_total": None if estimate is None else self.prior_bytes + estimate,
            "bytes_total_estimated": True,
        }
        if self._phase:
            msg["phase"] = self._phase
        return msg

    def _emit(self, msg: dict):
        asyncio.run_coroutine_threadsafe(self._queue.put(msg), self._loop)
        if self._on_event:
            self._on_event(msg)

    def _push(self):
        with self._lock:
            msg = self._build_message()
        self._emit(msg)

    def update(self, n=1, bytes=0):
        with self._lock:
            self.n += n
            self._bytes += bytes
            now = time.monotonic()
            self._samples.append((now, self.n))
            self._byte_samples.append((now, self._bytes))
            if not (self.n >= self.total or now - self._last_emit >= self._EMIT_INTERVAL):
                return
            self._last_emit = now
            msg = self._build_message()
        # Sent with the lock released: _emit hands the frame to the event loop
        # and calls on_event, which writes job.progress and fans out to every
        # SSE subscriber. Holding a segment thread's lock across that would
        # serialise the download behind the slowest browser watching it.
        self._emit(msg)

    def emit_status(self, phase: str):
        msg = {"type": "status", "phase": phase}
        asyncio.run_coroutine_threadsafe(self._queue.put(msg), self._loop)
        if self._on_event:
            self._on_event(msg)

    def close(self):
        pass

    def refresh(self):
        pass
