"""Direct downloads: the path that skips the request queue reports for itself.

A season or a series asked for in one go must produce one summary, not one
message per episode.
"""

import pytest

from app import downloads_notify
from app.jobs import JobManager
from app.requests import notify
from tests.conftest import do_setup, make_user, session_for


@pytest.fixture(autouse=True)
def _clean_batches():
    downloads_notify._batches.clear()
    yield
    downloads_notify._batches.clear()


@pytest.fixture
def delivered(monkeypatch):
    """Capture the direct-download notifications, without touching channels.

    Only download_* events are recorded: patching notify() catches the request
    system's own messages too, and those belong to service.py's tests.
    """
    calls: list[dict] = []
    real = notify.notify

    # **kw so a new presentation argument shows up as a test to update rather
    # than as a TypeError swallowed inside notify()'s per-channel try/except.
    def record(event, message, user_ids, request_id=None, *, markdown_message=None,
               title=None, notify_type=None, **kw):
        if not event.startswith("download"):
            return real(event, message, user_ids, request_id,
                        markdown_message=markdown_message, title=title,
                        notify_type=notify_type, **kw)
        calls.append({
            "event": event, "message": message, "user_ids": user_ids,
            "markdown": markdown_message, "title": title,
            "notify_type": notify_type or notify.EVENT_NOTIFY_TYPE.get(event, notify.INFO),
            **kw,
        })

    monkeypatch.setattr(downloads_notify.notify, "notify", record)
    return calls


@pytest.fixture
def db(tmp_path):
    """A throwaway database: the listener asks whether a request owns the job."""
    from app import db as database

    database.configure(tmp_path / "jobs.db")
    database.run_migrations()
    yield database
    database.close_all()


def _job(manager, **kwargs):
    kwargs.setdefault("title", "Film")
    kwargs.setdefault("type_", "film")
    title = kwargs.pop("title")
    type_ = kwargs.pop("type_")
    status = kwargs.pop("status", "done")
    error = kwargs.pop("error", None)
    job = manager._make_job(title, type_, **kwargs)
    job.status = status
    job.error = error
    return job


@pytest.fixture
def manager():
    return JobManager()


# ── Single downloads ───────────────────────────────────────────────────────────

def test_a_finished_film_notifies_once(db, manager, delivered):
    job = _job(manager, title="Inception", media_label="Inception", year="2010", user_id=7)

    downloads_notify.on_job_finished(job)

    assert len(delivered) == 1
    assert delivered[0]["event"] == notify.DOWNLOAD_COMPLETED
    assert "Inception (2010)" in delivered[0]["message"]
    assert delivered[0]["user_ids"] == [7]
    assert delivered[0]["notify_type"] == notify.SUCCESS


def test_a_failed_download_reports_the_error(db, manager, delivered):
    job = _job(manager, title="Inception", status="error", error="HTTP 403", user_id=7)

    downloads_notify.on_job_finished(job)

    assert delivered[0]["event"] == notify.DOWNLOAD_FAILED
    assert "HTTP 403" in delivered[0]["message"]
    assert delivered[0]["notify_type"] == notify.FAILURE


def test_a_cancelled_download_is_not_announced(manager, delivered):
    job = _job(manager, title="Inception", status="cancelled", user_id=7)

    downloads_notify.on_job_finished(job)

    assert delivered == []


def test_an_error_is_stripped_of_its_query_string(db, manager, delivered):
    job = _job(manager, title="Film", status="error", user_id=7,
               error="403 su https://cdn.example.test/playlist.m3u8?token=segretissimo&e=1")

    downloads_notify.on_job_finished(job)

    assert "segretissimo" not in delivered[0]["message"]
    assert "https://cdn.example.test/playlist.m3u8" in delivered[0]["message"]


def test_an_open_mode_download_has_no_in_app_recipient(db, manager, delivered):
    job = _job(manager, title="Film", user_id=None)

    downloads_notify.on_job_finished(job)

    assert delivered[0]["user_ids"] == []


def test_a_job_owned_by_a_request_is_not_announced_twice(
    client, admin_credentials, source, stub_jobs, manager, delivered
):
    """service.on_job_finished already reports these; a second message would be
    a duplicate."""
    from app.auth.permissions import ALL_PERMISSIONS
    from app.requests import service

    do_setup(client, admin_credentials)
    boss = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    request, _ = service.create_request(
        requested_by=boss.id, source="streamingcommunity", media_type="film",
        external_id="123", title="Film", audio_languages=["ita"], subtitle_languages=[],
    )
    service.approve(request.id, boss.id)

    from app.requests import models
    job_id = models.get(request.id).job_id
    job = _job(manager, title="Film", user_id=boss.id)
    job.job_id = job_id

    downloads_notify.on_job_finished(job)

    assert delivered == []


