"""Jobs beyond max_concurrent_downloads queue instead of failing or blocking.

This already worked before this test existed — ThreadPoolExecutor(64) accepts
every submission, and _run_download blocks on a BoundedSemaphore(max_concurrent)
before flipping a job to "running". This test just locks the behaviour in.
"""

import threading
import time

from app.jobs import JobManager


def test_jobs_beyond_the_limit_wait_instead_of_erroring():
    jm = JobManager()
    jm.update_max_concurrent(2)

    lock = threading.Lock()
    concurrent = 0
    peak = 0
    release = threading.Event()

    def fake_download(*args, **kwargs):
        nonlocal concurrent, peak
        with lock:
            concurrent += 1
            peak = max(peak, concurrent)
        release.wait(timeout=2)
        with lock:
            concurrent -= 1
        return "ok"

    try:
        jobs = [jm._make_job(f"t{i}", "film") for i in range(6)]
        for job in jobs:
            jm._submit_job(job, fake_download)

        time.sleep(0.2)  # let the first wave hit the semaphore
        assert 1 <= peak <= 2, "more jobs ran at once than max_concurrent_downloads allowed"

        release.set()
        deadline = time.time() + 3
        while time.time() < deadline and any(j.status not in ("done", "error") for j in jobs):
            time.sleep(0.02)

        assert all(j.status == "done" for j in jobs), "a queued job never got its turn"
    finally:
        jm._executor.shutdown(wait=False)
