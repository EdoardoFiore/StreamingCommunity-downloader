"""First-run bootstrap: it must happen exactly once, and only for a Jellyfin admin."""

from app.auth import models
from app.auth.permissions import ALL_PERMISSIONS, Permission
from tests.conftest import do_setup


def test_status_reports_pending_setup(client):
    body = client.get("/api/auth/status").json()
    assert body["setup_done"] is False
    assert body["jellyfin_url"] is None


def test_setup_creates_admin_with_every_permission(client, admin_credentials, jellyfin):
    payload = do_setup(client, admin_credentials)

    user = payload["user"]
    assert user["username"] == "admin"
    assert user["permissions"] == int(ALL_PERMISSIONS)
    assert user["is_jellyfin_admin"] is True
    assert payload["csrf_token"]

    stored = models.get_user_by_jellyfin_id("jf-admin-id")
    assert stored is not None and stored.enabled


def test_setup_creates_its_own_api_key(client, admin_credentials, jellyfin):
    """The panel must be able to query Jellyfin without anyone pasting a key."""
    do_setup(client, admin_credentials)

    assert [k["AppName"] for k in jellyfin.api_keys] == ["SC Panel"]
    _, api_key = models.jellyfin_config()
    assert api_key == jellyfin.api_keys[0]["AccessToken"]


def test_setup_marks_status_done(client, admin_credentials):
    do_setup(client, admin_credentials)
    body = client.get("/api/auth/status").json()
    assert body["setup_done"] is True
    assert body["jellyfin_url"] == "http://jellyfin.local:8096"


def test_setup_refused_for_non_administrator(client, jellyfin):
    jellyfin.add_user("bob", "bobpw", admin=False)

    response = client.post(
        "/api/auth/setup",
        json={"url": "http://jellyfin.local:8096", "username": "bob", "password": "bobpw"},
    )

    assert response.status_code == 403
    assert models.count_users() == 0
    assert models.setup_done() is False


def test_setup_refused_with_bad_credentials(client, jellyfin):
    jellyfin.add_user("admin", "adminpw", admin=True)

    response = client.post(
        "/api/auth/setup",
        json={"url": "http://jellyfin.local:8096", "username": "admin", "password": "wrong"},
    )

    assert response.status_code == 401
    assert models.count_users() == 0


def test_setup_cannot_run_twice(client, admin_credentials, jellyfin):
    do_setup(client, admin_credentials)
    jellyfin.add_user("admin2", "pw2", admin=True)

    response = client.post(
        "/api/auth/setup",
        json={"url": "http://jellyfin.local:8096", "username": "admin2", "password": "pw2"},
    )

    assert response.status_code == 403
    assert models.count_users() == 1


def test_setup_rejects_unreachable_server(client, jellyfin):
    jellyfin.reachable = False

    response = client.post(
        "/api/auth/setup",
        json={"url": "http://jellyfin.local:8096", "username": "admin", "password": "pw"},
    )

    assert response.status_code == 400
    assert models.count_users() == 0


def test_setup_rejects_invalid_url(client, jellyfin):
    response = client.post(
        "/api/auth/setup",
        json={"url": "ftp://jellyfin.local", "username": "admin", "password": "pw"},
    )
    assert response.status_code == 400


def test_bootstrap_admin_can_reach_a_guarded_page(client, admin_credentials):
    do_setup(client, admin_credentials)
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert Permission.MANAGE_REQUESTS.name in me.json()["user"]["permission_names"]
