"""Importing Jellyfin accounts, assigning permissions, disabling users."""

from app.auth import models, session as sessions
from app.auth.permissions import ALL_PERMISSIONS, Permission
from tests.conftest import do_login, do_setup, make_user, session_for


def _as_manager(client, permissions=Permission.MANAGE_USERS, username="manager"):
    user = make_user(username, f"jf-{username}-id", int(permissions))
    return user, session_for(client, user.id)


def test_jellyfin_users_are_listed_with_their_import_state(client, admin_credentials, jellyfin):
    do_setup(client, admin_credentials)
    jellyfin.add_user("bob", "bobpw")
    jellyfin.add_user("carol", "carolpw")
    make_user("bob", "jf-bob-id", int(Permission.REQUEST))

    listing = {u["username"]: u for u in client.get("/api/users/jellyfin").json()}

    assert listing["admin"]["imported"] is True
    assert listing["bob"]["imported"] is True
    assert listing["bob"]["panel_user"]["permission_names"] == ["REQUEST"]
    assert listing["carol"]["imported"] is False
    assert listing["carol"]["panel_user"] is None


def test_import_grants_only_the_requested_permissions(client, admin_credentials, jellyfin):
    do_setup(client, admin_credentials)
    jellyfin.add_user("carol", "carolpw")
    csrf = client.get("/api/auth/me").json()["csrf_token"]

    response = client.post(
        "/api/users/import",
        json={"jellyfin_user_ids": ["jf-carol-id"], "permissions": int(Permission.REQUEST)},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 201
    carol = models.get_user_by_jellyfin_id("jf-carol-id")
    assert carol.permissions == int(Permission.REQUEST)
    assert carol.enabled


def test_import_uses_the_default_permissions_when_unspecified(client, admin_credentials, jellyfin):
    do_setup(client, admin_credentials)
    jellyfin.add_user("carol", "carolpw")
    csrf = client.get("/api/auth/me").json()["csrf_token"]

    client.post(
        "/api/users/import",
        json={"jellyfin_user_ids": ["jf-carol-id"]},
        headers={"X-CSRF-Token": csrf},
    )

    carol = models.get_user_by_jellyfin_id("jf-carol-id")
    assert carol.permissions == models.default_permissions()


def test_import_refuses_ids_that_are_not_on_jellyfin(client, admin_credentials, jellyfin):
    """Identity and display names come from Jellyfin, never from the request body."""
    do_setup(client, admin_credentials)
    csrf = client.get("/api/auth/me").json()["csrf_token"]

    response = client.post(
        "/api/users/import",
        json={"jellyfin_user_ids": ["made-up-id"]},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400
    assert models.count_users() == 1


def test_import_is_idempotent(client, admin_credentials, jellyfin):
    do_setup(client, admin_credentials)
    jellyfin.add_user("carol", "carolpw")
    csrf = client.get("/api/auth/me").json()["csrf_token"]
    body = {"jellyfin_user_ids": ["jf-carol-id"]}

    client.post("/api/users/import", json=body, headers={"X-CSRF-Token": csrf})
    second = client.post("/api/users/import", json=body, headers={"X-CSRF-Token": csrf})

    assert second.json()["already_present"] == ["jf-carol-id"]
    assert models.count_users() == 2


def test_imported_user_can_then_sign_in(client, admin_credentials, jellyfin):
    do_setup(client, admin_credentials)
    jellyfin.add_user("carol", "carolpw")
    csrf = client.get("/api/auth/me").json()["csrf_token"]
    client.post(
        "/api/users/import",
        json={"jellyfin_user_ids": ["jf-carol-id"]},
        headers={"X-CSRF-Token": csrf},
    )
    client.cookies.clear()

    assert do_login(client, "carol", "carolpw").status_code == 200


# ── Permission changes and disabling ───────────────────────────────────────────

def test_permission_change_takes_effect_on_the_next_request(client, admin_credentials):
    do_setup(client, admin_credentials)
    admin_csrf = client.get("/api/auth/me").json()["csrf_token"]
    bob = make_user("bob", "jf-bob-id", int(Permission.REQUEST))

    client.patch(
        f"/api/users/{bob.id}",
        json={"permissions": int(Permission.REQUEST | Permission.DOWNLOAD)},
        headers={"X-CSRF-Token": admin_csrf},
    )

    client.cookies.clear()
    session_for(client, bob.id)
    assert models.get_user(bob.id).has(Permission.DOWNLOAD)
    assert client.get("/api/files").status_code == 403  # unrelated bit unaffected


def test_disabling_a_user_is_immediate_and_keeps_their_row(client, admin_credentials):
    do_setup(client, admin_credentials)
    admin_csrf = client.get("/api/auth/me").json()["csrf_token"]
    bob = make_user("bob", "jf-bob-id", int(ALL_PERMISSIONS))
    bob_token, _ = sessions.create_session(bob.id)

    response = client.patch(
        f"/api/users/{bob.id}", json={"enabled": False}, headers={"X-CSRF-Token": admin_csrf}
    )

    assert response.status_code == 200
    assert models.get_user(bob.id) is not None  # history preserved
    assert sessions.resolve_session(bob_token) is None


def test_disabled_user_cannot_use_an_existing_session(client, admin_credentials):
    do_setup(client, admin_credentials)
    bob = make_user("bob", "jf-bob-id", int(ALL_PERMISSIONS))
    client.cookies.clear()
    session_for(client, bob.id)
    assert client.get("/api/auth/me").status_code == 200

    models.set_enabled(bob.id, False)

    assert client.get("/api/auth/me").status_code == 401


def test_cannot_disable_the_last_user_manager(client, admin_credentials):
    do_setup(client, admin_credentials)
    csrf = client.get("/api/auth/me").json()["csrf_token"]
    admin = models.get_user_by_jellyfin_id("jf-admin-id")

    response = client.patch(
        f"/api/users/{admin.id}", json={"enabled": False}, headers={"X-CSRF-Token": csrf}
    )

    assert response.status_code == 409
    assert models.get_user(admin.id).enabled


def test_cannot_strip_manage_users_from_the_last_user_manager(client, admin_credentials):
    do_setup(client, admin_credentials)
    csrf = client.get("/api/auth/me").json()["csrf_token"]
    admin = models.get_user_by_jellyfin_id("jf-admin-id")

    response = client.patch(
        f"/api/users/{admin.id}",
        json={"permissions": int(Permission.REQUEST)},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    assert models.get_user(admin.id).has(Permission.MANAGE_USERS)


def test_can_step_down_once_someone_else_can_manage_users(client, admin_credentials):
    do_setup(client, admin_credentials)
    csrf = client.get("/api/auth/me").json()["csrf_token"]
    admin = models.get_user_by_jellyfin_id("jf-admin-id")
    make_user("deputy", "jf-deputy-id", int(Permission.MANAGE_USERS))

    response = client.patch(
        f"/api/users/{admin.id}",
        json={"permissions": int(Permission.REQUEST)},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200


def test_revoking_sessions_signs_a_user_out_without_disabling_them(client, admin_credentials):
    do_setup(client, admin_credentials)
    csrf = client.get("/api/auth/me").json()["csrf_token"]
    bob = make_user("bob", "jf-bob-id", int(ALL_PERMISSIONS))
    bob_token, _ = sessions.create_session(bob.id)

    client.post(f"/api/users/{bob.id}/sessions/revoke", headers={"X-CSRF-Token": csrf})

    assert sessions.resolve_session(bob_token) is None
    assert models.get_user(bob.id).enabled


# ── Open sign-in switch ────────────────────────────────────────────────────────

def test_open_signin_is_off_by_default(client, admin_credentials):
    do_setup(client, admin_credentials)
    assert client.get("/api/users/settings").json()["allow_new_jellyfin_login"] is False


def test_open_signin_can_be_toggled(client, admin_credentials, jellyfin):
    do_setup(client, admin_credentials)
    csrf = client.get("/api/auth/me").json()["csrf_token"]
    jellyfin.add_user("dave", "davepw")

    client.put(
        "/api/users/settings",
        json={"allow_new_jellyfin_login": True, "default_permissions": int(Permission.REQUEST)},
        headers={"X-CSRF-Token": csrf},
    )
    client.cookies.clear()

    assert do_login(client, "dave", "davepw").status_code == 200
    assert models.get_user_by_jellyfin_id("jf-dave-id").permissions == int(Permission.REQUEST)


# ── Authorisation on this router ───────────────────────────────────────────────

def test_user_management_requires_the_permission(client, admin_credentials):
    do_setup(client, admin_credentials)
    client.cookies.clear()
    bob, csrf = _as_manager(client, ALL_PERMISSIONS & ~Permission.MANAGE_USERS, "bob")

    assert client.get("/api/users").status_code == 403
    assert client.get("/api/users/jellyfin").status_code == 403
    assert client.post(
        "/api/users/import",
        json={"jellyfin_user_ids": ["x"]},
        headers={"X-CSRF-Token": csrf},
    ).status_code == 403
    assert client.patch(
        f"/api/users/{bob.id}", json={"enabled": False}, headers={"X-CSRF-Token": csrf}
    ).status_code == 403


def test_manage_users_alone_is_enough(client, admin_credentials):
    do_setup(client, admin_credentials)
    client.cookies.clear()
    _as_manager(client, Permission.MANAGE_USERS)
    assert client.get("/api/users").status_code == 200
