"""Requests: creation, deduplication, approval, dead links, missing tracks."""

import os

import pytest

from app.auth.permissions import ALL_PERMISSIONS, Permission
from app.requests import models, notify, service
from tests.conftest import do_setup, make_user, session_for


FILM_BODY = {
    "source": "streamingcommunity",
    "media_type": "film",
    "external_id": "123",
    "title": "Test Film",
    "year": "2020",
    "audio_languages": ["ita"],
    "subtitle_languages": ["ita"],
}

EPISODE_BODY = {
    "source": "streamingcommunity",
    "media_type": "episode",
    "external_id": "77",
    "slug": "test-series",
    "title": "Test Series",
    "year": "2019",
    "season": 1,
    "episode_number": "2",
    "audio_languages": ["ita"],
    "subtitle_languages": [],
}


@pytest.fixture
def panel(client, admin_credentials, source, stub_jobs):
    """A configured panel with an approver session and a requester on hand."""
    do_setup(client, admin_credentials)
    approver = models  # placeholder to keep the tuple readable below
    admin = _user(client, "boss", ALL_PERMISSIONS)
    return admin


def _user(client, username, permissions):
    from app.auth import models as auth_models
    user = auth_models.get_user_by_jellyfin_id(f"jf-{username}-id")
    if user is None:
        user = make_user(username, f"jf-{username}-id", int(permissions))
    return user


def _login(client, user):
    client.cookies.clear()
    return session_for(client, user.id)


def _create(client, csrf, body):
    return client.post("/api/requests", json=body, headers={"X-CSRF-Token": csrf})


# ── Creation ───────────────────────────────────────────────────────────────────

def test_requester_creates_a_pending_request(client, admin_credentials, source, stub_jobs):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)

    response = _create(client, csrf, FILM_BODY)

    assert response.status_code == 201
    body = response.json()
    assert body["created"] is True
    assert body["request"]["status"] == models.PENDING
    assert body["request"]["requested_by_username"] == "bob"
    assert body["request"]["audio_languages"] == ["ita"]
    assert stub_jobs == []  # nothing downloads before approval


def test_requester_identity_comes_from_the_session_not_the_body(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    carol = _user(client, "carol", Permission.REQUEST)
    csrf = _login(client, bob)

    response = _create(client, csrf, {**FILM_BODY, "requested_by": carol.id})

    assert response.json()["request"]["requested_by"] == bob.id


def test_request_records_the_tracks_the_source_really_offers(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    source.audio = ["ita", "jpn"]
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)

    snapshot = _create(client, csrf, FILM_BODY).json()["request"]["available_snapshot"]

    assert snapshot["audio"] == ["ita", "jpn"]


def test_cannot_request_an_audio_track_the_source_does_not_have(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    source.audio = ["eng"]
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)

    response = _create(client, csrf, FILM_BODY)

    assert response.status_code == 400
    assert "ita" in response.json()["detail"]
    assert models.list_all() == []


def test_invalid_language_codes_are_rejected(client, admin_credentials, source, stub_jobs):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)

    response = _create(client, csrf, {**FILM_BODY, "audio_languages": ["../../etc/passwd"]})

    assert response.status_code == 422


def test_request_is_marked_available_when_the_file_already_exists(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    folder = source.library / "Test Film (2020)"
    folder.mkdir(parents=True)
    (folder / "Test Film (2020).mp4").write_bytes(b"x")
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)

    request = _create(client, csrf, FILM_BODY).json()["request"]

    assert request["status"] == models.AVAILABLE
    assert stub_jobs == []


# ── Deduplication ──────────────────────────────────────────────────────────────

