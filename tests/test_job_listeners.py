"""Terminal-state listeners: every job created must reach them exactly once.

Anything counting completions (the direct-download batch summaries) relies on
this. A job that quietly skips its listeners leaves such a counter waiting for a
job that will never report.
"""

import threading
import time

import pytest

from app.jobs import JobManager


@pytest.fixture
def manager():
    jm = JobManager()
    jm.update_max_concurrent(2)
    return jm


def _collect(manager) -> list:
    seen = []
    manager.add_listener(seen.append)
    return seen


def _wait_for(predicate, timeout: float = 5.0):
    """Poll until the worker threads have caught up, or fail loudly."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_a_finished_job_reaches_the_listeners(manager):
    seen = _collect(manager)

    job_id = manager._submit_job(manager._make_job("Film", "film"), lambda: "/videos/f.mp4")

    assert _wait_for(lambda: seen), "listener never fired"
    assert [j.job_id for j in seen] == [job_id]
    assert seen[0].status == "done"


def test_a_failed_job_reaches_the_listeners(manager):
    seen = _collect(manager)

    def explode():
        raise RuntimeError("HTTP 403")

    manager._submit_job(manager._make_job("Film", "film"), explode)

    assert _wait_for(lambda: seen)
    assert seen[0].status == "error"
    assert seen[0].error == "HTTP 403"


def test_a_job_cancelled_before_it_starts_still_reaches_the_listeners(manager):
    """The regression: this used to return before the finally and notify nobody."""
    manager.update_max_concurrent(1)
    seen = _collect(manager)
    release = threading.Event()

    # Occupy the only slot so the second job is still waiting when it is cancelled.
    manager._submit_job(manager._make_job("Blocca", "film"), lambda: release.wait(5) and "/x")
    victim = manager._make_job("Vittima", "film")
    manager._submit_job(victim, lambda: "/videos/never.mp4")

    manager.cancel(victim.job_id)
    release.set()

    assert _wait_for(lambda: len(seen) == 2), f"solo {len(seen)} listener chiamati"
    cancelled = [j for j in seen if j.job_id == victim.job_id]
    assert cancelled and cancelled[0].status == "cancelled"


def test_a_scheduled_job_cancelled_before_firing_reaches_the_listeners(manager, tmp_path):
    """A scheduled job never enters _run_download, so cancel() must notify."""
    from datetime import datetime, timedelta, timezone

    from app.schedule import ScheduleStore

    manager.set_schedule_store(ScheduleStore(tmp_path / "schedule.json"))
    seen = _collect(manager)
    later = datetime.now(timezone.utc) + timedelta(days=1)
    job_id = manager.schedule_film(1, "Film", "example.test", later)

    assert manager.cancel(job_id) is True

    assert [j.job_id for j in seen] == [job_id]
    assert seen[0].status == "cancelled"


def test_a_listener_does_not_hold_a_download_slot(manager):
    """A slow listener must not stop the next download from starting.

    Notification channels do blocking network I/O in a listener, so running them
    inside the concurrency semaphore would throttle downloads to the speed of
    the slowest webhook.
    """
    manager.update_max_concurrent(1)
    listener_entered = threading.Event()
    release_listener = threading.Event()
    second_started = threading.Event()

    def slow_listener(job):
        if job.title == "Primo":
            listener_entered.set()
            release_listener.wait(5)

    manager.add_listener(slow_listener)
    manager._submit_job(manager._make_job("Primo", "film"), lambda: "/videos/1.mp4")
    assert listener_entered.wait(5), "il primo job non ha raggiunto il listener"

    manager._submit_job(
        manager._make_job("Secondo", "film"),
        lambda: (second_started.set(), "/videos/2.mp4")[1],
    )

    started = second_started.wait(5)
    release_listener.set()
    assert started, "il secondo download ha aspettato il listener del primo"


def test_a_raising_listener_does_not_stop_the_others(manager):
    seen = []

    def explode(job):
        raise RuntimeError("canale morto")

    manager.add_listener(explode)
    manager.add_listener(seen.append)

    manager._submit_job(manager._make_job("Film", "film"), lambda: "/videos/f.mp4")

    assert _wait_for(lambda: seen)
