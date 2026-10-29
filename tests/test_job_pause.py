"""Pausing a batch holds back what has not started; what runs finishes (#43).

A job mid-download is never paused: its stream token was issued when it started
and would not outlive a long pause. A job still waiting asks for its own when
it starts, so holding it back costs nothing - as long as it gives up its
executor thread meanwhile, or a long paused series would starve every other
download of the 64 threads.
"""

import threading
import time

from app.jobs import JobManager


def _wait_for(predicate, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _batch(jm, n, fn, batch_id="b1"):
    jobs = [jm._make_job(f"t{i}", "episode", batch_id=batch_id) for i in range(n)]
    for i, job in enumerate(jobs):
        jm._submit_job(job, fn, f"t{i}")
    return jobs


def test_the_running_job_finishes_and_the_rest_wait():
    jm = JobManager()
    jm.update_max_concurrent(1)
    gate, started = threading.Event(), []

    def fake_download(name, **kwargs):
        started.append(name)
        gate.wait(timeout=2)
        return "ok"

    try:
        jobs = _batch(jm, 4, fake_download)
        assert _wait_for(lambda: sum(j.status == "running" for j in jobs) == 1)

        assert jm.pause_batch("b1") == 3
        gate.set()
        running = next(j for j in jobs if j.status in ("running", "done"))
        assert _wait_for(lambda: running.status == "done")
        assert _wait_for(lambda: all(j.parked for j in jobs if j is not running))
        time.sleep(0.1)
        assert len(started) == 1
        assert {j.status for j in jobs if j is not running} == {"paused"}
    finally:
        gate.set()
        jm._executor.shutdown(wait=False)


def test_resuming_runs_every_paused_job():
    jm = JobManager()
    jm.update_max_concurrent(1)
    gate = threading.Event()

    def fake_download(name, **kwargs):
        gate.wait(timeout=2)
        return "ok"

    try:
        jobs = _batch(jm, 3, fake_download)
        assert _wait_for(lambda: any(j.status == "running" for j in jobs))
        jm.pause_batch("b1")
        gate.set()
        assert _wait_for(lambda: sum(j.parked is not None for j in jobs) == 2)

        assert jm.resume_batch("b1") == 2
        assert _wait_for(lambda: all(j.status == "done" for j in jobs))
    finally:
        gate.set()
        jm._executor.shutdown(wait=False)


def test_a_paused_job_holds_no_thread():
    """Parked jobs leave the executor: another download still gets one."""
    jm = JobManager()
    jm.update_max_concurrent(1)
    gate = threading.Event()

    def fake_download(name, **kwargs):
        gate.wait(timeout=2)
        return "ok"

    try:
        jobs = _batch(jm, 3, fake_download)
        assert _wait_for(lambda: any(j.status == "running" for j in jobs))
        jm.pause_batch("b1")
        gate.set()
        assert _wait_for(lambda: sum(j.parked is not None for j in jobs) == 2)
        assert _wait_for(lambda: jm._executor._work_queue.qsize() == 0)

        other = jm._make_job("other", "film")
        jm._submit_job(other, fake_download, "other")
        assert _wait_for(lambda: other.status == "done")
    finally:
        gate.set()
        jm._executor.shutdown(wait=False)


def test_pausing_leaves_other_batches_alone():
    jm = JobManager()
    jm.update_max_concurrent(1)
    gate = threading.Event()

    def fake_download(name, **kwargs):
        gate.wait(timeout=2)
        return "ok"

    try:
        first = _batch(jm, 2, fake_download, "b1")
        second = _batch(jm, 2, fake_download, "b2")
        assert _wait_for(lambda: any(j.status == "running" for j in first))
        jm.pause_batch("b2")
        gate.set()
        assert _wait_for(lambda: all(j.status == "done" for j in first))
        assert {j.status for j in second} == {"paused"}
    finally:
        gate.set()
        jm._executor.shutdown(wait=False)


def test_a_paused_job_cancelled_reaches_listeners_once():
    """It has left the executor, so _run_download will not notify for it -
    the batch would never close if cancel() did not."""
    jm = JobManager()
    jm.update_max_concurrent(1)
    gate, heard = threading.Event(), []
    jm.add_listener(lambda job: heard.append((job.job_id, job.status)))

    def fake_download(name, **kwargs):
        gate.wait(timeout=2)
        return "ok"

    try:
        jobs = _batch(jm, 2, fake_download)
        assert _wait_for(lambda: jobs[0].status == "running")
        jm.pause_batch("b1")
        assert _wait_for(lambda: jobs[1].parked is not None)

        assert jm.cancel(jobs[1].job_id)
        gate.set()
        assert _wait_for(lambda: jobs[0].status == "done")
        time.sleep(0.05)
        assert sorted(heard) == sorted([(jobs[0].job_id, "done"), (jobs[1].job_id, "cancelled")])
        assert jm.resume_batch("b1") == 0
    finally:
        gate.set()
        jm._executor.shutdown(wait=False)


def test_a_batch_with_nothing_waiting_has_nothing_to_pause():
    jm = JobManager()
    try:
        assert jm.pause_batch("nope") == 0
        assert jm.resume_batch("nope") == 0
    finally:
        jm._executor.shutdown(wait=False)
