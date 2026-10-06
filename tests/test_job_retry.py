"""A failed download can be run again, and the retry replaces the failed entry."""

import time

from app.jobs import JobManager


def _wait(job, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline and job.status not in ("done", "error", "cancelled"):
        time.sleep(0.02)


def test_a_failed_job_runs_again_with_the_same_arguments():
    jm = JobManager()
    calls = []

    def flaky(tv_id, *, audio_languages, temp_dir, progress_factory, cancel_event):
        calls.append((tv_id, audio_languages, temp_dir, cancel_event))
        if len(calls) == 1:
            raise RuntimeError("503")
        return "/lib/ep.mkv"

    try:
        job = jm._make_job("Serie S01E01", "episode", batch_id="b1", batch_kind="season",
                           batch_label="Serie — Stagione 1", media_label="Serie", season=1)
        jm._submit_job(job, flaky, 77, audio_languages=["ita", "eng"],
                       temp_dir=f"tmp/{job.job_id}", progress_factory=None,
                       cancel_event=job.cancel_event)
        _wait(job)
        assert job.status == "error"

        new_id = jm.retry(job.job_id)

        assert new_id and new_id != job.job_id
        # The failed entry is gone, so a second failure cannot leave a duplicate.
        assert jm.get(job.job_id) is None
        new = jm.get(new_id)
        _wait(new)
        assert new.status == "done"
        assert calls[1][:2] == (77, ["ita", "eng"])
        # Arguments bound to a job are rebuilt for the new one, not reused.
        assert new_id in calls[1][2]
        assert calls[1][3] is new.cancel_event
        # The batch counted this episode already; the retry stands on its own.
        assert new.batch_id is None
        assert (new.media_label, new.season) == ("Serie", 1)
    finally:
        jm._executor.shutdown(wait=False)


def test_only_a_failed_job_can_be_retried():
    jm = JobManager()
    try:
        job = jm._make_job("Film", "film")
        jm._submit_job(job, lambda: "/lib/film.mkv")
        _wait(job)
        assert job.status == "done"

        assert jm.retry(job.job_id) is None
        assert jm.get(job.job_id) is job
        assert jm.retry("no-such-job") is None
    finally:
        jm._executor.shutdown(wait=False)


def test_a_failure_is_retried_automatically_before_it_is_shown(monkeypatch):
    from app import jobs

    monkeypatch.setattr(jobs, "AUTO_RETRIES", 3)
    monkeypatch.setattr(jobs, "AUTO_RETRY_DELAY", 0)
    jm = JobManager()
    finished = []
    jm.add_listener(lambda job: finished.append(job.status))
    attempts = []

    def refused_twice():
        attempts.append(1)
        if len(attempts) <= 2:
            raise RuntimeError("403 from the source")
        return "/lib/ep.mkv"

    try:
        job = jm._make_job("Serie S01E02", "episode")
        jm._submit_job(job, refused_twice)
        _wait(job)
        time.sleep(0.05)

        assert job.status == "done"
        assert len(attempts) == 3
        assert job.retries == 2
        # One terminal report, with the final outcome: a batch counts it once.
        assert finished == ["done"]
    finally:
        jm._executor.shutdown(wait=False)


def test_it_gives_up_after_three_retries(monkeypatch):
    from app import jobs

    monkeypatch.setattr(jobs, "AUTO_RETRIES", 3)
    monkeypatch.setattr(jobs, "AUTO_RETRY_DELAY", 0)
    jm = JobManager()
    finished = []
    jm.add_listener(lambda job: finished.append(job.status))
    attempts = []

    def always_broken():
        attempts.append(1)
        raise RuntimeError("Percorso di destinazione non valido")

    try:
        job = jm._make_job("Film", "film")
        jm._submit_job(job, always_broken)
        _wait(job)
        time.sleep(0.05)

        assert job.status == "error"
        assert len(attempts) == 4   # the first try and three retries
        assert finished == ["error"]
    finally:
        jm._executor.shutdown(wait=False)


def test_a_missing_audio_track_is_not_retried(monkeypatch):
    from app import jobs
    from app.core._shared import MissingAudioTrackError

    monkeypatch.setattr(jobs, "AUTO_RETRIES", 3)
    monkeypatch.setattr(jobs, "AUTO_RETRY_DELAY", 0)
    jm = JobManager()
    attempts = []

    def no_english():
        attempts.append(1)
        raise MissingAudioTrackError("eng", ["ita"])

    try:
        job = jm._make_job("Film", "film")
        jm._submit_job(job, no_english)
        _wait(job)

        assert job.status == "error"
        assert len(attempts) == 1
    finally:
        jm._executor.shutdown(wait=False)


def test_a_permission_error_is_not_retried(monkeypatch):
    """#28: a non-root container could not create tmp/, and the job spent three
    minutes retrying an error the same uid would meet every time."""
    from app import jobs

    monkeypatch.setattr(jobs, "AUTO_RETRIES", 3)
    monkeypatch.setattr(jobs, "AUTO_RETRY_DELAY", 0)
    jm = JobManager()
    attempts = []

    def no_access():
        attempts.append(1)
        raise PermissionError(13, "Permission denied", "tmp")

    try:
        job = jm._make_job("Film", "film")
        jm._submit_job(job, no_access)
        _wait(job)

        assert job.status == "error"
        assert len(attempts) == 1
        assert "Permission denied" in job.error
    finally:
        jm._executor.shutdown(wait=False)


def test_what_is_worth_retrying():
    import requests

    from app.jobs import _worth_retrying

    class SharingViolation(PermissionError):
        # What Windows raises for a file another process holds open: it goes
        # away once Jellyfin or the antivirus lets go.
        winerror = 32

    assert not _worth_retrying(PermissionError(13, "Permission denied", "tmp"))
    assert _worth_retrying(SharingViolation(13, "in use", "film.mkv"))
    # Also an OSError, and exactly what a retry is for.
    assert _worth_retrying(requests.ConnectionError("connection reset"))
    assert _worth_retrying(RuntimeError("503"))


def test_cancelling_during_the_wait_ends_it_at_once(monkeypatch):
    from app import jobs

    monkeypatch.setattr(jobs, "AUTO_RETRIES", 3)
    monkeypatch.setattr(jobs, "AUTO_RETRY_DELAY", 30)
    jm = JobManager()
    finished = []
    jm.add_listener(lambda job: finished.append(job.status))

    def refused():
        raise RuntimeError("503")

    try:
        job = jm._make_job("Film", "film")
        jm._submit_job(job, refused)
        deadline = time.time() + 2
        while time.time() < deadline and job.retry_at is None:
            time.sleep(0.01)
        assert job.status == "queued" and job.retries == 1

        assert jm.cancel(job.job_id)
        deadline = time.time() + 2
        while time.time() < deadline and not finished:
            time.sleep(0.01)

        assert finished == ["cancelled"]
    finally:
        jm._executor.shutdown(wait=False)
