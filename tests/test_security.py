"""Untrusted input: the source host, poster names and titles from third parties."""

import json

import pytest

from app.auth.permissions import ALL_PERMISSIONS
from app.core.headers import sanitize_filename
from app.core.paths import episode_path, film_path
from tests.conftest import do_setup, make_user, session_for


@pytest.fixture
def signed_in(client, admin_credentials):
    do_setup(client, admin_credentials)
    user = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    client.cookies.clear()
    return session_for(client, user.id)


# ── The source host is server-side ─────────────────────────────────────────────

def test_download_endpoints_no_longer_accept_a_domain(client, signed_in, stub_jobs, monkeypatch):
    """The client used to choose which host the server would call."""
    from app import config
    monkeypatch.setattr(config, "configured_domain", lambda: "trusted.example")
    from app.routers import downloads
    monkeypatch.setattr(downloads, "configured_domain", lambda: "trusted.example")

    response = client.post(
        "/api/download/film",
        json={"id": 1, "title": "x", "domain": "attacker.example"},
        headers={"X-CSRF-Token": signed_in},
    )

    assert response.status_code == 202
    _, args, _ = stub_jobs[0]
    assert args[2] == "trusted.example"


def test_search_ignores_a_domain_from_the_query_string(client, signed_in, monkeypatch):
    from app.routers import search

    seen = {}
    monkeypatch.setattr(search, "configured_domain", lambda: "trusted.example")
    monkeypatch.setattr(
        search, "core_search",
        lambda query, domain: seen.setdefault("domain", domain) or [],
    )

    client.get("/api/search?q=abc&domain=attacker.example")

    assert seen["domain"] == "trusted.example"


def test_endpoints_refuse_to_run_without_a_configured_domain(client, signed_in, monkeypatch):
    from app.routers import downloads, search
    monkeypatch.setattr(downloads, "configured_domain", lambda: "")
    monkeypatch.setattr(search, "configured_domain", lambda: "")

    assert client.get("/api/search?q=abc").status_code == 409
    assert client.post(
        "/api/download/film", json={"id": 1, "title": "x"},
        headers={"X-CSRF-Token": signed_in},
    ).status_code == 409


# ── The image proxy is no longer open ──────────────────────────────────────────

def test_image_proxy_uses_the_configured_host(client, signed_in, monkeypatch):
    from app.routers import images

    seen = {}
    monkeypatch.setattr(images, "configured_domain", lambda: "trusted.example")
    monkeypatch.setattr(
        images, "_fetch_image",
        lambda domain, filename: seen.update(domain=domain, filename=filename) or (b"x", "image/png"),
    )

    assert client.get("/api/image/poster.jpg").status_code == 200
    assert seen["domain"] == "trusted.example"


@pytest.mark.parametrize("filename", [
    "../../etc/passwd",
    "..%2f..%2fetc%2fpasswd",
    "poster jpg",       # spaces would have to be encoded into the outbound URL
    "poster.jpg%00.png",
    "a" * 300,
])
def test_image_proxy_rejects_hostile_filenames(client, signed_in, monkeypatch, filename):
    from app.routers import images
    monkeypatch.setattr(images, "configured_domain", lambda: "trusted.example")
    monkeypatch.setattr(
        images, "_fetch_image",
        lambda domain, name: pytest.fail(f"should not have fetched {name!r}"),
    )

    assert client.get(f"/api/image/{filename}").status_code in (400, 404)


# ── Titles from third parties end up in filesystem paths ───────────────────────

@pytest.mark.parametrize("title", [
    "../../../etc/passwd",
    "..\\..\\windows\\system32",
    "normal/with/slashes",
    "with:colons*and?wildcards",
    "trailing dots...",
    "\x00null\x01control",
])
def test_sanitize_filename_strips_path_syntax_on_every_platform(title):
    cleaned = sanitize_filename(title)

    assert "/" not in cleaned
    assert "\\" not in cleaned
    assert "\x00" not in cleaned
    assert cleaned not in (".", "..", "")


def test_sanitize_filename_never_returns_empty():
    assert sanitize_filename("...") == "senza-nome"
    assert sanitize_filename("/") == "senza-nome"
    assert sanitize_filename("") == "senza-nome"


def test_destination_paths_stay_inside_the_library():
    import os

    hostile = "../../../../etc/cron.d/evil"
    film = os.path.abspath(film_path("/library", hostile, "2020"))
    episode = os.path.abspath(episode_path("/library", hostile, 1, "1", None))

    assert film.startswith(os.path.abspath("/library") + os.sep)
    assert episode.startswith(os.path.abspath("/library") + os.sep)


def test_long_titles_are_truncated_to_a_usable_length():
    assert len(sanitize_filename("A" * 500)) <= 180
