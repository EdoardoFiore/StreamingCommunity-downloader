"""Login paths that must fail, and the Jellyfin-facing guarantees of the ones that don't."""

from app.auth import models, session as sessions
from app.auth.jellyfin import device_id_for
from tests.conftest import do_login, do_setup


def _import(jellyfin_user_id: str, username: str, enabled: bool = True):
    return models.create_user(
        jellyfin_user_id=jellyfin_user_id,
        username=username,
        permissions=int(models.perms.DEFAULT_PERMISSIONS),
        enabled=enabled,
    )


# ── Failing paths ──────────────────────────────────────────────────────────────

def test_login_before_setup_is_refused(client, jellyfin):
    jellyfin.add_user("bob", "bobpw")
    assert do_login(client, "bob", "bobpw").status_code == 409


def test_login_with_wrong_password(client, admin_credentials, jellyfin):
    do_setup(client, admin_credentials)
    jellyfin.add_user("bob", "bobpw")
    _import("jf-bob-id", "bob")

    response = do_login(client, "bob", "not-the-password")

    assert response.status_code == 401
    assert sessions.db.query("SELECT * FROM jf_session WHERE user_id != 1") == []


def test_login_with_unknown_username(client, admin_credentials):
    do_setup(client, admin_credentials)
    assert do_login(client, "ghost", "whatever").status_code == 401


def test_unimported_jellyfin_user_is_refused(client, admin_credentials, jellyfin):
    """A valid Jellyfin account is not enough: it has to be imported first."""
    do_setup(client, admin_credentials)
    jellyfin.add_user("carol", "carolpw")

    response = do_login(client, "carol", "carolpw")

    assert response.status_code == 403
    assert models.get_user_by_jellyfin_id("jf-carol-id") is None


def test_disabled_user_cannot_log_in(client, admin_credentials, jellyfin):
    do_setup(client, admin_credentials)
    jellyfin.add_user("bob", "bobpw")
    _import("jf-bob-id", "bob", enabled=False)

    assert do_login(client, "bob", "bobpw").status_code == 403


def test_open_signup_is_off_by_default_and_can_be_enabled(client, admin_credentials, jellyfin):
    do_setup(client, admin_credentials)
    jellyfin.add_user("dave", "davepw")

    assert do_login(client, "dave", "davepw").status_code == 403

    models.set_setting(models.SETTING_ALLOW_NEW_LOGIN, "1")
    response = do_login(client, "dave", "davepw")

    assert response.status_code == 200
    created = models.get_user_by_jellyfin_id("jf-dave-id")
    assert created.permissions == models.default_permissions()


def test_unreachable_jellyfin_reports_bad_gateway(client, admin_credentials, jellyfin):
    do_setup(client, admin_credentials)
    jellyfin.reachable = False
    assert do_login(client, "admin", "adminpw").status_code == 502


# ── Guarantees on the happy path ───────────────────────────────────────────────

def test_login_matches_on_jellyfin_id_not_username(client, admin_credentials, jellyfin):
    """A rename on Jellyfin must follow the same panel account, not create one."""
    do_setup(client, admin_credentials)
    jellyfin.add_user("bob", "bobpw", user_id="stable-id")
    imported = _import("stable-id", "bob")

    jellyfin.rename_user("bob", "roberto")
    response = do_login(client, "roberto", "bobpw")

    assert response.status_code == 200
    assert response.json()["user"]["id"] == imported.id
    assert models.count_users() == 2  # the admin plus this one, no duplicate
    assert models.get_user(imported.id).username == "roberto"


def test_user_jellyfin_token_is_never_stored(client, admin_credentials, jellyfin):
    """Only the service API key is kept; user tokens are invalidated immediately."""
    do_setup(client, admin_credentials)
    jellyfin.add_user("bob", "bobpw")
    _import("jf-bob-id", "bob")

    do_login(client, "bob", "bobpw")

    assert jellyfin.issued_tokens <= jellyfin.revoked_tokens
    columns = {
        row["name"]
        for row in sessions.db.query("PRAGMA table_info(jf_user)")
    }
    assert not any("token" in c for c in columns)


def test_login_sets_an_httponly_session_cookie(client, admin_credentials, jellyfin):
    do_setup(client, admin_credentials)
    jellyfin.add_user("bob", "bobpw")
    _import("jf-bob-id", "bob")

    response = do_login(client, "bob", "bobpw")

    cookie = response.headers["set-cookie"]
    assert sessions.SESSION_COOKIE in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.lower().replace("samesite=lax", "SameSite=lax")


def test_session_token_is_stored_hashed(client, admin_credentials, jellyfin):
    do_setup(client, admin_credentials)
    jellyfin.add_user("bob", "bobpw")
    _import("jf-bob-id", "bob")

    do_login(client, "bob", "bobpw")
    raw = client.cookies.get(sessions.SESSION_COOKIE)

    stored = [r["token_hash"] for r in sessions.db.query("SELECT token_hash FROM jf_session")]
    assert raw not in stored
    assert sessions._hash(raw) in stored


def test_logout_deletes_the_session(client, admin_credentials, jellyfin):
    do_setup(client, admin_credentials)
    csrf = client.get("/api/auth/me").json()["csrf_token"]

    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert sessions.db.query("SELECT * FROM jf_session") == []
    assert client.get("/api/auth/me").status_code == 401


# ── DeviceId ───────────────────────────────────────────────────────────────────

def test_device_id_is_stable_across_logins(client, admin_credentials, jellyfin):
    """An unstable DeviceId fills the Jellyfin dashboard with phantom devices."""
    do_setup(client, admin_credentials)
    jellyfin.add_user("bob", "bobpw")
    _import("jf-bob-id", "bob")

    for _ in range(3):
        assert do_login(client, "bob", "bobpw").status_code == 200

    seen = [
        call["device_id"]
        for call in jellyfin.calls
        if call["path"] == "/Users/AuthenticateByName" and call["json"]["Username"] == "bob"
    ]
    assert len(seen) == 3
    assert len(set(seen)) == 1
    assert seen[0] == device_id_for("bob")


def test_device_id_ignores_username_casing(client, admin_credentials, jellyfin):
    do_setup(client, admin_credentials)
    jellyfin.add_user("Bob", "bobpw")
    _import("jf-Bob-id", "Bob")

    do_login(client, "Bob", "bobpw")
    assert device_id_for("bob") == device_id_for("BOB") == device_id_for(" Bob ")


def test_users_get_distinct_device_ids(client, admin_credentials, jellyfin):
    """Jellyfin keys sessions by DeviceId; a shared one would evict other users."""
    assert device_id_for("alice") != device_id_for("bob")


# ── X-Forwarded-For ────────────────────────────────────────────────────────────

def test_real_client_ip_is_forwarded_to_jellyfin(client, admin_credentials, jellyfin):
    do_setup(client, admin_credentials)
    auth_calls = [c for c in jellyfin.calls if c["path"] == "/Users/AuthenticateByName"]
    assert auth_calls[0]["forwarded_for"] == "testclient"


def test_login_still_works_when_jellyfin_rejects_the_forwarded_ip(
    client, admin_credentials, jellyfin
):
    """Jellyfin refuses X-Forwarded-For from an unknown proxy; the retry covers it."""
    jellyfin.reject_forwarded_ip = True

    response = client.post("/api/auth/setup", json=admin_credentials)

    assert response.status_code == 200
    auth_calls = [c for c in jellyfin.calls if c["path"] == "/Users/AuthenticateByName"]
    assert auth_calls[0]["forwarded_for"] == "testclient"
    assert auth_calls[1]["forwarded_for"] is None