def test_identical_requests_produce_one_download_and_notify_both(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    carol = _user(client, "carol", Permission.REQUEST)

    csrf = _login(client, bob)
    first = _create(client, csrf, FILM_BODY).json()
    csrf = _login(client, carol)
    second = _create(client, csrf, FILM_BODY).json()

    assert second["created"] is False
    assert second["request"]["id"] == first["request"]["id"]
    assert len(models.list_all()) == 1
    assert set(models.subscribers(first["request"]["id"])) == {bob.id, carol.id}
    assert any(n["event"] == notify.REQUEST_JOINED for n in notify.list_for_user(bob.id))
    assert any(n["event"] == notify.REQUEST_JOINED for n in notify.list_for_user(carol.id))


def test_the_same_title_with_different_audio_is_a_different_request(
    client, admin_credentials, source, stub_jobs
):
    """Two users asking for different audio are not asking for the same thing."""
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    carol = _user(client, "carol", Permission.REQUEST)

    csrf = _login(client, bob)
    _create(client, csrf, {**FILM_BODY, "audio_languages": ["ita"]})
    csrf = _login(client, carol)
    second = _create(client, csrf, {**FILM_BODY, "audio_languages": ["eng"]}).json()

    assert second["created"] is True
    assert len(models.list_all()) == 2


def test_different_subtitles_also_make_a_different_request(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)

    _create(client, csrf, {**FILM_BODY, "subtitle_languages": ["ita"]})
    second = _create(client, csrf, {**FILM_BODY, "subtitle_languages": ["ita", "eng"]}).json()

    assert second["created"] is True


def test_a_closed_request_does_not_block_asking_again(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    first = _create(client, csrf, FILM_BODY).json()["request"]
    models.transition(first["id"], models.DENIED)

    second = _create(client, csrf, FILM_BODY).json()

    assert second["created"] is True
    assert second["request"]["id"] != first["id"]


def test_language_order_does_not_change_identity(client, admin_credentials, source, stub_jobs):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)

    _create(client, csrf, {**FILM_BODY, "audio_languages": ["ita", "eng"]})
    second = _create(client, csrf, {**FILM_BODY, "audio_languages": ["eng", "ita"]}).json()

    assert second["created"] is False


# ── Approval ───────────────────────────────────────────────────────────────────

def test_approval_starts_the_download_and_records_the_job(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, FILM_BODY).json()["request"]["id"]

    boss = _user(client, "boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    response = client.post(
        f"/api/requests/{request_id}/approve", headers={"X-CSRF-Token": csrf}
    )

    assert response.status_code == 202
    assert len(stub_jobs) == 1
    assert models.get(request_id).status == models.DOWNLOADING
    assert models.get(request_id).job_id is not None
    assert any(
        n["event"] == notify.REQUEST_APPROVED for n in notify.list_for_user(bob.id)
    )


def test_approval_passes_the_requested_tracks_to_the_downloader(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    source.audio = ["ita", "eng"]
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _create(
        client, csrf, {**FILM_BODY, "audio_languages": ["eng"], "subtitle_languages": ["ita"]}
    ).json()["request"]["id"]

    boss = _user(client, "boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    client.post(f"/api/requests/{request_id}/approve", headers={"X-CSRF-Token": csrf})

    _, _, kwargs = stub_jobs[0]
    assert kwargs["audio_languages"] == ["eng"]
    assert kwargs["subtitle_languages"] == ["ita"]
    assert kwargs["strict_audio"] is True


def test_approval_re_resolves_the_episode_instead_of_reusing_stale_data(
    client, admin_credentials, source, stub_jobs
):
    """The token and episode list captured at request time expire in hours."""
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, EPISODE_BODY).json()["request"]["id"]

    source.episodes = [{"id": 555, "n": "2", "name": "Rinumerato"}]
    boss = _user(client, "boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    client.post(f"/api/requests/{request_id}/approve", headers={"X-CSRF-Token": csrf})

    name, args, _ = stub_jobs[0]
    assert name == "submit_episode"
    episodes, index = args[1], args[2]
    assert episodes[index]["id"] == 555


def test_approval_skips_the_download_when_the_file_appeared_meanwhile(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, FILM_BODY).json()["request"]["id"]

    folder = source.library / "Test Film (2020)"
    folder.mkdir(parents=True)
    (folder / "Test Film (2020).mp4").write_bytes(b"x")

    boss = _user(client, "boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    client.post(f"/api/requests/{request_id}/approve", headers={"X-CSRF-Token": csrf})

    assert stub_jobs == []
    assert models.get(request_id).status == models.COMPLETED


# ── Dead links and missing tracks ──────────────────────────────────────────────

def test_a_dead_link_parks_the_request_without_downloading(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, EPISODE_BODY).json()["request"]["id"]

    source.dead = True
    boss = _user(client, "boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    client.post(f"/api/requests/{request_id}/approve", headers={"X-CSRF-Token": csrf})

    request = models.get(request_id)
    assert request.status == models.NEEDS_ATTENTION
    assert request.problem.startswith("link_dead")
    assert stub_jobs == []


def test_a_vanished_episode_parks_the_request(client, admin_credentials, source, stub_jobs):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, EPISODE_BODY).json()["request"]["id"]

    source.episodes = [{"id": 901, "n": "1", "name": "Solo il primo"}]
    boss = _user(client, "boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    client.post(f"/api/requests/{request_id}/approve", headers={"X-CSRF-Token": csrf})

    assert models.get(request_id).status == models.NEEDS_ATTENTION
    assert stub_jobs == []


def test_a_missing_audio_track_parks_the_request_instead_of_substituting(
    client, admin_credentials, source, stub_jobs
):
    """Downloading the wrong language silently is worse than failing."""
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, FILM_BODY).json()["request"]["id"]

    source.audio = ["eng"]  # Italian disappeared between request and approval
    boss = _user(client, "boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    client.post(f"/api/requests/{request_id}/approve", headers={"X-CSRF-Token": csrf})

    request = models.get(request_id)
    assert request.status == models.NEEDS_ATTENTION
    assert request.problem.startswith("missing_audio")
    assert "ita" in request.problem
    assert stub_jobs == []


def test_parked_requests_notify_the_approvers(client, admin_credentials, source, stub_jobs):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, FILM_BODY).json()["request"]["id"]

    source.audio = ["eng"]
    boss = _user(client, "boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    client.post(f"/api/requests/{request_id}/approve", headers={"X-CSRF-Token": csrf})

    events = [n["event"] for n in notify.list_for_user(boss.id)]
    assert notify.REQUEST_NEEDS_ATTENTION in events


def test_an_approver_can_fix_the_tracks_and_re_approve(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, FILM_BODY).json()["request"]["id"]

    source.audio = ["eng"]
    boss = _user(client, "boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    client.post(f"/api/requests/{request_id}/approve", headers={"X-CSRF-Token": csrf})
    assert models.get(request_id).status == models.NEEDS_ATTENTION

    client.patch(
        f"/api/requests/{request_id}",
        json={"audio_languages": ["eng"]},
        headers={"X-CSRF-Token": csrf},
    )
    client.post(f"/api/requests/{request_id}/approve", headers={"X-CSRF-Token": csrf})

    assert models.get(request_id).status == models.DOWNLOADING
    assert len(stub_jobs) == 1


def test_only_a_parked_request_can_be_edited(client, admin_credentials, source, stub_jobs):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, FILM_BODY).json()["request"]["id"]

    boss = _user(client, "boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    response = client.patch(
        f"/api/requests/{request_id}",
        json={"audio_languages": ["eng"]},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409


# ── Denial, cancellation, completion ───────────────────────────────────────────

def test_denial_reason_is_optional_and_reaches_the_requester_when_given(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, FILM_BODY).json()["request"]["id"]

    boss = _user(client, "boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    response = client.post(
        f"/api/requests/{request_id}/deny",
        json={"reason": "Già disponibile altrove"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    assert response.json()["denial_reason"] == "Già disponibile altrove"
    assert response.json()["decided_by_username"] == "boss"
    messages = [n["message"] for n in notify.list_for_user(bob.id)]
    assert any("Già disponibile altrove" in m for m in messages)


def test_denial_without_a_reason_still_works(client, admin_credentials, source, stub_jobs):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, FILM_BODY).json()["request"]["id"]

    boss = _user(client, "boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)

    response = client.post(
        f"/api/requests/{request_id}/deny", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200
    assert response.json()["denial_reason"] is None
    assert response.json()["status"] == "denied"


def test_denial_with_a_blank_reason_is_treated_as_none(client, admin_credentials, source, stub_jobs):
    """A whitespace-only reason from the client is the same as no reason at all."""
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, FILM_BODY).json()["request"]["id"]

    boss = _user(client, "boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    response = client.post(
        f"/api/requests/{request_id}/deny", json={"reason": "   "}, headers={"X-CSRF-Token": csrf}
    )

    assert response.status_code == 200
    assert response.json()["denial_reason"] is None


def test_job_completion_marks_the_request_done_and_notifies(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, FILM_BODY).json()["request"]["id"]
    boss = _user(client, "boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    client.post(f"/api/requests/{request_id}/approve", headers={"X-CSRF-Token": csrf})

    job = type("Job", (), {
        "job_id": models.get(request_id).job_id,
        "status": "done",
        "output_path": "/library/Test Film (2020)/Test Film (2020).mp4",
        "error": None,
    })()
    service.on_job_finished(job)

    request = models.get(request_id)
    assert request.status == models.COMPLETED
    assert request.output_path.endswith(".mp4")
    assert any(n["event"] == notify.REQUEST_COMPLETED for n in notify.list_for_user(bob.id))


def test_job_failure_marks_the_request_failed_and_tells_everyone(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, FILM_BODY).json()["request"]["id"]
    boss = _user(client, "boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    client.post(f"/api/requests/{request_id}/approve", headers={"X-CSRF-Token": csrf})

    job = type("Job", (), {
        "job_id": models.get(request_id).job_id,
        "status": "error",
        "output_path": None,
        "error": "segmento mancante",
    })()
    service.on_job_finished(job)

    assert models.get(request_id).status == models.FAILED
    assert any(n["event"] == notify.REQUEST_FAILED for n in notify.list_for_user(bob.id))
    assert any(n["event"] == notify.REQUEST_FAILED for n in notify.list_for_user(boss.id))


def test_requester_can_withdraw_their_own_pending_request(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, FILM_BODY).json()["request"]["id"]

    response = client.delete(f"/api/requests/{request_id}", headers={"X-CSRF-Token": csrf})

    assert response.status_code == 200
    assert models.get(request_id).status == models.CANCELLED


def test_requester_can_withdraw_their_own_request_once_it_is_downloading(
    client, admin_credentials, source, stub_jobs
):
    """It is still their request even after an admin has picked it up — and
    withdrawing it also stops the running job (see service.cancel)."""
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    bob_csrf = _login(client, bob)
    request_id = _create(client, bob_csrf, FILM_BODY).json()["request"]["id"]
    boss = _user(client, "boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    client.post(f"/api/requests/{request_id}/approve", headers={"X-CSRF-Token": csrf})
    assert models.get(request_id).status == models.DOWNLOADING

    bob_csrf = _login(client, bob)
    response = client.delete(f"/api/requests/{request_id}", headers={"X-CSRF-Token": bob_csrf})

    assert response.status_code == 200
    assert models.get(request_id).status == models.CANCELLED


def test_requester_still_cannot_withdraw_a_terminal_request(
    client, admin_credentials, source, stub_jobs
):
    """The state machine, not a permission check, is what stops this: CANCELLED
    has no transition in from a closed status."""
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    bob_csrf = _login(client, bob)
    request_id = _create(client, bob_csrf, FILM_BODY).json()["request"]["id"]
    boss = _user(client, "boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    client.post(
        f"/api/requests/{request_id}/deny",
        json={"reason": "no"},
        headers={"X-CSRF-Token": csrf},
    )

    bob_csrf = _login(client, bob)
    response = client.delete(f"/api/requests/{request_id}", headers={"X-CSRF-Token": bob_csrf})

    assert response.status_code == 409
    assert models.get(request_id).status == models.DENIED
