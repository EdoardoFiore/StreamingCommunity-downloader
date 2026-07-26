"""Static assets carry a content hash, and the panel reports its version.

Both exist because of the same problem: nothing else tells you what is actually
running. A reverse proxy kept serving a stale app.js against new HTML, and no
bug report could say which build it came from.
"""

from app import __version__
from app.main import _asset_version, asset


# ── Cache busting ──────────────────────────────────────────────────────────────

def test_asset_url_carries_a_content_hash():
    url = asset("app.js")
    assert url.startswith("/static/app.js?v=")
    assert len(url.split("?v=")[1]) == 10


def test_asset_hash_differs_between_files():
    """A shared version string would defeat the point: touching one file has to
    change only that file's URL."""
    assert _asset_version("app.js") != _asset_version("panel.js")


def test_missing_asset_falls_back_to_an_unversioned_url():
    _asset_version.cache_clear()
    assert asset("does-not-exist.js") == "/static/does-not-exist.js"
    _asset_version.cache_clear()


def test_templates_never_hardcode_a_static_path():
    """A hardcoded /static/... path is exactly the stale-cache bug coming back,
    and it would be invisible until a deploy failed to take effect."""
    from pathlib import Path

    templates = Path(__file__).parent.parent / "app" / "templates"
    offenders = [
        path.name for path in templates.glob("*.html")
        if '"/static/' in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"hardcoded /static/ paths in: {', '.join(offenders)}"


def test_versioned_request_may_be_cached_forever(client):
    response = client.get("/static/app.js?v=abcdef1234")
    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]


def test_unversioned_request_must_be_revalidated(client):
    """Without a version in the URL there is nothing to distinguish builds, so
    the response must never be allowed to pin."""
    response = client.get("/static/app.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_rendered_page_references_versioned_assets(client, monkeypatch):
    from app.auth import deps

    monkeypatch.setattr(deps, "AUTH_ENABLED", False)
    body = client.get("/").text

    assert f'/static/app.js?v={_asset_version("app.js")}' in body
    assert '"/static/app.js"' not in body


# ── Version reporting ──────────────────────────────────────────────────────────

def test_me_reports_the_version(client, admin_credentials):
    from tests.conftest import do_setup

    do_setup(client, admin_credentials)
    assert client.get("/api/auth/me").json()["version"] == __version__


def test_public_status_does_not_leak_the_version(client):
    """An unauthenticated visitor has no use for it."""
    assert "version" not in client.get("/api/auth/status").json()
