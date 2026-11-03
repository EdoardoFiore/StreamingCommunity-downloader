"""The new-version check: what counts as newer, what a failed check leaves
behind, who may ask, and that release notes can never carry markup of their own.
"""

import pytest
import requests

from app import updates
from app.auth.permissions import Permission
from tests.conftest import do_setup, enable_open_mode, make_user, session_for


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _release(tag, body="", **extra):
    return {
        "tag_name": tag,
        "body": body,
        "draft": False,
        "prerelease": False,
        "published_at": "2026-11-02T18:01:59Z",
        **extra,
    }


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """The answer is process-wide by design."""
    monkeypatch.setattr(updates, "__version__", "2.7.0")
    monkeypatch.setattr(updates, "_releases", [])
    monkeypatch.setattr(updates, "_checked_at", None)
    monkeypatch.setattr(updates, "_attempted_at", None)
    monkeypatch.setattr(updates, "_failed", False)


@pytest.fixture
def github(monkeypatch):
    """Answer with whatever ``.response`` holds, and record each call."""

    class Github:
        response = _Response([])
        calls: list = []

    Github.calls = []

    def fake_get(url, **kwargs):
        Github.calls.append((url, kwargs))
        if isinstance(Github.response, Exception):
            raise Github.response
        return Github.response

    monkeypatch.setattr(updates.requests, "get", fake_get)
    return Github


# ── What counts as newer ─────────────────────────────────────────────────────


@pytest.mark.parametrize("value, expected", [
    ("2.7.0", (2, 7, 0)),
    ("v2.10.1", (2, 10, 1)),
    ("v2.7", None),
    ("v2.8.0-rc1", None),
    ("nightly", None),
    (None, None),
])
def test_parse_version(value, expected):
    assert updates.parse_version(value) == expected


def test_newer_releases_are_listed_newest_first(github):
    github.response = _Response([_release("v2.8.0"), _release("v2.10.0"), _release("v2.9.0")])

    info = updates.check()

    assert info["update_available"] is True
    assert info["latest"] == "2.10.0"  # numeric: as text, 2.9.0 would win
    assert [r["version"] for r in info["releases"]] == ["2.10.0", "2.9.0", "2.8.0"]


def test_the_running_version_and_older_ones_are_not_an_update(github):
    github.response = _Response([_release("v2.7.0"), _release("v2.6.0")])

    info = updates.check()

    assert info["update_available"] is False
    assert info["latest"] is None
    assert info["checked_at"] is not None


def test_drafts_prereleases_and_odd_tags_are_never_offered(github):
    github.response = _Response([
        _release("v9.0.0", draft=True),
        _release("v9.1.0", prerelease=True),
        _release("v9.2.0-rc1"),
        _release("nightly"),
        "not a release",
    ])

    assert updates.check()["update_available"] is False


def test_the_release_link_is_built_from_the_tag_not_read_from_the_payload(github):
    github.response = _Response([_release("v2.8.0", html_url="https://attacker.example/")])

    release = updates.check()["releases"][0]

    assert release["url"] == f"https://github.com/{updates.REPOSITORY}/releases/tag/v2.8.0"


def test_the_request_carries_a_timeout_and_a_user_agent(github):
    updates.check()

    url, kwargs = github.calls[0]
    assert url == updates.RELEASES_URL
    assert kwargs["timeout"] is not None
    assert kwargs["headers"]["User-Agent"]


# ── A failed check ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("failure", [
    requests.ConnectionError("down"),
    _Response([], status_code=403),
    _Response(ValueError("not json")),
    _Response({"message": "rate limited"}),
])
def test_a_failure_is_reported_and_keeps_what_was_known(github, failure):
    github.response = _Response([_release("v2.8.0")])
    updates.check()

    github.response = failure
    info = updates.check()

    assert info["failed"] is True
    assert info["latest"] == "2.8.0"


def test_a_success_clears_the_failure(github):
    github.response = requests.ConnectionError("down")
    updates.check()

    github.response = _Response([])

    assert updates.check()["failed"] is False


def test_the_button_does_not_reach_github_twice_in_a_row(github):
    updates.check_now()
    updates.check_now()

    assert len(github.calls) == 1


