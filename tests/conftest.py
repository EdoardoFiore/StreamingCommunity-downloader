"""Shared test fixtures.

Jellyfin is faked at the ``requests`` layer rather than by patching our own
helpers, so the client's own behaviour — header construction, the stable
DeviceId, the X-Forwarded-For retry, token invalidation — is exercised too.
"""

import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import db
from app.auth import jellyfin as jf
from app.auth import models, permissions, session as sessions


# ── Fake Jellyfin server ───────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.ok = 200 <= status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload) if self._payload is not None else ""


class FakeJellyfin:
    """Stands in for a Jellyfin server at the requests.request level."""

    def __init__(self):
        self.users: dict[str, dict] = {}
        self.passwords: dict[str, str] = {}
        self.api_keys: list[dict] = []
        self.calls: list[dict] = []
        self.issued_tokens: set[str] = set()
        self.revoked_tokens: set[str] = set()
        # When True the server rejects any request carrying X-Forwarded-For,
        # which is what Jellyfin does when the panel is not a known proxy.
        self.reject_forwarded_ip = False
        self.reachable = True
        self._token_seq = 0

    def add_user(self, name: str, password: str = "pw", admin: bool = False, user_id: str = None):
        user = {
            "Id": user_id or f"jf-{name}-id",
            "Name": name,
            "ServerId": "server-1",
            "Policy": {"IsAdministrator": admin, "IsDisabled": False},
        }
        self.users[name] = user
        self.passwords[name] = password
        return user

    def rename_user(self, old: str, new: str):
        user = self.users.pop(old)
        user["Name"] = new
        self.users[new] = user
        self.passwords[new] = self.passwords.pop(old)

    def device_ids_seen(self, path: str = "/Users/AuthenticateByName") -> list[str]:
        return [c["device_id"] for c in self.calls if c["path"] == path]

    # ── requests.request replacement ──────────────────────────────────────────

    def __call__(self, method, url, json=None, headers=None, timeout=None):
        if not self.reachable:
            import requests as _requests
            raise _requests.ConnectionError("connection refused")

        headers = headers or {}
        parsed = urlparse(url)
        path = parsed.path
        auth = headers.get("Authorization", "")
        device_id = ""
        token = ""
        for part in auth.replace("MediaBrowser ", "").split(", "):
            key, _, value = part.partition("=")
            value = value.strip('"')
            if key == "DeviceId":
                device_id = value
            elif key == "Token":
                token = value

        self.calls.append({
            "method": method,
            "path": path,
            "query": parse_qs(parsed.query),
            "device_id": device_id,
            "token": token,
            "forwarded_for": headers.get("X-Forwarded-For"),
            "json": json,
        })

        if self.reject_forwarded_ip and "X-Forwarded-For" in headers:
            return FakeResponse(400, {"Error": "unknown proxy"})

        return self._route(method, path, parsed, json, token)

    def _route(self, method, path, parsed, body, token):
        if path == "/System/Info/Public":
            return FakeResponse(200, {"ServerName": "fake", "Version": "10.9.0", "Id": "server-1"})

        if path == "/Users/AuthenticateByName" and method == "POST":
            name = (body or {}).get("Username")
            user = self.users.get(name)
            if user is None or self.passwords.get(name) != (body or {}).get("Pw"):
                return FakeResponse(401, {"Error": "invalid"})
            self._token_seq += 1
            access = f"token-{self._token_seq}"
            self.issued_tokens.add(access)
            return FakeResponse(200, {"User": user, "AccessToken": access, "ServerId": "server-1"})

        if path == "/Sessions/Logout" and method == "POST":
            self.revoked_tokens.add(token)
            return FakeResponse(204)

        if path == "/System/Info":
            if not self._valid(token):
                return FakeResponse(401, {"Error": "bad token"})
            return FakeResponse(200, {"ServerName": "fake", "Id": "server-1"})

        if path == "/Users" and method == "GET":
            if not self._valid(token):
                return FakeResponse(401, {"Error": "bad token"})
            return FakeResponse(200, list(self.users.values()))

        if path == "/Auth/Keys" and method == "POST":
            if not self._valid(token):
                return FakeResponse(401, {"Error": "bad token"})
            app_name = parse_qs(parsed.query).get("App", ["?"])[0]
            self.api_keys.append({"AppName": app_name, "AccessToken": f"apikey-{len(self.api_keys) + 1}"})
            return FakeResponse(204)

        if path == "/Auth/Keys" and method == "GET":
            if not self._valid(token):
                return FakeResponse(401, {"Error": "bad token"})
            return FakeResponse(200, {"Items": list(self.api_keys)})

        return FakeResponse(404, {"Error": f"no route {method} {path}"})

    def _valid(self, token: str) -> bool:
        if not token:
            return False
        if token in self.revoked_tokens:
            return False
        return token in self.issued_tokens or any(
            k["AccessToken"] == token for k in self.api_keys
        )


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def jellyfin(monkeypatch):
    fake = FakeJellyfin()
    monkeypatch.setattr(jf.requests, "request", fake)
    return fake


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient on a throwaway database, with the app lifespan not started.

    The lifespan would start the download scheduler and re-hydrate the schedule
    store, neither of which belongs in a unit test, so migrations are run here
    instead.
    """
    db.configure(tmp_path / "test.db")
    db.run_migrations()

    from app.main import app

    # Not entered as a context manager on purpose: that would run the lifespan,
    # which starts the download scheduler and re-hydrates the schedule store.
    yield TestClient(app)
    db.close_all()


@pytest.fixture
def stub_jobs(monkeypatch):
    """Stop download submissions from actually reaching the network.

    The call is bound against the *real* signature before being recorded, so a
    caller passing an argument JobManager does not accept fails here instead of
    passing the tests and raising in production — which is exactly what happened
    with strict_audio.
    """
    import inspect

    from app.jobs import job_manager

    submitted: list[tuple] = []

    def fake_submit(name):
        signature = inspect.signature(getattr(job_manager, name))

        def _submit(*args, **kwargs):
            signature.bind(*args, **kwargs)  # raises TypeError on a bad call
            submitted.append((name, args, kwargs))
            return f"job-{len(submitted)}"

        return _submit

    for name in ("submit_film", "submit_episode", "submit_anime_episode"):
        monkeypatch.setattr(job_manager, name, fake_submit(name))
    return submitted


class FakeSource:
    """Stands in for StreamingCommunity / AnimeUnity during request tests."""

    def __init__(self):
        self.audio = ["ita", "eng"]
        self.subtitles = ["ita", "eng"]
        self.dead = False
        self.episodes = [{"id": 900 + n, "n": str(n), "name": f"Episodio {n}"} for n in (1, 2, 3)]
        self.anime_episodes = [{"id": 800 + n, "number": str(n)} for n in (1, 2, 3)]

    @property
    def languages(self) -> dict:
        if self.dead:
            raise RuntimeError("HTTP 404")
        return {"audio": list(self.audio), "subtitles": list(self.subtitles)}


class InlineExecutor:
    """Runs approval work on the calling thread, so tests stay deterministic."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


