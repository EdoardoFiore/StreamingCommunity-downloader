"""AUTH_ENABLED=0: the panel runs without a Jellyfin server, exactly like
before SSO existed — no login, no setup wizard, no request queue or user
management. Every request gets the same implicit permissions.
"""

from app import main as main_module
from app.auth import deps
from app.auth import router as auth_router
from tests.conftest import do_setup


def _enable_open_mode(monkeypatch):
    monkeypatch.setattr(deps, "AUTH_ENABLED", False)
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", False)
    monkeypatch.setattr(main_module, "AUTH_ENABLED", False)


def test_root_reachable_with_no_cookie_at_all(client, monkeypatch):
    _enable_open_mode(monkeypatch)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200


def test_login_page_redirects_home(client, monkeypatch):
    _enable_open_mode(monkeypatch)
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"


def test_me_reports_the_implicit_permission_set(client, monkeypatch):
    _enable_open_mode(monkeypatch)
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    body = response.json()
    assert body["auth_enabled"] is False
    assert set(body["user"]["permission_names"]) == {
        "DOWNLOAD", "MANAGE_SETTINGS", "MANAGE_FILES", "VIEW_LIBRARY",
    }


def test_download_endpoint_reachable_without_any_session(client, monkeypatch, stub_jobs):
    _enable_open_mode(monkeypatch)
    response = client.post("/api/download/film", json={"id": 1, "title": "Movie"})
    assert response.status_code == 202


def test_no_csrf_token_required_for_state_changing_requests(client, monkeypatch, stub_jobs):
    """No cookie means no X-CSRF-Token header either — open mode must not
    reject state-changing calls for lacking one."""
    _enable_open_mode(monkeypatch)
    response = client.post("/api/download/film", json={"id": 2, "title": "Another"})
    assert response.status_code == 202


def test_request_queue_is_not_reachable(client, monkeypatch):
    _enable_open_mode(monkeypatch)
    assert client.get("/api/requests").status_code == 403


def test_user_management_is_not_reachable(client, monkeypatch):
    _enable_open_mode(monkeypatch)
    assert client.get("/api/users").status_code == 403


def test_setup_and_login_endpoints_are_disabled(client, monkeypatch):
    _enable_open_mode(monkeypatch)
    setup = client.post(
        "/api/auth/setup", json={"url": "http://x", "username": "a", "password": "b"}
    )
    login = client.post("/api/auth/jellyfin", json={"username": "a", "password": "b"})
    assert setup.status_code == 404
    assert login.status_code == 404


def test_status_reports_auth_disabled(client, monkeypatch):
    _enable_open_mode(monkeypatch)
    response = client.get("/api/auth/status")
    assert response.status_code == 200
    body = response.json()
    assert body == {"setup_done": True, "jellyfin_url": None, "auth_enabled": False}


def test_normal_mode_is_unaffected(client, admin_credentials):
    """AUTH_ENABLED defaults to True when not overridden: setup still works
    and reports itself as enabled."""
    body = do_setup(client, admin_credentials)
    assert body["auth_enabled"] is True
