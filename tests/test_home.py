"""The start page: the shelves each source publishes on its own front page.

The cache is the part worth pinning. It is modelled on metadata's, and departs
from it in the two ways that matter here: the key carries the host, so a domain
rotation cannot serve the old domain's shelves, and a failed fetch is never
remembered, because a home page that failed is a flap and caching it would blank
the start page for everyone.
"""

import html
import json

import pytest
import requests

from app.auth.permissions import ALL_PERMISSIONS
from app.core import animeunity, home, page
from tests.conftest import do_setup, make_user, session_for


@pytest.fixture(autouse=True)
def _empty_cache():
    home.clear_cache()
    yield
    home.clear_cache()


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _Response:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _sc_title(n, kind="movie"):
    return {"id": n, "name": f"Title {n}", "slug": f"title-{n}", "type": kind,
            "score": "7.7", "seasons_count": 1,
            "images": [{"type": "poster", "filename": f"poster-{n}.webp"}]}


def _sc_home(*sliders):
    payload = {"props": {"sliders": list(sliders)}}
    return ('<html><body><div id="app" data-page="'
            + html.escape(json.dumps(payload), quote=True)
            + '"></div></body></html>')


@pytest.fixture
def sc(monkeypatch):
    """Serve the StreamingCommunity front page, and count the requests for it."""
    state = {"html": _sc_home(), "calls": [], "error": None}

    def fake_get(url, *args, **kwargs):
        state["calls"].append(url)
        if state["error"]:
            raise state["error"]
        return _Response(state["html"])

    monkeypatch.setattr(page.requests, "get", fake_get)
    return state


def _au_record(n, kind="TV"):
    return {"id": n, "slug": f"anime-{n}", "title_eng": f"Anime {n}", "type": kind,
            "imageurl": f"https://cdn.animeunity.test/{n}.jpg", "episodes_count": 12}


def _au_home(latest_rows, featured):
    items = html.escape(json.dumps({"data": latest_rows}), quote=True)
    animes = html.escape(json.dumps(featured), quote=True)
    return (f'<html><body><layout-items items-json="{items}"></layout-items>'
            f'<the-carousel animes="{animes}"></the-carousel></body></html>')


@pytest.fixture
def au(monkeypatch):
    state = {"html": _au_home([], []), "calls": []}

    class _Scraper:
        def get(self, url, **kwargs):
            state["calls"].append(url)
            return _Response(state["html"])

    monkeypatch.setattr(animeunity, "_get_scraper", lambda: _Scraper())
    monkeypatch.setattr(animeunity, "_csrf", None)
    return state


# ── StreamingCommunity shelves ────────────────────────────────────────────────

def test_the_sliders_become_shelves_keyed_by_name(sc):
    sc["html"] = _sc_home(
        {"name": "trending", "label": "I titoli del momento", "titles": [_sc_title(1)]},
        {"name": "top10", "label": "Top 10 titoli oggi", "titles": [_sc_title(2)]},
    )

    result = home.shelves("streamingcommunity", "source.test")

    assert [s["key"] for s in result] == ["trending", "top10"]
    # The panel's own heading, not the source's copy, which it can re-word.
    assert [s["title"] for s in result] == ["Di tendenza", "Top 10 di oggi"]


def test_an_unknown_shelf_keeps_the_sources_own_label(sc):
    """A vetrina added next month renders under its own heading rather than
    disappearing."""
    sc["html"] = _sc_home(
        {"name": "oscars2027", "label": "Candidati agli Oscar", "titles": [_sc_title(1)]})

    assert home.shelves("streamingcommunity", "source.test")[0]["title"] == "Candidati agli Oscar"


def test_the_items_are_search_result_shaped(sc):
    """Same shape as /api/search, so the grid and the modal need nothing new."""
    sc["html"] = _sc_home({"name": "trending", "titles": [_sc_title(1, "tv")]})

    item = home.shelves("streamingcommunity", "source.test")[0]["items"][0]

    assert item["id"] == 1 and item["name"] == "Title 1" and item["type"] == "tv"
    assert item["slug"] == "title-1"


def test_a_streamingcommunity_poster_is_a_bare_filename(sc):
    """It goes through /api/image, where the host is resolved server-side."""
    sc["html"] = _sc_home({"name": "trending", "titles": [_sc_title(1)]})

    item = home.shelves("streamingcommunity", "source.test")[0]["items"][0]

    assert item["poster"] == "poster-1.webp"


def test_an_empty_slider_is_not_a_shelf(sc):
    sc["html"] = _sc_home({"name": "trending", "titles": []},
                          {"name": "latest", "titles": [_sc_title(1)]})

    assert [s["key"] for s in home.shelves("streamingcommunity", "source.test")] == ["latest"]


# ── AnimeUnity shelves ────────────────────────────────────────────────────────

def test_the_anime_front_page_gives_both_lists(au):
    au["html"] = _au_home([{"anime": _au_record(1)}], [_au_record(2), _au_record(3)])

    result = home.shelves("animeunity", "www.animeunity.test")

    assert [s["key"] for s in result] == ["latest_episodes", "featured"]
    assert [s["title"] for s in result] == ["Ultimi episodi", "In evidenza"]
    assert len(result[1]["items"]) == 2


def test_the_latest_episode_list_is_deduplicated_by_anime(au):
    """Two new episodes of the same series are two rows and one card."""
    au["html"] = _au_home(
        [{"anime": _au_record(1)}, {"anime": _au_record(1)}, {"anime": _au_record(2)}], [])

    items = home.shelves("animeunity", "www.animeunity.test")[0]["items"]

    assert [i["id"] for i in items] == ["1-anime-1", "2-anime-2"]


