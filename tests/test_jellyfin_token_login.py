"""POST /api/auth/jellyfin-token — trading an already-issued Jellyfin access
token for a panel session, used by the Jellyfin custom-tab embed so a
visitor already signed into Jellyfin does not have to log in a second time.
"""

from app.auth.jellyfin import JellyfinClient
from tests.conftest import ALL, do_setup, make_user


def _mint_token(jellyfin, url: str, username: str, password: str) -> str:
    """A token minted directly against the fake server, bypassing the panel's
    own /api/auth/jellyfin — which would immediately invalidate it. Stands in
    for "the browser's own, already-open Jellyfin session token"."""
    account = JellyfinClient(url).authenticate(username, password)
    return account["AccessToken"]


def test_token_exchange_creates_a_panel_session(client, jellyfin, admin_credentials):
    do_setup(client, admin_credentials)
    jellyfin.add_user("alice", "alicepw", user_id="jf-alice-id")
    make_user("alice", "jf-alice-id", ALL)

    token = _mint_token(jellyfin, admin_credentials["url"], "alice", "alicepw")

    response = client.post("/api/auth/jellyfin-token", json={"token": token})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["username"] == "alice"
    assert "sc_session" in response.cookies


def test_token_exchange_does_not_log_the_user_out_of_jellyfin(client, jellyfin, admin_credentials):
    """The token belongs to the caller's own Jellyfin session — unlike a
    password login, exchanging it must not invalidate it."""
    do_setup(client, admin_credentials)
    jellyfin.add_user("alice", "alicepw", user_id="jf-alice-id")
    make_user("alice", "jf-alice-id", ALL)

    token = _mint_token(jellyfin, admin_credentials["url"], "alice", "alicepw")
    client.post("/api/auth/jellyfin-token", json={"token": token})

    assert token not in jellyfin.revoked_tokens
    assert token in jellyfin.issued_tokens


def test_token_exchange_rejects_an_invalid_token(client, jellyfin, admin_credentials):
    do_setup(client, admin_credentials)
    response = client.post("/api/auth/jellyfin-token", json={"token": "garbage"})
    assert response.status_code == 401


def test_token_exchange_refuses_an_unimported_user_by_default(client, jellyfin, admin_credentials):
    do_setup(client, admin_credentials)
    jellyfin.add_user("bob", "bobpw", user_id="jf-bob-id")
    token = _mint_token(jellyfin, admin_credentials["url"], "bob", "bobpw")

    response = client.post("/api/auth/jellyfin-token", json={"token": token})
    assert response.status_code == 403


def test_token_exchange_refuses_a_disabled_user(client, jellyfin, admin_credentials):
    do_setup(client, admin_credentials)
    jellyfin.add_user("alice", "alicepw", user_id="jf-alice-id")
    make_user("alice", "jf-alice-id", ALL, enabled=False)
    token = _mint_token(jellyfin, admin_credentials["url"], "alice", "alicepw")

    response = client.post("/api/auth/jellyfin-token", json={"token": token})
    assert response.status_code == 403


def test_token_exchange_requires_setup_done(client):
    response = client.post("/api/auth/jellyfin-token", json={"token": "whatever"})
    assert response.status_code == 409


def test_token_exchange_disabled_when_auth_disabled(client, monkeypatch):
    from app.auth import router as auth_router

    monkeypatch.setattr(auth_router, "AUTH_ENABLED", False)
    response = client.post("/api/auth/jellyfin-token", json={"token": "whatever"})
    assert response.status_code == 404
