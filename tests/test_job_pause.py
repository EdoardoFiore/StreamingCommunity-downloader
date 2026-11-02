"""Pausing a batch holds back what has not started; what runs finishes (#43).

A job mid-download is never paused: its stream token was issued when it started
and would not outlive a long pause. A job still waiting asks for its own when
it starts, so holding it back costs nothing - as long as it gives up its
executor thread meanwhile, or a long paused series would starve every other
download of the 64 threads.
"""

import os
import threading
import time

from app import jobs as jobs_module
from app.jobs import JobManager
from app.progress import DownloadCancelledError


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



def _cancellable(started, gate, library=None, instant=()):
    """A fake download that stops the way a real one does when cancelled, and,
    given a library, writes an episode into it as a real one would. The names
    in ``instant`` finish without waiting for the gate."""
    def fake_download(name, cancel_event=None, **kwargs):
        started.append(name)
        while name not in instant and not gate.wait(0.01):
            if cancel_event.is_set():
                raise DownloadCancelledError("Download annullato dall'utente")
        if library is None:
            return "ok"
        folder = library / "Serie (2020)" / "Season 01"
        folder.mkdir(parents=True, exist_ok=True)
        video = folder / f"Serie {name}.mkv"
        video.write_bytes(b"x")
        (folder / f"Serie {name}.ita.vtt").write_text("WEBVTT")
        return str(video)
    return fake_download


def _cancellable_batch(jm, n, fn, batch_id="b1"):
    jobs = [jm._make_job(f"t{i}", "episode", batch_id=batch_id) for i in range(n)]
    for i, job in enumerate(jobs):
        jm._submit_job(job, fn, f"t{i}", cancel_event=job.cancel_event)
    return jobs


def test_cancelling_a_paused_batch_stops_everything_left_once():
    """The running job and the parked ones alike, each heard exactly once -
    and no sibling starts in the slot the running one frees."""
    jm = JobManager()
    jm.update_max_concurrent(1)
    gate, started, heard = threading.Event(), [], []
    jm.add_listener(lambda job: heard.append((job.job_id, job.status)))

    try:
        jobs = _cancellable_batch(jm, 4, _cancellable(started, gate))
        assert _wait_for(lambda: jobs[0].status == "running")
        jm.pause_batch("b1")
        assert _wait_for(lambda: all(j.parked for j in jobs[1:]))

        assert jm.cancel_batch("b1") == (4, 0)
        assert _wait_for(lambda: len(heard) == 4)
        time.sleep(0.05)
        assert started == ["t0"]
        assert {j.status for j in jobs} == {"cancelled"}
        assert sorted(heard) == sorted((j.job_id, "cancelled") for j in jobs)
    finally:
        gate.set()
        jm._executor.shutdown(wait=False)


def test_cancelling_a_batch_leaves_other_batches_and_finished_jobs_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "_library_roots", lambda: [os.path.realpath(tmp_path)])
    jm = JobManager()
    jm.update_max_concurrent(1)
    gate, started = threading.Event(), []

    try:
        gate.set()
        first = _cancellable_batch(jm, 2, _cancellable(started, gate, tmp_path), "b1")
        assert _wait_for(lambda: all(j.status == "done" for j in first))
        gate.clear()

        second = _cancellable_batch(jm, 2, _cancellable(started, gate), "b2")
        assert _wait_for(lambda: second[0].status == "running")
        jm.pause_batch("b2")

        assert jm.cancel_batch("b1") == (0, 0)
        assert {j.status for j in first} == {"done"}
        assert all(os.path.exists(j.output_path) for j in first)
        assert jm.cancel_batch("b2") == (2, 0)
        assert jm.cancel_batch("nope") == (0, 0)
    finally:
        gate.set()
        jm._executor.shutdown(wait=False)


def test_cancelling_with_files_removes_what_the_batch_put_in_the_library(tmp_path, monkeypatch):
    """The finished episodes and their subtitles go, then the folders they leave
    empty - never the library root, never a file the batch did not write."""
    monkeypatch.setattr(jobs_module, "_library_roots", lambda: [os.path.realpath(tmp_path)])
    other = tmp_path / "Altro (2019)" / "Altro.mkv"
    other.parent.mkdir()
    other.write_bytes(b"x")
    jm = JobManager()
    jm.update_max_concurrent(1)
    gate, started, heard = threading.Event(), [], []
    jm.add_listener(lambda job: heard.append((job.job_id, job.status)))

    try:
        jobs = _cancellable_batch(
            jm, 4, _cancellable(started, gate, tmp_path, instant=("t0", "t1")))
        assert _wait_for(lambda: jobs[2].status == "running")
        jm.pause_batch("b1")

        assert jm.cancel_batch("b1", delete_files=True) == (2, 2)
        assert _wait_for(lambda: len(heard) == 4)
        assert {j.status for j in jobs} == {"cancelled"}
        assert not (tmp_path / "Serie (2020)").exists()
        assert other.exists() and tmp_path.exists()
        # The two that finished were heard as done, once, and never again.
        assert sorted(s for _, s in heard) == ["cancelled", "cancelled", "done", "done"]
    finally:
        gate.set()
        jm._executor.shutdown(wait=False)


def test_a_download_finishing_as_its_batch_is_purged_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "_library_roots", lambda: [os.path.realpath(tmp_path)])
    jm = JobManager()
    jm.update_max_concurrent(1)
    gate, heard = threading.Event(), []
    jm.add_listener(lambda job: heard.append(job.status))

    def ignores_the_cancel(name, **kwargs):
        gate.wait(timeout=2)
        video = tmp_path / "Film (2020)" / "Film.mkv"
        video.parent.mkdir()
        video.write_bytes(b"x")
        return str(video)

    try:
        job = jm._make_job("f", "episode", batch_id="b1")
        jm._submit_job(job, ignores_the_cancel, "f")
        assert _wait_for(lambda: job.status == "running")
        assert jm.cancel_batch("b1", delete_files=True) == (1, 0)
        gate.set()
        assert _wait_for(lambda: heard == ["cancelled"])
        assert not (tmp_path / "Film (2020)").exists()
    finally:
        gate.set()
        jm._executor.shutdown(wait=False)


def test_nothing_outside_a_library_is_removed(tmp_path, monkeypatch):
    library, outside = tmp_path / "lib", tmp_path / "elsewhere"
    library.mkdir()
    outside.mkdir()
    stray = outside / "Film.mkv"
    stray.write_bytes(b"x")
    monkeypatch.setattr(jobs_module, "_library_roots", lambda: [os.path.realpath(library)])

    assert jobs_module.remove_download_output(str(stray)) == []
    assert jobs_module.remove_download_output(str(library)) == []
    assert stray.exists() and library.exists()
