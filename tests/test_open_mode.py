"""Open mode: the panel runs without a Jellyfin server, exactly like before
SSO existed — no login, no request queue, no user management. Every request
gets the same implicit permissions.

There used to be two ways in, an ``AUTH_ENABLED=0`` deployment and the setup
wizard's "Continua senza Jellyfin", and they had to be kept from drifting
apart. Only the wizard's answer is left: the reachability and permission
assertions live here, and tests/test_auth_skip.py covers the endpoint that
writes it.
"""

from app.auth import models
from tests.conftest import do_setup, enable_open_mode


def test_root_reachable_with_no_cookie_at_all(client):
    enable_open_mode()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200


def test_login_page_redirects_home(client):
    enable_open_mode()
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"


def test_me_reports_the_implicit_permission_set(client):
    enable_open_mode()
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    body = response.json()
    assert body["auth_enabled"] is False
    assert set(body["user"]["permission_names"]) == {
        "DOWNLOAD", "MANAGE_SETTINGS", "MANAGE_FILES", "VIEW_LIBRARY",
    }


def test_download_endpoint_reachable_without_any_session(client, stub_jobs):
    enable_open_mode()
    response = client.post("/api/download/film", json={"id": 1, "title": "Movie"})
    assert response.status_code == 202


def test_no_csrf_token_required_for_state_changing_requests(client, stub_jobs):
    """No cookie means no X-CSRF-Token header either — open mode must not
    reject state-changing calls for lacking one."""
    enable_open_mode()
    response = client.post("/api/download/film", json={"id": 2, "title": "Another"})
    assert response.status_code == 202


def test_request_queue_is_not_reachable(client):
    enable_open_mode()
    assert client.get("/api/requests").status_code == 403


def test_user_management_is_not_reachable(client):
    enable_open_mode()
    assert client.get("/api/users").status_code == 403


def test_setup_is_refused_and_login_has_no_server_to_ask(client):
    """Neither endpoint answers 404 any more — the panel simply has its answer
    already (the wizard ran) and no Jellyfin to authenticate against. Going
    back to Jellyfin from here is /jellyfin-connect, not /setup."""
    enable_open_mode()
    setup = client.post(
        "/api/auth/setup", json={"url": "http://x", "username": "a", "password": "b"}
    )
    login = client.post("/api/auth/jellyfin", json={"username": "a", "password": "b"})
    assert setup.status_code == 403
    assert login.status_code == 409


def test_status_reports_auth_disabled(client):
    enable_open_mode()
    response = client.get("/api/auth/status")
    assert response.status_code == 200
    body = response.json()
    assert body == {"setup_done": True, "jellyfin_url": None, "auth_enabled": False}


def test_jellyfin_mode_is_unaffected(client, admin_credentials):
    """A panel that went through the wizard the other way reports itself as
    authenticated."""
    body = do_setup(client, admin_credentials)
    assert body["auth_enabled"] is True
    assert models.runtime_open_mode() is False


def test_a_fresh_install_starts_closed_and_asks(client):
    """No auth_mode row yet: the panel is nobody's until the wizard runs, so
    the shell is not served to an anonymous visitor.

    This is the whole point of removing AUTH_ENABLED — the default is a
    question, not an answer that an environment variable already gave.
    """
    assert models.get_setting(models.SETTING_AUTH_MODE) is None
    assert client.get("/api/domain/settings").status_code == 401
    assert client.get("/login", follow_redirects=False).status_code == 200
