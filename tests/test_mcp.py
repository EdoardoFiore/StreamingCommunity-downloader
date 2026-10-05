"""The MCP endpoint: who may reach it, and who its tools act as.

Driven over real JSON-RPC through the whole app — AuthMiddleware included —
rather than by calling tool functions, because the two things most worth
pinning live on that path: the bearer gate, and the acting user reaching the
same route handlers the browser uses.
"""

import asyncio
import json

import httpx
import pytest

from app import config
from app.auth import models
from app.auth.deps import OPEN_MODE_USER
from app.auth.permissions import Permission
from app.core import tv
from app.mcp import auth as mcp_auth, server as mcp_http
from app.requests import models as request_models
from app.watches import models as watch_models
from tests.conftest import enable_open_mode, make_user, session_for


def _enable(on: bool = True):
    config.save_settings({**config.get_settings(), "mcp_enabled": on})


def _jellyfin_mode():
    models.set_setting(models.SETTING_AUTH_MODE, "jellyfin")


def _send(token: str | None, *calls, headers: dict | None = None, query: str = ""):
    """POST each ``(method, params)`` to /mcp; return the httpx responses."""
    from app.main import app

    async def run():
        async with mcp_http.running():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://panel") as c:
                sent = {"Accept": "application/json, text/event-stream", **(headers or {})}
                if token is not None:
                    sent["Authorization"] = f"Bearer {token}"
                return [
                    await c.post(f"/mcp{query}", headers=sent,
                                 json={"jsonrpc": "2.0", "id": i, "method": m, "params": p})
                    for i, (m, p) in enumerate(calls, 1)
                ]

    return asyncio.run(run())


def _tool(token: str, name: str, **arguments) -> tuple[bool, object]:
    """Call one tool; return ``(is_error, decoded payload or message)``."""
    (response,) = _send(token, ("tools/call", {"name": name, "arguments": arguments}))
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    text = result["content"][0]["text"]
    try:
        return result["isError"], json.loads(text)
    except ValueError:
        return result["isError"], text


# ── The gate ───────────────────────────────────────────────────────────────────

def test_switched_off_answers_404_even_with_a_valid_token(client):
    enable_open_mode()
    token = mcp_auth.generate_token(OPEN_MODE_USER)
    (response,) = _send(token, ("tools/list", {}))
    assert response.status_code == 404


def test_no_stored_token_refuses_instead_of_opening_up(client):
    enable_open_mode()
    _enable()
    (no_header,) = _send(None, ("tools/list", {}))
    (any_token,) = _send("anything", ("tools/list", {}))
    assert no_header.status_code == any_token.status_code == 401


def test_wrong_token_and_query_string_token_are_refused(client):
    enable_open_mode()
    _enable()
    token = mcp_auth.generate_token(OPEN_MODE_USER)
    (wrong,) = _send("not-it", ("tools/list", {}))
    (in_query,) = _send(None, ("tools/list", {}), query=f"?token={token}")
    assert wrong.status_code == in_query.status_code == 401
    assert wrong.headers["www-authenticate"] == "Bearer"


def test_token_is_stored_hashed(client):
    enable_open_mode()
    token = mcp_auth.generate_token(OPEN_MODE_USER)
    stored = models.get_setting(mcp_auth.SETTING_TOKEN_HASH)
    assert stored and token not in stored


def test_lists_tools(client):
    enable_open_mode()
    _enable()
    token = mcp_auth.generate_token(OPEN_MODE_USER)
    (response,) = _send(token, ("tools/list", {}))
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert {"search_content", "download_film", "follow_series", "submit_request",
            "approve_request", "list_downloads"} <= names


# ── Who the agent is ───────────────────────────────────────────────────────────

def test_owner_disabled_or_gone_stops_the_token(client):
    _jellyfin_mode()
    _enable()
    owner = make_user("ada", "jf-ada", int(Permission.DOWNLOAD))
    token = mcp_auth.generate_token(owner)
    assert _send(token, ("tools/list", {}))[0].status_code == 200

    models.set_enabled(owner.id, False)
    assert _send(token, ("tools/list", {}))[0].status_code == 401


def test_open_mode_token_dies_with_the_switch_to_jellyfin(client):
    enable_open_mode()
    _enable()
    token = mcp_auth.generate_token(OPEN_MODE_USER)
    _jellyfin_mode()
    assert _send(token, ("tools/list", {}))[0].status_code == 401


def test_tools_use_the_owners_live_permissions(client, stub_jobs):
    _jellyfin_mode()
    _enable()
    owner = make_user("bob", "jf-bob", int(Permission.REQUEST))
    token = mcp_auth.generate_token(owner)

    is_error, message = _tool(token, "download_film", film_id=7, title="Film")
    assert is_error and "DOWNLOAD" in message
    assert stub_jobs == []

    models.set_permissions(owner.id, int(Permission.DOWNLOAD))
    is_error, body = _tool(token, "download_film", film_id=7, title="Film")
    assert not is_error and body["status"] == "queued"
    (name, _, kwargs), = stub_jobs
    assert name == "submit_film" and kwargs["user_id"] == owner.id


