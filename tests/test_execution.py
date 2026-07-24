"""Approval must always reach a visible state — never silently stall.

Regression cover for a bug where resolver.resolve() called JobManager with an
argument it did not accept. The TypeError was raised on a worker thread whose
Future nobody read, so it vanished without a log and the request sat in
`approved` forever: approved in the UI, no download, no error.
"""

import inspect

import pytest

from app.auth.permissions import ALL_PERMISSIONS, Permission
from app.jobs import job_manager
from app.requests import models, notify, resolver, service
from tests.conftest import do_setup, make_user
from tests.test_requests import FILM_BODY, EPISODE_BODY


SUBMIT_METHODS = ["submit_film", "submit_episode", "submit_anime_episode"]


@pytest.mark.parametrize("method", SUBMIT_METHODS)
def test_job_manager_accepts_strict_audio(method):
    """The request path always asks for strict audio; the API has to take it."""
    assert "strict_audio" in inspect.signature(getattr(job_manager, method)).parameters


@pytest.fixture
def approver(client, admin_credentials):
    do_setup(client, admin_credentials)
    return make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))


def _pending(user_id: int, body=FILM_BODY) -> models.Request:
    request, _ = models.create(
        source=body["source"],
        media_type=body["media_type"],
        external_id=body["external_id"],
        title=body["title"],
        year=body.get("year"),
        slug=body.get("slug"),
        season=body.get("season"),
        episode_number=body.get("episode_number"),
        audio_languages=body["audio_languages"],
        subtitle_languages=body["subtitle_languages"],
        requested_by=user_id,
    )
    return request


@pytest.mark.parametrize("body", [FILM_BODY, EPISODE_BODY], ids=["film", "episode"])
def test_approval_reaches_the_real_job_manager(client, approver, source, body, monkeypatch):
    """End to end with the genuine JobManager, only the download itself faked.

    stub_jobs is not used here on purpose: this is the seam where a signature
    mismatch used to hide.
    """
    started = []
    monkeypatch.setattr(
        job_manager, "_submit_job",
        lambda job, fn, *args, **kwargs: started.append((fn.__name__, kwargs)) or job.job_id,
    )
    request = _pending(approver.id, body)

    service.approve(request.id, approver.id)

    assert len(started) == 1, "the download was never handed to the job manager"
    _, kwargs = started[0]
    assert kwargs["strict_audio"] is True
    assert models.get(request.id).status == models.DOWNLOADING
    assert models.get(request.id).job_id


def test_a_failure_after_resolution_parks_the_request(client, approver, source, monkeypatch):
    """Whatever goes wrong, the request must not stay stuck in approved."""
    monkeypatch.setattr(
        job_manager, "submit_film",
        lambda *a, **k: (_ for _ in ()).throw(TypeError("unexpected keyword argument")),
    )
    request = _pending(approver.id)

    service.approve(request.id, approver.id)

    updated = models.get(request.id)
    assert updated.status == models.NEEDS_ATTENTION
    assert "unexpected keyword argument" in updated.problem
    assert any(
        n["event"] == notify.REQUEST_NEEDS_ATTENTION for n in notify.list_for_user(approver.id)
    )


def test_no_request_is_left_in_approved_after_execution(client, approver, source, monkeypatch):
    """`approved` is a claim state: execution always moves out of it."""
    monkeypatch.setattr(
        job_manager, "submit_film", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    request = _pending(approver.id)

    service.approve(request.id, approver.id)

    assert models.get(request.id).status != models.APPROVED


def test_a_job_that_finished_before_its_id_was_stored_is_still_recorded(
    client, approver, source, monkeypatch
):
    """The job goes to the executor before its id reaches the row.

    A download that fails instantly fires the completion listener while
    get_by_job_id() still finds nothing, which used to strand the request in
    `downloading` forever.
    """
    class FinishedJob:
        job_id = "job-instant"
        status = "error"
        output_path = None
        error = "morto subito"

    monkeypatch.setattr(job_manager, "submit_film", lambda *a, **k: FinishedJob.job_id)
    monkeypatch.setattr(job_manager, "get", lambda job_id: FinishedJob())
    request = _pending(approver.id)

    service.approve(request.id, approver.id)

    assert models.get(request.id).status == models.FAILED


def test_a_failure_outside_the_resolve_step_is_still_caught(client, approver, source, monkeypatch):
    """The library check runs after resolve, outside its try — it must be covered too.

    _execute's Future is never read, so anything escaping it would be invisible:
    no log, no state change, a request approved forever.
    """
    monkeypatch.setattr(
        resolver, "is_in_library", lambda request: (_ for _ in ()).throw(OSError("disco assente"))
    )
    request = _pending(approver.id)

    service.approve(request.id, approver.id)  # must not propagate

    updated = models.get(request.id)
    assert updated.status == models.NEEDS_ATTENTION
    assert "disco assente" in updated.problem
