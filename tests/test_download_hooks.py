"""Post-download hooks: what fires, what does not, and what never leaks back.

The design constraint driving most of these tests is that open mode grants
MANAGE_SETTINGS to every anonymous visitor, so whoever configures a hook may be
nobody in particular. There is deliberately no shell hook. A webhook is still an
outbound request to an address that person chose — and pointing it at a private
address is the whole point, that is where Jellyfin lives — so the mitigation is
that the request is *blind*: the response body never comes back to the caller
and is never logged, only the status code.
"""

import json
from types import SimpleNamespace

import pytest

from app import db, downloads_hooks
from app.auth import models as auth_models
from app.auth.permissions import ALL_PERMISSIONS
from tests.conftest import do_setup, make_user, session_for


def _job(**overrides):
    base = dict(
        job_id="job-1", title="Test Series S01E01", status="done", type="episode",
        output_path="/srv/tv/Test Series/Season 01/Test Series S01E01.mkv",
        season=1, episode_number="1", year="2019", error=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Response:
    def __init__(self, status_code=200, text="secret body"):
        self.status_code = status_code
        self.text = text
        self.ok = status_code < 400


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return _Response()

    monkeypatch.setattr(downloads_hooks.requests, "request", fake_request)
    monkeypatch.setattr(downloads_hooks.requests, "post",
                        lambda url, **kw: calls.append({"method": "POST", "url": url, **kw})
                        or _Response())
    return calls


@pytest.fixture
def hook(client):
    """One enabled hook subscribed to everything."""
    return downloads_hooks.create_hook(name="Jellyfin", url="http://jf.local/refresh")


# ── Migration ─────────────────────────────────────────────────────────────────

def test_the_hook_table_exists(client):
    assert db.query_one("PRAGMA user_version")[0] == len(db.MIGRATIONS)
    downloads_hooks.create_hook(name="x", url="http://x/y")
    assert len(downloads_hooks.list_hooks()) == 1


# ── Event filtering ───────────────────────────────────────────────────────────

def test_an_empty_event_list_means_every_event(client, hook, sent):
    """The same rule the notification channels use, and easy to get backwards."""
    for status in ("done", "error", "cancelled"):
        downloads_hooks.on_job_finished(_job(status=status))
    assert len(sent) == 3


def test_a_hook_only_fires_for_the_events_it_asked_for(client, sent):
    downloads_hooks.create_hook(name="only-failures", url="http://x/y", events=["error"])

    downloads_hooks.on_job_finished(_job(status="done"))
    assert sent == []

    downloads_hooks.on_job_finished(_job(status="error", error="boom"))
    assert len(sent) == 1


def test_a_disabled_hook_never_fires(client, sent):
    created = downloads_hooks.create_hook(name="off", url="http://x/y")
    downloads_hooks.update_hook(created["id"], enabled=False)

    downloads_hooks.on_job_finished(_job())
    assert sent == []


def test_a_job_that_is_not_finished_fires_nothing(client, hook, sent):
    for status in ("queued", "running", "scheduled"):
        downloads_hooks.on_job_finished(_job(status=status))
    assert sent == []


# ── Payload ───────────────────────────────────────────────────────────────────

def test_the_default_body_carries_every_token(client, hook, sent):
    downloads_hooks.on_job_finished(_job())
    payload = json.loads(sent[0]["data"].decode())
    assert payload["title"] == "Test Series S01E01"
    assert payload["status"] == "done"
    assert payload["season"] == "1"


def test_a_template_is_substituted(client, sent):
    downloads_hooks.create_hook(
        name="t", url="http://x/y",
        body_template='{"content": "{title} \\u00e8 pronto in {path}"}',
    )
    downloads_hooks.on_job_finished(_job())

    payload = json.loads(sent[0]["data"].decode())
    assert payload["content"].startswith("Test Series S01E01 è pronto in /srv/tv/")


def test_a_title_with_a_quote_does_not_break_the_payload(client, sent):
    """str.format would happily produce a body the other end rejects."""
    downloads_hooks.create_hook(
        name="t", url="http://x/y", body_template='{"content": "{title}"}',
    )
    downloads_hooks.on_job_finished(_job(title='Un film "particolare"'))

    payload = json.loads(sent[0]["data"].decode())
    assert payload["content"] == 'Un film "particolare"'


def test_missing_fields_render_empty_rather_than_raising(client, sent):
    """A job restored from schedule.json has no year, season or episode number."""
    downloads_hooks.create_hook(
        name="t", url="http://x/y",
        body_template='{"y": "{year}", "s": "{season}", "e": "{episode}"}',
    )
    downloads_hooks.on_job_finished(_job(year=None, season=None, episode_number=None))

    assert json.loads(sent[0]["data"].decode()) == {"y": "", "s": "", "e": ""}


# ── Isolation ─────────────────────────────────────────────────────────────────

def test_one_broken_hook_does_not_stop_the_others(client, monkeypatch):
    downloads_hooks.create_hook(name="broken", url="http://a/1")
    downloads_hooks.create_hook(name="fine", url="http://b/2")

    reached = []

    def fake_request(method, url, **kwargs):
        reached.append(url)
        if "http://a/1" == url:
            raise RuntimeError("connection refused")
        return _Response()

    monkeypatch.setattr(downloads_hooks.requests, "request", fake_request)
    monkeypatch.setattr(downloads_hooks, "_notify_failure", lambda *a, **k: None)

    downloads_hooks.on_job_finished(_job())
    assert reached == ["http://a/1", "http://b/2"]


def test_a_failing_hook_is_reported(client, monkeypatch):
    downloads_hooks.create_hook(name="broken", url="http://a/1")
    monkeypatch.setattr(
        downloads_hooks.requests, "request",
        lambda *a, **k: _Response(status_code=500),
    )
    announced = []
    monkeypatch.setattr(downloads_hooks, "_notify_failure",
                        lambda name, status: announced.append((name, status)))

    downloads_hooks.on_job_finished(_job())
    assert announced == [("broken", 500)]


def test_an_unsupported_method_is_refused(client, sent):
    created = downloads_hooks.create_hook(name="x", url="http://x/y")
    downloads_hooks.update_hook(created["id"], method="DELETE")

    ok, status = downloads_hooks.fire(downloads_hooks.get_hook(created["id"]), {})
    assert ok is False and status is None
    assert sent == []


def test_the_outbound_call_has_a_timeout(client, hook, sent):
    downloads_hooks.on_job_finished(_job())
    assert sent[0].get("timeout") is not None


# ── Jellyfin refresh ──────────────────────────────────────────────────────────

def test_the_refresh_is_off_by_default(client, monkeypatch):
    called = []
    monkeypatch.setattr(downloads_hooks, "refresh_jellyfin_library",
                        lambda: called.append(True) or (True, 200))

    downloads_hooks.on_job_finished(_job())
    assert called == []


def test_the_refresh_runs_only_on_success(client, monkeypatch):
    from app import config

    config.save_settings({**config.get_settings(), "jellyfin_refresh_on_download": True})
    called = []
    monkeypatch.setattr(downloads_hooks, "refresh_jellyfin_library",
                        lambda: called.append(True) or (True, 200))

    downloads_hooks.on_job_finished(_job(status="error", error="boom"))
    assert called == []

    downloads_hooks.on_job_finished(_job(status="done"))
    assert called == [True]


def test_the_refresh_is_inert_without_jellyfin(client, sent):
    """Open mode with no Jellyfin has no URL and no key: nothing to call."""
    assert downloads_hooks.refresh_jellyfin_library() == (False, None)
    assert sent == []


def test_the_refresh_uses_the_stored_credentials(client, sent):
    auth_models.set_setting(auth_models.SETTING_JELLYFIN_URL, "http://jf.local:8096/")
    auth_models.set_setting(auth_models.SETTING_JELLYFIN_API_KEY, "abc123")

    downloads_hooks.refresh_jellyfin_library()

    assert sent[0]["url"] == "http://jf.local:8096/Library/Refresh"
    assert sent[0]["headers"]["X-Emby-Token"] == "abc123"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@pytest.fixture
def admin(client, admin_credentials):
    do_setup(client, admin_credentials)
    user = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    client.cookies.clear()
    return user, session_for(client, user.id)


def _csrf(admin):
    return {"X-CSRF-Token": admin[1]}


def test_create_list_and_delete(client, admin):
    created = client.post("/api/download-hooks", headers=_csrf(admin), json={
        "name": "Jellyfin", "url": "http://jf.local/refresh",
    })
    assert created.status_code == 200
    hook_id = created.json()["hook"]["id"]

    assert len(client.get("/api/download-hooks").json()["hooks"]) == 1

    assert client.delete(f"/api/download-hooks/{hook_id}",
                         headers=_csrf(admin)).status_code == 200
    assert client.get("/api/download-hooks").json()["hooks"] == []


def test_an_unknown_event_is_refused(client, admin):
    res = client.post("/api/download-hooks", headers=_csrf(admin), json={
        "name": "x", "url": "http://x/y", "events": ["exploded"],
    })
    assert res.status_code == 422


def test_a_non_http_url_is_refused(client, admin):
    res = client.post("/api/download-hooks", headers=_csrf(admin), json={
        "name": "x", "url": "file:///etc/passwd",
    })
    assert res.status_code == 422


def test_the_test_button_never_returns_the_response_body(client, admin, monkeypatch):
    """Blind on purpose: a hook may point anywhere on the local network.

    Returning what the other end said would turn a webhook into a way of reading
    services the caller cannot otherwise reach.
    """
    created = downloads_hooks.create_hook(name="x", url="http://internal/secrets")
    monkeypatch.setattr(
        downloads_hooks.requests, "request",
        lambda *a, **k: _Response(status_code=200, text="TOP SECRET"),
    )

    body = client.post(f"/api/download-hooks/{created['id']}/test",
                       headers=_csrf(admin)).json()

    assert body == {"ok": True, "status": 200}
    assert "TOP SECRET" not in json.dumps(body)