def test_follow_that_cannot_read_the_source_is_rolled_back(client, source, monkeypatch):
    """The baseline is what stops a follow from fetching the back catalogue."""
    _jellyfin_mode()
    _enable()
    monkeypatch.setattr(tv, "get_info_tv", lambda *a, **k: 1)
    owner = make_user("cy", "jf-cy", int(Permission.DOWNLOAD))
    token = mcp_auth.generate_token(owner)
    source.dead = True

    is_error, _ = _tool(token, "follow_series", source="streamingcommunity",
                        external_id="42", title="Serie", slug="serie")
    assert is_error
    assert watch_models.find_open("streamingcommunity", "tv", "42") is None


def test_follow_belongs_to_the_owner_and_seeds_what_is_out(client, source, monkeypatch):
    _jellyfin_mode()
    _enable()
    monkeypatch.setattr(tv, "get_info_tv", lambda *a, **k: 1)
    owner = make_user("di", "jf-di", int(Permission.REQUEST))
    token = mcp_auth.generate_token(owner)

    is_error, _ = _tool(token, "follow_series", source="streamingcommunity",
                        external_id="42", title="Serie", slug="serie")
    assert not is_error
    watch = watch_models.find_open("streamingcommunity", "tv", "42")
    assert watch.created_by == owner.id
    assert len(watch_models.seen_keys(watch.id)) == len(source.episodes)


def test_request_is_filed_by_the_owner_not_by_an_admin(client, source):
    _jellyfin_mode()
    _enable()
    make_user("admin", "jf-admin", int(Permission.MANAGE_REQUESTS))
    owner = make_user("eve", "jf-eve", int(Permission.REQUEST))
    token = mcp_auth.generate_token(owner)

    is_error, body = _tool(token, "submit_request", source="streamingcommunity",
                           media_type="film", external_id="7", title="Film")
    assert not is_error, body
    request = request_models.get(body["request"]["id"])
    assert request.requested_by == owner.id


def test_request_audio_is_checked_against_the_source(client, source):
    _jellyfin_mode()
    _enable()
    owner = make_user("fay", "jf-fay", int(Permission.REQUEST))
    token = mcp_auth.generate_token(owner)

    is_error, message = _tool(token, "submit_request", source="streamingcommunity",
                              media_type="film", external_id="7", title="Film",
                              audio_languages=["jpn"])
    assert is_error and "jpn" in message


def test_queue_tools_say_there_is_no_queue_in_open_mode(client):
    enable_open_mode()
    _enable()
    token = mcp_auth.generate_token(OPEN_MODE_USER)
    is_error, message = _tool(token, "list_requests")
    assert is_error and "coda" in message


def test_season_goes_through_the_batch(client, source, stub_jobs):
    enable_open_mode()
    _enable()
    token = mcp_auth.generate_token(OPEN_MODE_USER)
    is_error, body = _tool(token, "download_season", tv_id=1, slug="s", tv_name="Serie",
                           season_number=1)
    assert not is_error, body
    assert body["count"] == len(source.episodes)
    assert {kwargs["batch_id"] for _, _, kwargs in stub_jobs} == {body["batch_id"]}


def test_episode_is_picked_by_its_number(client, source, stub_jobs):
    enable_open_mode()
    _enable()
    token = mcp_auth.generate_token(OPEN_MODE_USER)
    is_error, _ = _tool(token, "download_episode", tv_id=1, slug="s", tv_name="Serie",
                        season_number=1, episode_number=2)
    assert not is_error
    (_, args, _), = stub_jobs
    assert args[2] == 1  # ep_index of episode 2

    is_error, message = _tool(token, "download_episode", tv_id=1, slug="s", tv_name="Serie",
                              season_number=1, episode_number=9)
    assert is_error and "9" in message


# ── Settings API ───────────────────────────────────────────────────────────────

@pytest.fixture
def settings_admin(client):
    _jellyfin_mode()
    user = make_user("root", "jf-root", int(Permission.MANAGE_SETTINGS))
    return user, session_for(client, user.id)


def test_token_is_shown_once_and_owned_by_who_generated_it(client, settings_admin):
    user, csrf = settings_admin
    minted = client.post("/api/mcp/token", headers={"X-CSRF-Token": csrf})
    assert minted.status_code == 200
    assert minted.json()["owner"] == "root"

    status = client.get("/api/mcp/status").json()
    assert status["has_token"] is True and status["owner"] == "root"
    assert minted.json()["token"] not in json.dumps(status)

    assert client.delete("/api/mcp/token", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/mcp/status").json()["has_token"] is False


def test_switch_is_saved_through_the_settings_endpoint(client, settings_admin):
    _, csrf = settings_admin
    response = client.put("/api/domain/settings", json={"mcp_enabled": True},
                          headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert config.get_settings()["mcp_enabled"] is True
