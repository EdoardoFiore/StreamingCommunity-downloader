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