# ── Release notes ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("markdown", [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "**<img src=x onerror=alert(1)>**",
    "`<script>`",
    "- <script>alert(1)</script>",
    "## <script>alert(1)</script>",
    "> <script>alert(1)</script>",
    "```\n<script>alert(1)</script>\n```",
    "[<script>](https://example.com/)",
])
def test_markup_in_the_notes_is_only_ever_text(markdown):
    rendered = updates.render_notes(markdown)

    assert "<script" not in rendered
    assert "<img" not in rendered
    assert "&lt;" in rendered


def test_a_link_cannot_leave_http():
    rendered = updates.render_notes("[click](javascript:alert(1))")

    assert "<a " not in rendered


def test_a_link_cannot_break_out_of_its_attribute():
    rendered = updates.render_notes('[x](https://example.com/"onmouseover="alert(1))')

    assert '"onmouseover' not in rendered


def test_an_image_loads_nothing():
    assert "<img" not in updates.render_notes("![pixel](https://tracker.example/p.gif)")


def test_quotes_nested_without_end_do_not_exhaust_the_stack():
    assert updates.render_notes(">" * 15000 + " deep")


def test_the_markdown_our_release_notes_actually_use():
    rendered = updates.render_notes(
        "Intro con `codice` e **grassetto**.\n\n"
        "> [!IMPORTANT]\n> **Attenzione** a questo.\n\n"
        "## Sezione (#40)\n\n"
        "- primo\n- secondo\n  a capo\n\n"
        "Da [@utente](https://github.com/utente) in #20.\n\n"
        "**Full Changelog**: https://github.com/a/b/compare/v1...v2\n"
    )

    assert "<p>Intro con <code>codice</code> e <strong>grassetto</strong>.</p>" in rendered
    assert 'class="release-alert release-alert-important"' in rendered
    assert '<p class="release-alert-title">Importante</p>' in rendered
    assert "<h4>Sezione (#40)</h4>" in rendered
    assert "<ul><li>primo</li><li>secondo a capo</li></ul>" in rendered
    assert '<a href="https://github.com/utente" target="_blank" rel="noopener noreferrer">@utente</a>' in rendered
    assert '<a href="https://github.com/a/b/compare/v1...v2"' in rendered
    assert "[!IMPORTANT]" not in rendered


def test_a_full_stop_is_not_part_of_a_bare_link():
    assert 'href="https://example.com/a"' in updates.render_notes("Vedi https://example.com/a.")


@pytest.mark.parametrize("empty", [None, "", "  \n ", 42])
def test_no_notes_render_as_nothing(empty):
    assert updates.render_notes(empty) == ""


def test_a_release_without_notes_is_still_offered(github):
    github.response = _Response([_release("v2.8.0", body=None)])

    release = updates.check()["releases"][0]

    assert release["notes_html"] == ""


# ── The endpoints ────────────────────────────────────────────────────────────


def test_the_endpoints_need_manage_settings(client, github, jellyfin, admin_credentials):
    do_setup(client, admin_credentials)
    user = make_user("viewer", "jf-viewer", int(Permission.DOWNLOAD))
    csrf = session_for(client, user.id)

    assert client.get("/api/updates").status_code == 403
    assert client.post("/api/updates/check", headers={"X-CSRF-Token": csrf}).status_code == 403
    assert github.calls == []


def test_the_endpoints_report_and_check(client, github):
    enable_open_mode()
    github.response = _Response([_release("v2.8.0", body="**ciao**")])

    assert client.get("/api/updates").json()["update_available"] is False
    info = client.post("/api/updates/check").json()

    assert info["current"] == "2.7.0"
    assert info["latest"] == "2.8.0"
    assert info["releases"][0]["notes_html"] == "<p><strong>ciao</strong></p>"
    assert client.get("/api/updates").json()["latest"] == "2.8.0"


def test_the_switch_is_saved_and_defaults_to_on(client):
    enable_open_mode()

    assert client.get("/api/domain/settings").json()["update_check_enabled"] is True
    assert client.put("/api/domain/settings", json={"update_check_enabled": False}).status_code == 200
    assert client.get("/api/domain/settings").json()["update_check_enabled"] is False
