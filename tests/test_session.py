"""Session behaviour: closed by default, immediate revocation, CSRF."""

from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.auth import models, session as sessions
from tests.conftest import ALL, do_setup, make_user, session_for


PROTECTED_GET = [
    "/api/auth/me",
    "/api/jobs",
    "/api/files",
    "/api/domain/settings",
]


@pytest.mark.parametrize("path", PROTECTED_GET)
def test_endpoints_are_closed_without_a_session(client, path):
    assert client.get(path).status_code == 401


def test_public_endpoints_stay_reachable(client):
    assert client.get("/api/auth/status").status_code == 200
    assert client.get("/login").status_code == 200


def test_html_pages_redirect_to_login(client):
    response = client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_disabling_a_user_revokes_their_session_immediately(client, admin_credentials, jellyfin):
    """The reason sessions are rows and not signed tokens."""
    do_setup(client, admin_credentials)
    bob = make_user("bob", "jf-bob-id", ALL)
    session_for(client, bob.id)
    assert client.get("/api/auth/me").json()["user"]["username"] == "bob"

    models.set_enabled(bob.id, False)

    assert client.get("/api/auth/me").status_code == 401
    assert db.query("SELECT * FROM jf_session WHERE user_id = ?", (bob.id,)) == []


def test_expired_sessions_are_rejected_and_cleaned(client, admin_credentials):
    do_setup(client, admin_credentials)
    bob = make_user("bob", "jf-bob-id", ALL)
    session_for(client, bob.id)

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    db.execute("UPDATE jf_session SET expires_at = ? WHERE user_id = ?", (past, bob.id))

    assert client.get("/api/auth/me").status_code == 401
    assert db.query("SELECT * FROM jf_session WHERE user_id = ?", (bob.id,)) == []


def test_unknown_session_token_is_rejected(client, admin_credentials):
    do_setup(client, admin_credentials)
    client.cookies.set(sessions.SESSION_COOKIE, "not-a-real-token")
    assert client.get("/api/auth/me").status_code == 401


def test_state_changing_request_needs_the_csrf_token(client, admin_credentials):
    do_setup(client, admin_credentials)
    bob = make_user("bob", "jf-bob-id", ALL)
    session_for(client, bob.id)

    assert client.post("/api/auth/logout").status_code == 403


def test_state_changing_request_rejects_a_foreign_csrf_token(client, admin_credentials):
    do_setup(client, admin_credentials)
    bob = make_user("bob", "jf-bob-id", ALL)
    session_for(client, bob.id)
    _, other_csrf = sessions.create_session(bob.id)

    response = client.post("/api/auth/logout", headers={"X-CSRF-Token": other_csrf})

    assert response.status_code == 403


def test_csrf_rejection_carries_a_retry_marker_a_client_can_act_on(client, admin_credentials):
    """A stale token — e.g. a tab left open across a newer login elsewhere,
    which rotates the shared session cookie but not that tab's own in-memory
    token — is something a client can recover from by refreshing and retrying
    once. The marker header is what lets it tell that case apart from a real
    permission denial, which a refresh would never fix."""
    do_setup(client, admin_credentials)
    bob = make_user("bob", "jf-bob-id", ALL)
    session_for(client, bob.id)

    response = client.post("/api/auth/logout")

    assert response.status_code == 403
    assert response.headers.get("x-csrf-retry") == "1"


def test_permission_denied_carries_no_retry_marker(client, admin_credentials):
    """Refreshing the CSRF token cannot fix a missing permission — the
    frontend must not loop retrying this the way it does a stale token."""
    from app.auth.permissions import Permission

    do_setup(client, admin_credentials)
    bob = make_user("bob", "jf-bob-id", int(Permission.REQUEST))
    csrf = session_for(client, bob.id)

    response = client.post("/api/users/import", json={"jellyfin_user_ids": ["x"]},
                            headers={"X-CSRF-Token": csrf})

    assert response.status_code == 403
    assert "x-csrf-retry" not in {k.lower() for k in response.headers.keys()}


def test_state_changing_request_passes_with_the_matching_token(client, admin_credentials):
    do_setup(client, admin_credentials)
    bob = make_user("bob", "jf-bob-id", ALL)
    csrf = session_for(client, bob.id)

    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200


def test_get_requests_do_not_need_a_csrf_token(client, admin_credentials):
    do_setup(client, admin_credentials)
    bob = make_user("bob", "jf-bob-id", ALL)
    session_for(client, bob.id)
    assert client.get("/api/auth/me").status_code == 200