def test_an_anime_poster_is_an_absolute_url(au):
    """The other half of the dual poster shape, pinned on this path too."""
    au["html"] = _au_home([], [_au_record(1)])

    item = home.shelves("animeunity", "www.animeunity.test")[0]["items"][0]

    assert item["poster"] == "https://cdn.animeunity.test/1.jpg"
    assert item["type"] == "anime" and item["media_type"] == "TV"


def test_a_front_page_with_neither_list_is_no_shelves(au):
    au["html"] = "<html><body>nothing here</body></html>"

    assert home.shelves("animeunity", "www.animeunity.test") == []


# ── The cache ─────────────────────────────────────────────────────────────────

def test_a_second_call_costs_no_request(sc):
    sc["html"] = _sc_home({"name": "trending", "titles": [_sc_title(1)]})

    home.shelves("streamingcommunity", "source.test")
    home.shelves("streamingcommunity", "source.test")

    assert len(sc["calls"]) == 1


def test_the_cache_expires(sc, monkeypatch):
    sc["html"] = _sc_home({"name": "trending", "titles": [_sc_title(1)]})
    home.shelves("streamingcommunity", "source.test")

    monkeypatch.setattr(home, "_now", lambda: home.time.time() + 3600)
    home.shelves("streamingcommunity", "source.test")

    assert len(sc["calls"]) == 2


def test_a_domain_rotation_does_not_serve_the_old_shelves(sc):
    """The host is in the key, so the old domain's shelves cannot be reached.
    This is where the cache parts company with metadata's, which keys on the
    title alone and serves the old domain's artwork for six hours."""
    sc["html"] = _sc_home({"name": "trending", "titles": [_sc_title(1)]})
    home.shelves("streamingcommunity", "old.test")

    sc["html"] = _sc_home({"name": "trending", "titles": [_sc_title(2)]})
    fresh = home.shelves("streamingcommunity", "new.test")

    assert fresh[0]["items"][0]["id"] == 2
    assert len(sc["calls"]) == 2


def test_a_failed_fetch_is_not_cached(sc):
    """A home page that failed is a flap. Remembering it would blank the start
    page for every user for a quarter of an hour."""
    sc["error"] = requests.ConnectionError("no route")

    assert home.shelves("streamingcommunity", "source.test") == []

    sc["error"] = None
    sc["html"] = _sc_home({"name": "trending", "titles": [_sc_title(1)]})

    assert home.shelves("streamingcommunity", "source.test")[0]["items"]


def test_applying_a_candidate_clears_the_shelves(sc, monkeypatch):
    from app.core import domain_recovery

    sc["html"] = _sc_home({"name": "trending", "titles": [_sc_title(1)]})
    home.shelves("streamingcommunity", "old.test")

    monkeypatch.setattr(domain_recovery, "is_plausible", lambda host: (True, ""))
    monkeypatch.setattr(domain_recovery, "verify", lambda host: "v9")
    monkeypatch.setattr(domain_recovery.config, "update_data", lambda changes: None)
    domain_recovery.apply_candidate("new.test")

    assert home._cached(home._cache_key("streamingcommunity", "old.test")) is None


# ── The endpoint ──────────────────────────────────────────────────────────────

@pytest.fixture
def signed_in(client, admin_credentials):
    do_setup(client, admin_credentials)
    user = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    client.cookies.clear()
    return session_for(client, user.id)


def test_the_endpoint_answers_shelves(client, signed_in, monkeypatch):
    from app.routers import home as router
    monkeypatch.setattr(router, "configured_domain", lambda: "source.test")
    monkeypatch.setattr(router.core_home, "shelves",
                        lambda source, host: [{"key": "trending", "title": "Di tendenza",
                                               "items": []}])

    body = client.get("/api/home").json()

    assert body["source"] == "streamingcommunity"
    assert body["shelves"][0]["key"] == "trending"


def test_streamingcommunity_needs_a_configured_domain(client, signed_in, monkeypatch):
    from app.routers import home as router
    monkeypatch.setattr(router, "configured_domain", lambda: "")

    assert client.get("/api/home").status_code == 409


def test_the_anime_front_page_needs_no_domain(client, signed_in, monkeypatch):
    """AnimeUnity lives at a fixed host: refusing there would be a lie."""
    from app.routers import home as router
    monkeypatch.setattr(router, "configured_domain", lambda: "")
    monkeypatch.setattr(router.core_home, "shelves", lambda source, host: [])

    assert client.get("/api/home?source=animeunity").status_code == 200


def test_an_unknown_source_is_refused(client, signed_in):
    assert client.get("/api/home?source=nowhere").status_code == 422


def test_the_domain_reaching_the_source_is_never_the_callers(client, signed_in, monkeypatch):
    from app.routers import home as router
    seen = {}
    monkeypatch.setattr(router, "configured_domain", lambda: "trusted.example")
    monkeypatch.setattr(router.core_home, "shelves",
                        lambda source, host: seen.setdefault("host", host) or [])

    client.get("/api/home?domain=attacker.example&host=attacker.example")

    assert seen["host"] == "trusted.example"


def test_setting_the_domain_by_hand_clears_the_shelves(client, signed_in, sc, monkeypatch):
    from app.routers import domain as domain_router

    sc["html"] = _sc_home({"name": "trending", "titles": [_sc_title(1)]})
    home.shelves("streamingcommunity", "old.test")

    monkeypatch.setattr(domain_router, "get_domain_version", lambda host: "v9")
    monkeypatch.setattr(domain_router, "_update_data", lambda changes: None)
    response = client.put("/api/domain", json={"domain": "new.test"},
                          headers={"X-CSRF-Token": signed_in})

    assert response.status_code == 200
    assert home._cached(home._cache_key("streamingcommunity", "old.test")) is None
