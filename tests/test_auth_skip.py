"""Interactive alternative to Jellyfin SSO: skipping it from the setup wizard
(POST /api/auth/skip), and connecting/reconfiguring Jellyfin later from
Settings (POST /api/auth/jellyfin-connect).
"""

from app import db
from app.auth import models
from app.auth import router as auth_router
from app.auth.permissions import Permission
from tests.conftest import ALL, do_setup, make_user, session_for


def _skip(client):
    response = client.post("/api/auth/skip")
    assert response.status_code == 200, response.text
    return response


# ── /api/auth/skip ──────────────────────────────────────────────────────────────

def test_skip_marks_status_done_with_no_jellyfin(client):
    _skip(client)
    body = client.get("/api/auth/status").json()
    assert body == {"setup_done": True, "jellyfin_url": None, "auth_enabled": False}


def test_skip_matches_the_open_mode_permission_surface(client):
    """Skipping from the wizard must land on exactly the same implicit
    permission set as AUTH_ENABLED=0 (tests/test_open_mode.py), so the two
    entry points into open mode never drift apart."""
    _skip(client)
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    body = response.json()
    assert body["auth_enabled"] is False
    assert set(body["user"]["permission_names"]) == {
        "DOWNLOAD", "MANAGE_SETTINGS", "MANAGE_FILES", "VIEW_LIBRARY",
    }


def test_skip_gives_reachability_matching_open_mode(client, stub_jobs):
    _skip(client)
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.post("/api/download/film", json={"id": 1, "title": "x"}).status_code == 202
    assert client.get("/api/requests").status_code == 403
    assert client.get("/api/users").status_code == 403


def test_skip_refused_once_jellyfin_is_configured(client, admin_credentials):
    do_setup(client, admin_credentials)
    response = client.post("/api/auth/skip")
    assert response.status_code == 403
    assert models.runtime_open_mode() is False


def test_skip_refused_once_already_skipped(client):
    _skip(client)
    response = client.post("/api/auth/skip")
    assert response.status_code == 403


def test_skip_returns_404_when_auth_is_force_disabled(client, monkeypatch):
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", False)
    response = client.post("/api/auth/skip")
    assert response.status_code == 404


def test_setup_done_is_unaffected_by_missing_auth_mode_key(client, admin_credentials):
    """Installs that ran /setup before SETTING_AUTH_MODE existed have no
    ``auth_mode`` row at all: only jellyfin_url + a jf_user row. Simulated
    here by deleting the key /setup now writes. setup_done() must not send
    such an install back to the wizard, and /skip must still refuse to run."""
    do_setup(client, admin_credentials)
    db.execute("DELETE FROM jf_setting WHERE key = ?", (models.SETTING_AUTH_MODE,))

    assert models.get_setting(models.SETTING_AUTH_MODE) is None
    assert models.setup_done() is True
    assert client.post("/api/auth/skip").status_code == 403


# ── /api/auth/jellyfin-connect: first connection from open mode ────────────────

def test_connect_from_open_mode_succeeds_and_sets_a_session(client, admin_credentials, jellyfin):
    _skip(client)

    response = client.post("/api/auth/jellyfin-connect", json=admin_credentials)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["user"]["username"] == "admin"
    assert payload["user"]["permissions"] == ALL
    assert payload["csrf_token"]
    assert models.runtime_open_mode() is False
    assert models.setup_done() is True
    assert models.jellyfin_config()[0] == admin_credentials["url"]


def test_connect_from_open_mode_refused_for_non_administrator(client, jellyfin):
    _skip(client)
    jellyfin.add_user("bob", "bobpw", admin=False)

    response = client.post(
        "/api/auth/jellyfin-connect",
        json={"url": "http://jellyfin.local:8096", "username": "bob", "password": "bobpw"},
    )

    assert response.status_code == 403
    assert models.runtime_open_mode() is True
    assert models.count_users() == 0


def test_connect_from_open_mode_refused_with_bad_credentials(client, jellyfin):
    _skip(client)
    jellyfin.add_user("admin", "adminpw", admin=True)

    response = client.post(
        "/api/auth/jellyfin-connect",
        json={"url": "http://jellyfin.local:8096", "username": "admin", "password": "wrong"},
    )

    assert response.status_code == 401
    assert models.runtime_open_mode() is True


def test_connect_disengages_open_mode_for_other_sessions(client, admin_credentials, jellyfin):
    """Once connected, a second cookie-less request that previously reached an
    open-mode endpoint must be treated as unauthenticated: the middleware
    short-circuit only fires while runtime_open_mode() is still true."""
    _skip(client)
    client.post("/api/auth/jellyfin-connect", json=admin_credentials)

    client.cookies.clear()
    response = client.get("/api/domain/settings")
    assert response.status_code == 401


# ── /api/auth/jellyfin-connect: reconfiguring an already-connected instance ────

def test_reconfigure_requires_manage_users(client, admin_credentials, jellyfin):
    payload = do_setup(client, admin_credentials)

    new_url = {"url": "http://jellyfin2.local:8096", "username": "admin", "password": "adminpw"}
    response = client.post(
        "/api/auth/jellyfin-connect", json=new_url,
        headers={"X-CSRF-Token": payload["csrf_token"]},
    )

    assert response.status_code == 200, response.text
    assert models.jellyfin_config()[0] == "http://jellyfin2.local:8096"


def test_reconfigure_refused_for_manage_settings_without_manage_users(client, admin_credentials, jellyfin):
    """The route-level dependency accepts MANAGE_SETTINGS *or* MANAGE_USERS,
    but once the panel is connected, reconfiguring it needs MANAGE_USERS
    specifically. This proves the stricter in-handler check, which the
    generic permission sweep in test_permissions.py cannot see."""
    do_setup(client, admin_credentials)
    client.cookies.clear()
    make_user("settings-only", "jf-settings-only", int(Permission.MANAGE_SETTINGS))
    csrf = session_for(client, models.get_user_by_jellyfin_id("jf-settings-only").id)

    response = client.post(
        "/api/auth/jellyfin-connect",
        json={"url": "http://jellyfin2.local:8096", "username": "admin", "password": "adminpw"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 403
    assert models.jellyfin_config()[0] == admin_credentials["url"]


def test_reconfigure_matches_existing_user_instead_of_duplicating(client, admin_credentials, jellyfin):
    """Reconnecting the same Jellyfin admin (e.g. after rotating their
    password) must touch the existing panel user, not create a second one."""
    payload = do_setup(client, admin_credentials)
    before = models.count_users()

    response = client.post(
        "/api/auth/jellyfin-connect",
        json={"url": "http://jellyfin2.local:8096", "username": "admin", "password": "adminpw"},
        headers={"X-CSRF-Token": payload["csrf_token"]},
    )

    assert response.status_code == 200, response.text
    assert models.count_users() == before
