"""Authorisation: every permission is enforced server-side, on every API route.

The sweep test at the bottom is the point of this file: it fails when a new
endpoint is added without deciding what may reach it, rather than trusting each
endpoint to have remembered a guard.
"""

import pytest
from fastapi.routing import APIRoute

from app.auth.deps import PUBLIC_PATHS, SESSION_ONLY_PATHS
from app.auth.permissions import ALL_PERMISSIONS, Permission
from tests.conftest import do_setup, make_user, session_for


def _as(client, permissions: Permission | int, username="user"):
    """Sign ``client`` in as a user holding exactly ``permissions``."""
    user = make_user(username, f"jf-{username}-id", int(permissions))
    return user, session_for(client, user.id)


# ── Endpoint coverage ──────────────────────────────────────────────────────────
#
# (granting permissions, method, path, body). Some endpoints accept any of
# several permissions — searching serves both the download and the request flow
# — so the denied case has to strip every alternative, not just one.
CASES = [
    ((Permission.DOWNLOAD,), "POST", "/api/download/film",
     {"id": 1, "title": "x", "domain": "example.test"}),
    ((Permission.DOWNLOAD,), "POST", "/api/download/season",
     {"tv_id": 1, "slug": "x", "tv_name": "x", "season": 1}),
    ((Permission.DOWNLOAD,), "POST", "/api/download/series",
     {"tv_id": 1, "slug": "x", "tv_name": "x"}),
    ((Permission.DOWNLOAD,), "POST", "/api/download/anime-all",
     {"anime_id": "1", "anime_name": "x"}),
    ((Permission.REQUEST, Permission.DOWNLOAD), "GET",
     "/api/search?q=abc&domain=example.test", None),
    ((Permission.MANAGE_SETTINGS,), "GET", "/api/domain/settings", None),
    ((Permission.MANAGE_SETTINGS,), "GET", "/api/download-hooks", None),
    ((Permission.MANAGE_SETTINGS,), "GET", "/api/domain/settings/naming-defaults", None),
    ((Permission.MANAGE_SETTINGS,), "POST", "/api/domain/settings/naming-preview",
     {"templates": {"film_file": "{title}"}}),
    ((Permission.MANAGE_SETTINGS,), "POST", "/api/download-hooks",
     {"name": "x", "url": "http://x/y"}),
    ((Permission.REQUEST, Permission.DOWNLOAD), "GET", "/api/metadata/tv/1", None),
    ((Permission.MANAGE_SETTINGS,), "POST", "/api/domain/check", None),
    ((Permission.MANAGE_SETTINGS,), "POST", "/api/domain/candidate/apply",
     {"domain": "example.test"}),
    ((Permission.MANAGE_SETTINGS,), "POST", "/api/domain/candidate/dismiss", None),
    ((Permission.REQUEST, Permission.DOWNLOAD, Permission.MANAGE_SETTINGS), "GET",
     "/api/domain/candidate", None),
    ((Permission.MANAGE_FILES,), "POST", "/api/files/rename",
     {"path": "nope", "new_name": "other"}),
    ((Permission.VIEW_LIBRARY,), "GET", "/api/files", None),
    ((Permission.MANAGE_REQUESTS, Permission.DOWNLOAD), "POST",
     "/api/download/does-not-exist/fire", None),
    ((Permission.MANAGE_USERS,), "GET", "/api/users", None),
]


def _case_id(case):
    granting, method, path, _ = case
    return f"{'|'.join(p.name for p in granting)}-{method}-{path.split('?')[0]}"


def _union(permissions) -> Permission:
    total = Permission.NONE
    for permission in permissions:
        total |= permission
    return total


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_endpoint_is_denied_without_any_granting_permission(client, admin_credentials, case):
    granting, method, path, body = case
    do_setup(client, admin_credentials)
    client.cookies.clear()
    _, csrf = _as(client, ALL_PERMISSIONS & ~_union(granting))

    response = client.request(method, path, json=body, headers={"X-CSRF-Token": csrf})

    assert response.status_code == 403, f"{method} {path} reachable without {_case_id(case)}"


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_each_granting_permission_opens_the_endpoint_on_its_own(
    client, admin_credentials, case, stub_jobs
):
    granting, method, path, body = case
    do_setup(client, admin_credentials)

    for index, permission in enumerate(granting):
        client.cookies.clear()
        _, csrf = _as(client, permission, username=f"user{index}-{permission.name}")

        response = client.request(method, path, json=body, headers={"X-CSRF-Token": csrf})

        # The call may still fail on its own merits (no such job, unreachable
        # source); what matters is that authorisation is not what stopped it.
        assert response.status_code != 403, f"{method} {path} denied despite {permission.name}"


