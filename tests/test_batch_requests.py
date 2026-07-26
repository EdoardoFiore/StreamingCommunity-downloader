"""Batch approve/deny/cancel, the /counts badge endpoint, and the card status
no longer showing closed requests as if they blocked a new one."""

import pytest

from app.auth.permissions import ALL_PERMISSIONS, Permission
from app.requests import models, notify
from tests.conftest import do_setup, make_user, session_for
from tests.test_requests import EPISODE_BODY, FILM_BODY, _create, _login, _user


def _episode(client, csrf, tv_id="77", season=1, episode="2", slug="test-series"):
    body = {**EPISODE_BODY, "external_id": tv_id, "season": season, "episode_number": episode,
            "slug": slug}
    return _create(client, csrf, body).json()["request"]["id"]


@pytest.fixture
def approver(client, admin_credentials):
    do_setup(client, admin_credentials)
    return make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))


# ── Batch approve ────────────────────────────────────────────────────────────────

def test_approve_batch_starts_every_download(client, approver, source, stub_jobs):
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    ids = [_episode(client, csrf, episode=str(n)) for n in (1, 2, 3)]

    csrf = _login(client, approver)
    response = client.post(
        "/api/requests/approve-batch", json={"ids": ids}, headers={"X-CSRF-Token": csrf}
    )

    # Approval returns the moment the claim is taken — same "approved" snapshot
    # the single-item endpoint returns — while resolution and download run in
    # the background; the source fixture runs that inline, so by the time the
    # response comes back the DB rows are already further along.
    assert response.status_code == 202
    results = response.json()["results"]
    assert all(r["ok"] for r in results)
    assert all(models.get(i).status == "downloading" for i in ids)
    assert len(stub_jobs) == 3


def test_approve_batch_reports_partial_failure_without_aborting(
    client, approver, source, stub_jobs
):
    """One bad id in the batch must not stop the others from being approved."""
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    good = _episode(client, csrf, episode="1")

    csrf = _login(client, approver)
    response = client.post(
        "/api/requests/approve-batch",
        json={"ids": [good, 999999]},
        headers={"X-CSRF-Token": csrf},
    )

    results = {r["id"]: r for r in response.json()["results"]}
    assert results[good]["ok"] is True
    assert results[999999]["ok"] is False
    assert results[999999]["error"]
    assert models.get(good).status == "downloading"


def test_approve_batch_requires_manage_requests(client, approver, source, stub_jobs):
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _episode(client, csrf)

    response = client.post(
        "/api/requests/approve-batch", json={"ids": [request_id]}, headers={"X-CSRF-Token": csrf}
    )

    assert response.status_code == 403