@pytest.fixture
def source(monkeypatch, tmp_path):
    """Fake external source plus a temporary library and configured domain."""
    from app.core import animeunity, film, page, tv
    from app.requests import resolver, service

    fake = FakeSource()

    data_file = tmp_path / "data.json"
    library = tmp_path / "library"
    library.mkdir()
    data_file.write_text(
        json.dumps({
            "domain": "example.test",
            "libraries": [
                {"type": "film", "path": str(library)},
                {"type": "tv", "path": str(library)},
                {"type": "anime", "path": str(library)},
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(resolver, "DATA_FILE", data_file)
    fake.library = library

    monkeypatch.setattr(film, "get_film_languages", lambda *a, **k: fake.languages)
    monkeypatch.setattr(page, "get_domain_version", lambda *a, **k: "v1")
    monkeypatch.setattr(tv, "get_token", lambda *a, **k: "xsrf-token")
    monkeypatch.setattr(
        tv, "get_info_season",
        lambda *a, **k: ([] if fake.dead else list(fake.episodes)),
    )
    monkeypatch.setattr(tv, "get_episode_languages", lambda *a, **k: fake.languages)
    monkeypatch.setattr(
        animeunity, "get_episodes",
        lambda *a, **k: ([] if fake.dead else list(fake.anime_episodes)),
    )
    monkeypatch.setattr(animeunity, "get_episode_languages", lambda *a, **k: fake.languages)

    # Approval resolution runs inline instead of on its worker pool.
    monkeypatch.setattr(service, "_resolution_pool", InlineExecutor())
    return fake


@pytest.fixture
def admin_credentials(jellyfin):
    jellyfin.add_user("admin", "adminpw", admin=True)
    return {"url": "http://jellyfin.local:8096", "username": "admin", "password": "adminpw"}


def do_setup(client, credentials) -> dict:
    """Run the first-run bootstrap and return the JSON payload."""
    response = client.post("/api/auth/setup", json=credentials)
    assert response.status_code == 200, response.text
    return response.json()


def do_login(client, username: str, password: str):
    return client.post("/api/auth/jellyfin", json={"username": username, "password": password})


def make_user(username: str, jellyfin_user_id: str, perms: int, enabled: bool = True):
    return models.create_user(
        jellyfin_user_id=jellyfin_user_id,
        username=username,
        permissions=perms,
        enabled=enabled,
    )


def session_for(client, user_id: int):
    """Attach a session cookie for ``user_id`` to ``client`` and return its CSRF token."""
    raw_token, csrf_token = sessions.create_session(user_id)
    client.cookies.set(sessions.SESSION_COOKIE, raw_token)
    return csrf_token


ALL = int(permissions.ALL_PERMISSIONS)