def test_a_user_with_no_permissions_can_do_nothing(client, admin_credentials):
    do_setup(client, admin_credentials)
    client.cookies.clear()
    _, csrf = _as(client, Permission.NONE)

    for _, method, path, body in CASES:
        response = client.request(method, path, json=body, headers={"X-CSRF-Token": csrf})
        assert response.status_code == 403, f"{method} {path}"


def test_permissions_are_independent_not_hierarchical(client, admin_credentials):
    """An administrator of settings and users need not see the request queue."""
    do_setup(client, admin_credentials)
    client.cookies.clear()
    _, csrf = _as(client, Permission.MANAGE_SETTINGS | Permission.MANAGE_USERS)

    assert client.get("/api/domain/settings").status_code == 200
    assert client.post(
        "/api/download/x/fire", headers={"X-CSRF-Token": csrf}
    ).status_code == 403


def test_unknown_permission_bits_are_dropped(client, admin_credentials):
    do_setup(client, admin_credentials)
    client.cookies.clear()
    user, _ = _as(client, int(ALL_PERMISSIONS) | (1 << 30))

    assert user.permissions == int(ALL_PERMISSIONS)


# ── Systematic sweep ───────────────────────────────────────────────────────────

def _all_routes():
    """Every routed endpoint, with its effective path and merged dependencies.

    FastAPI no longer flattens ``include_router`` into ``app.routes`` — an
    included router appears as one opaque entry — so walking ``app.routes``
    alone sees only what was declared on the app itself, and any sweep over it
    silently passes.
    """
    from app.main import app

    routes = []
    for route in app.routes:
        effective = getattr(route, "effective_route_contexts", None)
        if callable(effective):
            for context in effective():
                routes.append((context.path, context.methods, context.dependant))
        elif isinstance(route, APIRoute):
            routes.append((route.path, route.methods, route.dependant))
    return routes


def _permission_guards(dependant) -> list[tuple]:
    """Permission requirements on a route, at any dependency depth."""
    found = []
    stack = list(dependant.dependencies)
    while stack:
        dependency = stack.pop()
        required = getattr(dependency.call, "__required_permissions__", None)
        if required is not None:
            found.append(required)
        stack.extend(dependency.dependencies)
    return found


def test_the_route_sweep_actually_sees_the_routers():
    """Guard against the sweep below passing because it found nothing to check."""
    paths = {path for path, _, _ in _all_routes()}
    assert "/api/download/film" in paths
    assert "/api/users" in paths
    assert len([p for p in paths if p.startswith("/api/")]) > 20


def test_every_api_route_is_classified():
    """No /api route may exist without an explicit decision about who reaches it.

    A new endpoint has to be either public, explicitly session-only, or guarded
    by a permission — otherwise this fails. Hiding a button is not authorisation.
    """
    unclassified = []
    for path, methods, dependant in _all_routes():
        if not path.startswith("/api/"):
            continue
        if path in PUBLIC_PATHS or path in SESSION_ONLY_PATHS:
            continue
        if not _permission_guards(dependant):
            unclassified.append(f"{sorted(methods)} {path}")

    assert not unclassified, (
        "These API routes carry no permission guard and are not in the public or "
        "session-only allowlists:\n  " + "\n  ".join(sorted(unclassified))
    )


def test_every_permission_is_enforced_somewhere():
    """A permission nobody checks is a permission that does not exist."""
    enforced = set()
    for _, _, dependant in _all_routes():
        for required in _permission_guards(dependant):
            enforced.update(required)

    defined = {p for p in Permission if p is not Permission.NONE}
    assert defined - enforced == set(), (
        "These permissions are never checked on any route: "
        + ", ".join(sorted(p.name for p in defined - enforced))
    )


def test_public_allowlist_stays_small():
    """The allowlist is security-critical; growth should be deliberate."""
    assert PUBLIC_PATHS == {
        "/login",
        "/favicon.ico",
        "/api/auth/status",
        "/api/auth/setup",
        "/api/auth/skip",
        "/api/auth/jellyfin-token",
        "/api/auth/jellyfin",
    }
    assert SESSION_ONLY_PATHS == {"/api/auth/me", "/api/auth/logout"}