def test_approve_batch_rejects_an_empty_list(client, approver):
    csrf = _login(client, approver)
    response = client.post(
        "/api/requests/approve-batch", json={"ids": []}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 422


# ── Batch deny ─────────────────────────────────────────────────────────────────

def test_deny_batch_applies_the_same_reason_to_all(client, approver, source, stub_jobs):
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    ids = [_episode(client, csrf, episode=str(n)) for n in (1, 2)]

    csrf = _login(client, approver)
    response = client.post(
        "/api/requests/deny-batch",
        json={"ids": ids, "reason": "stagione già in libreria"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    for request_id in ids:
        request = models.get(request_id)
        assert request.status == "denied"
        assert request.denial_reason == "stagione già in libreria"
    assert all(
        "stagione già in libreria" in n["message"] for n in notify.list_for_user(bob.id)
    )


def test_deny_batch_reason_is_optional(client, approver, source, stub_jobs):
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _episode(client, csrf, episode="1")

    csrf = _login(client, approver)
    response = client.post(
        "/api/requests/deny-batch", json={"ids": [request_id]}, headers={"X-CSRF-Token": csrf}
    )

    assert response.status_code == 200
    assert response.json()["results"][0]["ok"] is True
    assert models.get(request_id).denial_reason is None


# ── Batch cancel ───────────────────────────────────────────────────────────────

def test_requester_can_batch_cancel_their_own_pending_requests(client, source):
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    ids = [_episode(client, csrf, episode=str(n)) for n in (1, 2)]

    response = client.post(
        "/api/requests/cancel-batch", json={"ids": ids}, headers={"X-CSRF-Token": csrf}
    )

    assert all(r["ok"] for r in response.json()["results"])
    assert all(models.get(i).status == "cancelled" for i in ids)


def test_batch_cancel_refuses_someone_elses_request(client, source):
    bob = _user(client, "bob", Permission.REQUEST)
    carol = _user(client, "carol", Permission.REQUEST)
    csrf = _login(client, bob)
    bobs_request = _episode(client, csrf, episode="1")

    csrf = _login(client, carol)
    response = client.post(
        "/api/requests/cancel-batch", json={"ids": [bobs_request]}, headers={"X-CSRF-Token": csrf}
    )

    result = response.json()["results"][0]
    assert result["ok"] is False
    assert models.get(bobs_request).status == "pending"


def test_approver_can_batch_cancel_anyones_request(client, approver, source):
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _episode(client, csrf)

    csrf = _login(client, approver)
    response = client.post(
        "/api/requests/cancel-batch", json={"ids": [request_id]}, headers={"X-CSRF-Token": csrf}
    )

    assert response.json()["results"][0]["ok"] is True
    assert models.get(request_id).status == "cancelled"


def test_batch_cancel_lets_a_plain_requester_withdraw_a_downloading_request(
    client, approver, source, stub_jobs
):
    """Not just pending: it is still their own request once it is running."""
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _episode(client, csrf)
    csrf = _login(client, approver)
    client.post(f"/api/requests/{request_id}/approve", headers={"X-CSRF-Token": csrf})

    csrf = _login(client, bob)
    response = client.post(
        "/api/requests/cancel-batch", json={"ids": [request_id]}, headers={"X-CSRF-Token": csrf}
    )

    assert response.json()["results"][0]["ok"] is True
    assert models.get(request_id).status == "cancelled"


def test_batch_cancel_still_refuses_a_terminal_request(client, approver, source, stub_jobs):
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _episode(client, csrf)
    csrf = _login(client, approver)
    client.post(
        f"/api/requests/{request_id}/deny",
        json={"reason": "no"},
        headers={"X-CSRF-Token": csrf},
    )

    csrf = _login(client, bob)
    response = client.post(
        "/api/requests/cancel-batch", json={"ids": [request_id]}, headers={"X-CSRF-Token": csrf}
    )

    assert response.json()["results"][0]["ok"] is False
    assert models.get(request_id).status == "denied"


# ── Counts badge ───────────────────────────────────────────────────────────────

def test_counts_reflect_pending_and_needs_attention_only(client, approver, source, stub_jobs):
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    _episode(client, csrf, episode="1")
    parked_id = _episode(client, csrf, episode="2")
    source.dead = True  # the next approval parks instead of downloading
    csrf = _login(client, approver)
    client.post(f"/api/requests/{parked_id}/approve", headers={"X-CSRF-Token": csrf})

    counts = client.get("/api/requests/counts").json()

    assert counts["pending"] == 1
    assert counts["needs_attention"] == 1
    assert counts["action_required"] == 2


def test_counts_needs_manage_requests(client, source):
    bob = _user(client, "bob", Permission.REQUEST)
    _login(client, bob)
    assert client.get("/api/requests/counts").status_code == 403


def test_counts_is_zero_on_an_empty_queue(client, approver):
    counts = client.get("/api/requests/counts").json()
    assert counts == {"pending": 0, "needs_attention": 0, "action_required": 0}


# ── Card status hides closed requests ───────────────────────────────────────────

def test_denied_request_does_not_appear_in_card_status(client, approver, source, stub_jobs):
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, FILM_BODY).json()["request"]["id"]
    csrf = _login(client, approver)
    client.post(
        f"/api/requests/{request_id}/deny",
        json={"reason": "no"},
        headers={"X-CSRF-Token": csrf},
    )

    csrf = _login(client, bob)
    status = client.post(
        "/api/requests/status",
        json={"source": "streamingcommunity", "external_ids": [FILM_BODY["external_id"]]},
        headers={"X-CSRF-Token": csrf},
    ).json()

    assert status == {}


def test_a_fresh_request_after_denial_does_appear_in_card_status(
    client, approver, source, stub_jobs
):
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    first_id = _create(client, csrf, FILM_BODY).json()["request"]["id"]
    csrf = _login(client, approver)
    client.post(
        f"/api/requests/{first_id}/deny", json={"reason": "no"}, headers={"X-CSRF-Token": csrf}
    )

    csrf = _login(client, bob)
    second = _create(client, csrf, FILM_BODY).json()
    assert second["created"] is True

    status = client.post(
        "/api/requests/status",
        json={"source": "streamingcommunity", "external_ids": [FILM_BODY["external_id"]]},
        headers={"X-CSRF-Token": csrf},
    ).json()

    assert status[FILM_BODY["external_id"]]["status"] == "pending"
    assert status[FILM_BODY["external_id"]]["id"] == second["request"]["id"]


def test_cancelled_and_failed_are_also_hidden_from_card_status(
    client, approver, source, stub_jobs
):
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, FILM_BODY).json()["request"]["id"]

    client.delete(f"/api/requests/{request_id}", headers={"X-CSRF-Token": csrf})

    status = client.post(
        "/api/requests/status",
        json={"source": "streamingcommunity", "external_ids": [FILM_BODY["external_id"]]},
        headers={"X-CSRF-Token": csrf},
    ).json()
    assert status == {}
