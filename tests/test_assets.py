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
    and it would be invisible until a deploy failed to take effect.

    Recursive on purpose. This swept only the top level once, which meant the
    check would have quietly stopped covering anything the moment templates were
    split into partials/ and pages/ — losing its grip exactly as the number of
    files it had to watch went up.
    """
    from pathlib import Path

    templates = Path(__file__).parent.parent / "app" / "templates"
    offenders = [
        str(path.relative_to(templates)) for path in templates.rglob("*.html")
        if '"/static/' in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"hardcoded /static/ paths in: {', '.join(sorted(offenders))}"


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


# ── The stream indicator and the stream it describes ───────────────────────────

def _read(*parts) -> str:
    from pathlib import Path

    return (Path(__file__).parent.parent / "app").joinpath(*parts).read_text(encoding="utf-8")


def _all_panel_js() -> str:
    """Every script the panel serves, concatenated.

    The tables below used to be read out of app.js by name. That made the test
    a lock on a filename rather than on the rule it exists for: splitting the
    download page into its own script would have failed a test about phase
    vocabulary, and the obvious "fix" — repointing it at the new file — leaves
    the next split to fail the same way. What has to hold is that *the client*
    can render every phase, wherever the table lives.
    """
    from pathlib import Path

    static = Path(__file__).parent.parent / "app" / "static"
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(static.glob("*.js"))
    )


def test_the_stream_indicator_is_shown_exactly_when_the_stream_is_opened():
    """Three files state the same rule, and they drifted apart once already.

    progress.py requires DOWNLOAD or MANAGE_REQUESTS; app.js opens the
    EventSource under the same test; the indicator itself had no gate at all, so
    a user who can only make requests watched it say "Connessione..." forever —
    a connection deliberately never attempted, reported as one that failed.
    """
    import re
    from pathlib import Path

    # Searched across every template rather than in one named file. The
    # indicator moved into a partial once, and a test that hardcodes where it
    # lives reports that move as a failure while quietly covering nothing after
    # someone "fixes" it by pointing at the new path.
    templates = Path(__file__).parent.parent / "app" / "templates"
    matches = [
        m
        for path in sorted(templates.rglob("*.html"))
        for m in re.finditer(r'id="stream-status"[^>]*data-perm="([^"]+)"',
                             path.read_text(encoding="utf-8"))
    ]
    assert len(matches) == 1, (
        "expected exactly one stream indicator, gated by the stream's own "
        f"permissions; found {len(matches)}"
    )
    shown_for = set(matches[0].group(1).split("|"))

    # The endpoint's requirement.
    router = _read("routers", "progress.py")
    required = set(re.findall(r"Permission\.(\w+)", router))
    assert shown_for == required

    # And the client only connects under the same condition.
    app_js = _read("static", "app.js")
    # Kept on one line so the match cannot wander back to an earlier if().
    guard = re.search(r"if \(([^{\n]*)\)\s*\{\s*connectGlobalStream\(\);", app_js)
    assert guard, "connectGlobalStream must stay behind a permission check"
    assert set(re.findall(r"can\('(\w+)'\)", guard.group(1))) == required


# ── The phase vocabulary, server and client ────────────────────────────────────

def test_every_phase_the_server_emits_has_a_client_entry():
    """Four lookup tables in app.js turn a phase into a label, a badge, a bar
    colour and a border. "video" - the first phase of every download - was in
    none of them, so a card rebuilt mid-download (switching page and back)
    fell through to the grey fallback and lost its colour, border and label.

    Live updates hid it: the progress handler only sets the bar's width, so
    the card kept the colour it was created with until something rebuilt it.
    """
    import re

    from app.jobs import JobManager

    # Audio phases are per-language (audio_ita, audio_eng, ...) and the client
    # resolves them with a startsWith fallback, so only the fixed names have
    # to be present by name.
    phases = [p for p in JobManager._compute_phases(["ita"]) if not p.startswith("audio_")]
    assert "video" in phases, "the phase this test exists for has been renamed"

    scripts = _all_panel_js()
    missing = []
    for table in ("PHASE_LABELS", "PHASE_BADGE", "PHASE_BAR", "PHASE_BORDER_MAP"):
        match = re.search(rf"const {table} = \{{(.*?)\n\}};", scripts, re.S)
        assert match, f"{table} is not defined in any script under app/static/"
        body = match.group(1)
        for phase in phases:
            if not re.search(rf"\b{re.escape(phase)}\s*:", body):
                missing.append(f"{table}.{phase}")

    assert not missing, "phases the client cannot render: " + ", ".join(missing)
