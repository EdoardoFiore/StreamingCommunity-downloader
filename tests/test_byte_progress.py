"""How many bytes a download has pulled down, and roughly how many it will.

The segment count was always reported; the byte tally was accumulated and then
thrown away. What makes this safe to add is that it rides on the *existing*
update() calls in m3u8.py, both of which pass n=1 alongside the bytes - so
bytes only accrue on a segment actually obtained, exactly like the count the
stall watchdog reads.
"""

import asyncio
import threading

import pytest

from app.progress import WebProgressBar


@pytest.fixture
def loop():
    """A real event loop on its own thread: the bar pushes frames into it."""
    made = asyncio.new_event_loop()
    thread = threading.Thread(target=made.run_forever, daemon=True)
    thread.start()
    yield made
    made.call_soon_threadsafe(made.stop)
    thread.join(timeout=5)


def _bar(loop, total, **kw):
    events = []
    bar = WebProgressBar(total, asyncio.Queue(), loop, on_event=events.append, **kw)
    return bar, events


# ── The counter itself ─────────────────────────────────────────────────────────

def test_parallel_segment_threads_do_not_lose_updates(loop):
    """`self.n += n` is a read-modify-write, and up to max_segment_workers
    threads run it at once. A segment count off by one is invisible; a byte
    figure drifting below its own total is what a reader notices."""
    bar, _ = _bar(loop, total=16_000)

    def worker():
        for _ in range(1000):
            bar.update(1, bytes=10)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert bar.n == 16_000
    assert bar.bytes_done == 160_000


# ── The estimate ───────────────────────────────────────────────────────────────

def test_no_total_is_reported_from_too_small_a_sample(loop):
    """A size extrapolated from three segments is worse than no size."""
    bar, events = _bar(loop, total=1000)

    for _ in range(5):
        bar.update(1, bytes=1000)
    bar._push()

    assert events[-1]["bytes_done"] == 5000
    assert events[-1]["bytes_total"] is None


def test_the_total_is_extrapolated_once_the_sample_is_worth_it(loop):
    bar, events = _bar(loop, total=1000)

    for _ in range(100):
        bar.update(1, bytes=1000)
    bar._push()

    assert events[-1]["bytes_done"] == 100_000
    assert events[-1]["bytes_total"] == 1_000_000       # 1000 B x 1000 segments
    assert events[-1]["bytes_total_estimated"] is True


def test_the_percentage_still_comes_from_segments_not_bytes(loop):
    """The bar must agree with the number the stall watchdog reads. A bar that
    disagrees with the timeout is worse than a slightly non-linear one."""
    bar, events = _bar(loop, total=200)

    for i in range(100):
        # Wildly uneven segments: bytes and segments must not track each other.
        bar.update(1, bytes=1 if i % 2 else 100_000)
    bar._push()

    assert events[-1]["pct"] == 50.0
    assert events[-1]["current"] == 100


# ── Across the phases of one job ───────────────────────────────────────────────

def test_the_tally_carries_across_phases(loop, monkeypatch):
    """One bar per phase, one running total per job.

    Without this the readout drops to zero when the first audio track starts,
    which reads as a download that lost everything it had done.
    """
    from app.jobs import job_manager

    # The factory hands the bar the manager's loop; nothing has started one
    # here, and the bar pushes every frame into it.
    monkeypatch.setattr(job_manager, "_loop", loop)

    job = job_manager._make_job("Film", "film")
    factory = job_manager._make_progress_factory(job)

    video = factory(total=100, phase="video")
    for _ in range(100):
        video.update(1, bytes=1000)

    audio = factory(total=50, phase="audio_ita")
    for _ in range(50):
        audio.update(1, bytes=200)

    assert video.bytes_done == 100_000
    # The second phase reports the job, not just itself.
    assert audio.bytes_done == 100_000 + 10_000
    assert job.progress["bytes_done"] == 110_000