# ── Batches ────────────────────────────────────────────────────────────────────

def _season_job(manager, number, status="done", error=None):
    return _job(manager, title=f"Serie S02E{number:02d}", type_="episode", status=status,
                error=error, batch_id="b1", batch_kind="season",
                batch_label="Serie — Stagione 2", media_label="Serie",
                season=2, episode_number=str(number), user_id=7)


def test_a_batch_emits_one_summary_not_one_per_episode(manager, delivered):
    downloads_notify.register("b1", kind="season", label="Serie — Stagione 2",
                              expected=3, user_id=7)

    for n in (1, 2, 3):
        downloads_notify.on_job_finished(_season_job(manager, n))

    assert len(delivered) == 1
    assert delivered[0]["event"] == notify.DOWNLOAD_BATCH_COMPLETED
    assert "3 episodi su 3" in delivered[0]["message"]
    assert delivered[0]["title"] == "Stagione completata"


def test_a_partial_batch_lists_the_failed_episodes(manager, delivered):
    downloads_notify.register("b1", kind="season", label="Serie — Stagione 2",
                              expected=3, user_id=7)

    downloads_notify.on_job_finished(_season_job(manager, 1))
    downloads_notify.on_job_finished(_season_job(manager, 2, status="error", error="HTTP 403"))
    downloads_notify.on_job_finished(_season_job(manager, 3))

    assert len(delivered) == 1
    summary = delivered[0]
    assert summary["event"] == notify.DOWNLOAD_BATCH_COMPLETED
    # Mixed outcome: neither green nor red.
    assert summary["notify_type"] == notify.WARNING
    assert "S02E02" in summary["markdown"]
    assert "HTTP 403" in summary["markdown"]
    assert summary["title"] == "Stagione completata con errori"


def test_a_batch_where_nothing_worked_uses_the_failure_event(manager, delivered):
    downloads_notify.register("b1", kind="season", label="Serie — Stagione 2",
                              expected=2, user_id=7)

    for n in (1, 2):
        downloads_notify.on_job_finished(_season_job(manager, n, status="error", error="timeout"))

    assert delivered[0]["event"] == notify.DOWNLOAD_BATCH_FAILED
    assert delivered[0]["notify_type"] == notify.FAILURE
    assert "nessun episodio scaricato" in delivered[0]["message"]


def test_a_cancelled_episode_still_closes_its_batch(manager, delivered):
    downloads_notify.register("b1", kind="season", label="Serie — Stagione 2",
                              expected=2, user_id=7)

    downloads_notify.on_job_finished(_season_job(manager, 1))
    downloads_notify.on_job_finished(_season_job(manager, 2, status="cancelled"))

    assert len(delivered) == 1
    assert "annullati" in delivered[0]["markdown"]
    assert downloads_notify.pending_batches() == 0


def test_the_summary_waits_for_every_episode(manager, delivered):
    downloads_notify.register("b1", kind="season", label="Serie — Stagione 2",
                              expected=3, user_id=7)

    downloads_notify.on_job_finished(_season_job(manager, 1))
    downloads_notify.on_job_finished(_season_job(manager, 2))

    assert delivered == []
    assert downloads_notify.pending_batches() == 1


def test_jobs_that_were_never_submitted_are_written_off(manager, delivered):
    downloads_notify.register("b1", kind="season", label="Serie — Stagione 2",
                              expected=3, user_id=7)
    downloads_notify.on_job_finished(_season_job(manager, 1))

    downloads_notify.abandon("b1", 2)

    assert len(delivered) == 1
    assert downloads_notify.pending_batches() == 0


def test_a_long_failure_list_is_capped(manager, delivered):
    downloads_notify.register("b1", kind="series", label="Serie", expected=15, user_id=7)

    for n in range(1, 16):
        downloads_notify.on_job_finished(_season_job(manager, n, status="error", error="HTTP 403"))

    markdown = delivered[0]["markdown"]
    assert markdown.count("• S02E") == downloads_notify.MAX_LISTED_FAILURES
    assert "…e altri 5." in markdown


def test_an_anime_batch_uses_flat_episode_labels(manager, delivered):
    downloads_notify.register("b1", kind="anime_all", label="Naruto", expected=1, user_id=7)
    job = _job(manager, title="Naruto E7", type_="anime", status="error", error="timeout",
               batch_id="b1", batch_kind="anime_all", batch_label="Naruto",
               media_label="Naruto", episode_number="7", user_id=7)

    downloads_notify.on_job_finished(job)

    assert delivered[0]["title"] == "Anime non scaricato"
    assert "• E7" in delivered[0]["markdown"]
