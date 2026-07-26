"""In-app notifications, and who is allowed to see or decide which request."""

from app.auth.permissions import ALL_PERMISSIONS, Permission
from app.requests import notify
from tests.conftest import do_setup, make_user, session_for
from tests.test_requests import FILM_BODY, _create, _login, _user


def _pair(client):
    return (
        _user(client, "bob", Permission.REQUEST),
        _user(client, "carol", Permission.REQUEST),
    )


# ── Scoping ────────────────────────────────────────────────────────────────────

def test_my_requests_shows_only_mine(client, admin_credentials, source, stub_jobs):
    do_setup(client, admin_credentials)
    bob, carol = _pair(client)

    csrf = _login(client, bob)
    _create(client, csrf, FILM_BODY)
    csrf = _login(client, carol)
    _create(client, csrf, {**FILM_BODY, "external_id": "999", "title": "Altro Film"})

    mine = client.get("/api/requests/mine").json()

    assert [r["title"] for r in mine] == ["Altro Film"]


def test_a_requester_cannot_read_someone_elses_request(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    bob, carol = _pair(client)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, FILM_BODY).json()["request"]["id"]

    _login(client, carol)

    assert client.get(f"/api/requests/{request_id}").status_code == 404


def test_a_requester_cannot_see_the_queue(client, admin_credentials, source, stub_jobs):
    do_setup(client, admin_credentials)
    bob, _ = _pair(client)
    _login(client, bob)

    assert client.get("/api/requests").status_code == 403


def test_a_requester_cannot_approve_or_deny(client, admin_credentials, source, stub_jobs):
    do_setup(client, admin_credentials)
    bob, _ = _pair(client)
    csrf = _login(client, bob)
    request_id = _create(client, csrf, FILM_BODY).json()["request"]["id"]

    assert client.post(
        f"/api/requests/{request_id}/approve", headers={"X-CSRF-Token": csrf}
    ).status_code == 403
    assert client.post(
        f"/api/requests/{request_id}/deny",
        json={"reason": "no"},
        headers={"X-CSRF-Token": csrf},
    ).status_code == 403
    assert stub_jobs == []


def test_an_admin_without_manage_requests_never_sees_the_queue(
    client, admin_credentials, source, stub_jobs
):
    """The reason permissions are independent rather than hierarchical."""
    do_setup(client, admin_credentials)
    settings_admin = make_user(
        "settings-admin", "jf-settings-admin-id",
        int(Permission.MANAGE_SETTINGS | Permission.MANAGE_USERS | Permission.MANAGE_FILES),
    )
    _login(client, settings_admin)

    assert client.get("/api/users").status_code == 200
    assert client.get("/api/requests").status_code == 403


def test_card_status_shows_only_what_the_user_may_know(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    bob, carol = _pair(client)
    csrf = _login(client, bob)
    _create(client, csrf, FILM_BODY)

    csrf = _login(client, carol)
    carols_view = client.post(
        "/api/requests/status",
        json={"source": "streamingcommunity", "external_ids": ["123"]},
        headers={"X-CSRF-Token": csrf},
    ).json()

    boss = _user(client, "boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    approvers_view = client.post(
        "/api/requests/status",
        json={"source": "streamingcommunity", "external_ids": ["123"]},
        headers={"X-CSRF-Token": csrf},
    ).json()

    assert carols_view == {}
    assert approvers_view["123"]["status"] == "pending"


# ── Notifications ──────────────────────────────────────────────────────────────

def test_approvers_are_notified_of_a_new_request(client, admin_credentials, source, stub_jobs):
    do_setup(client, admin_credentials)
    boss = _user(client, "boss", ALL_PERMISSIONS)
    bob, _ = _pair(client)
    csrf = _login(client, bob)

    _create(client, csrf, FILM_BODY)

    events = [n["event"] for n in notify.list_for_user(boss.id)]
    assert notify.REQUEST_CREATED in events
    assert notify.list_for_user(bob.id) == []


def test_a_disabled_approver_is_not_notified(client, admin_credentials, source, stub_jobs):
    from app.auth import models as auth_models

    do_setup(client, admin_credentials)
    boss = _user(client, "boss", ALL_PERMISSIONS)
    auth_models.set_enabled(boss.id, False)
    bob, _ = _pair(client)
    csrf = _login(client, bob)

    _create(client, csrf, FILM_BODY)

    assert notify.list_for_user(boss.id) == []


def test_notifications_are_listed_with_an_unread_count(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    bob, carol = _pair(client)
    csrf = _login(client, bob)
    _create(client, csrf, FILM_BODY)
    csrf = _login(client, carol)
    _create(client, csrf, FILM_BODY)  # joins, notifying both

    payload = client.get("/api/notifications").json()

    assert payload["unread"] == len(payload["items"]) >= 1
    assert all(item["read_at"] is None for item in payload["items"])


def test_marking_read_only_touches_your_own_notifications(
    client, admin_credentials, source, stub_jobs
):
    do_setup(client, admin_credentials)
    bob, carol = _pair(client)
    csrf = _login(client, bob)
    _create(client, csrf, FILM_BODY)
    csrf = _login(client, carol)
    _create(client, csrf, FILM_BODY)

    carols = client.get("/api/notifications").json()["items"]
    bobs_ids = [n["id"] for n in notify.list_for_user(bob.id)]

    csrf = _login(client, carol)
    response = client.post(
        "/api/notifications/read",
        json={"ids": [n["id"] for n in carols] + bobs_ids},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.json()["unread"] == 0
    assert notify.unread_count(bob.id) == len(bobs_ids)


def test_notifications_need_a_session(client, admin_credentials):
    do_setup(client, admin_credentials)
    client.cookies.clear()
    assert client.get("/api/notifications").status_code == 401
