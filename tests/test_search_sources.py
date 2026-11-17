"""Searching each source: what is asked for, and what comes back.

Both halves live in one file on purpose. The two sources disagree about almost
everything — paging, filters, and the shape of a poster — and the pins that
matter are the ones that read side by side.
"""

import html
import json

import pytest
import requests

from app.core import page


# ── StreamingCommunity ────────────────────────────────────────────────────────

def _page_html(payload: dict) -> str:
    """A server-rendered page carrying *payload*, escaped the way the site does."""
    return (
        '<html><body><div id="app" data-page="'
        + html.escape(json.dumps(payload), quote=True)
        + '"></div></body></html>'
    )


def _title(n, kind="tv", poster="p.jpg"):
    return {
        "id": n, "name": f"Title {n}", "slug": f"title-{n}", "type": kind,
        "score": "8.1", "last_air_date": "2024-05-01", "age": 14,
        "seasons_count": 2,
        "images": [{"type": "background", "filename": "b.jpg"},
                   {"type": "poster", "filename": poster}],
    }


class _Response:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def sc(monkeypatch):
    """Serve one page payload, and record every request made to get it."""
    state = {"payload": {"props": {"titles": []}}, "calls": []}

    def fake_get(url, *args, **kwargs):
        state["calls"].append((url, kwargs))
        return _Response(_page_html(state["payload"]))

    monkeypatch.setattr(page.requests, "get", fake_get)
    return state


def test_the_search_reads_the_server_rendered_props(sc):
    sc["payload"] = {"props": {"titles": [_title(1), _title(2)]}}

    results = page.search("x", "source.test")

    assert [r["name"] for r in results] == ["Title 1", "Title 2"]
    assert results[0]["id"] == 1 and results[0]["slug"] == "title-1"


def test_it_costs_one_request_and_carries_no_inertia_headers(sc):
    """The payload is in the HTML any visitor gets: no handshake, no session."""
    page.search("x", "source.test")

    assert len(sc["calls"]) == 1
    url, kwargs = sc["calls"][0]
    assert url == "https://source.test/it/search"
    assert kwargs["params"] == {"q": "x"}
    assert not any(h.lower().startswith("x-inertia") for h in kwargs["headers"])


def test_the_request_carries_a_timeout(sc):
    page.search("x", "source.test")

    assert sc["calls"][0][1]["timeout"]


def test_the_page_number_is_passed_through(sc):
    page.search("x", "source.test", page=3)

    assert sc["calls"][0][1]["params"]["page"] == 3


def test_the_kind_filter_runs_before_the_cap(sc):
    """Filtering the cap's output answers "no films" for a search whose films
    were all sitting at position 22."""
    sc["payload"] = {"props": {"titles": [_title(n, "tv") for n in range(65)]
                                         + [_title(100 + n, "movie") for n in range(5)]}}

    results = page.search("x", "source.test", media_type="movie")

    assert len(results) == 5
    assert {r["type"] for r in results} == {"movie"}


def test_at_most_sixty_results_come_back(sc):
    sc["payload"] = {"props": {"titles": [_title(n) for n in range(100)]}}

    assert len(page.search("x", "source.test")) == 60


def test_the_poster_is_a_bare_filename(sc):
    """The frontend routes it through /api/image, where the host is ours."""
    sc["payload"] = {"props": {"titles": [_title(1, poster="poster-1.jpg")]}}

    assert page.search("x", "source.test")[0]["poster"] == "poster-1.jpg"


def test_a_page_with_no_titles_is_an_empty_list(sc):
    sc["payload"] = {"props": {}}

    assert page.search("nothing", "source.test") == []


def test_a_row_without_an_id_is_dropped_not_rendered(sc):
    """One malformed row must not take the shelf, or the search, with it."""
    broken = _title(2)
    del broken["id"]
    sc["payload"] = {"props": {"titles": [_title(1), broken]}}

    assert [r["id"] for r in page.search("x", "source.test")] == [1]


def test_entities_are_unescaped_exactly_once(sc):
    """BeautifulSoup already decodes the attribute. A second pass would turn a
    plot's "&amp;amp;" into "&"."""
    titled = _title(1)
    titled["name"] = 'Tom &amp; Jerry "1940"'
    sc["payload"] = {"props": {"titles": [titled]}}

    assert page.search("x", "source.test")[0]["name"] == 'Tom &amp; Jerry "1940"'


# ── A host without the /it segment (#34) ─────────────────────────────────────

@pytest.fixture
def unprefixed(monkeypatch):
    """A clone that serves /search and answers 404 to /it/search."""
    monkeypatch.setattr(page, "_locale_by_host", {})
    calls = []

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        if "/it/" in url or url.endswith("/it"):
            return _Response("not found", status_code=404)
        return _Response(_page_html({"props": {"titles": [_title(1)]}}))

    monkeypatch.setattr(page.requests, "get", fake_get)
    return calls


def test_a_host_without_the_locale_segment_is_searched_without_it(unprefixed):
    assert [r["name"] for r in page.search("x", "clone.test")] == ["Title 1"]
    assert unprefixed == ["https://clone.test/it/search", "https://clone.test/search"]


def test_the_form_that_worked_is_remembered_per_host(unprefixed):
    page.search("x", "clone.test")
    unprefixed.clear()

    page.search("y", "clone.test")

    assert unprefixed == ["https://clone.test/search"]
    assert page.site_url("clone.test", "/titles/1-a") == "https://clone.test/titles/1-a"
    assert page.site_url("other.test", "/titles/1-a") == "https://other.test/it/titles/1-a"


def test_a_page_missing_both_ways_does_not_flip_the_host(monkeypatch):
    """A title that does not exist is a 404 with or without /it."""
    monkeypatch.setattr(page, "_locale_by_host", {})
    monkeypatch.setattr(page.requests, "get",
                        lambda url, *a, **kw: _Response("nope", status_code=404))

    response = page.get_localized("source.test", "/titles/9-gone")

    assert response.status_code == 404
    assert page.locale("source.test") == "/it"


def test_a_host_that_regains_the_segment_is_followed_back(monkeypatch):
    monkeypatch.setattr(page, "_locale_by_host", {"source.test": ""})

    def fake_get(url, *args, **kwargs):
        if "/it/" in url:
            return _Response(_page_html({"props": {"titles": [_title(2)]}}))
        return _Response("not found", status_code=404)

    monkeypatch.setattr(page.requests, "get", fake_get)

    assert [r["name"] for r in page.search("x", "source.test")] == ["Title 2"]
    assert page.locale("source.test") == "/it"


# ── get_domain_version keeps its three answers ────────────────────────────────

def test_an_unreachable_domain_raises(monkeypatch):
    def boom(*a, **kw):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(page.requests, "get", boom)

    with pytest.raises(RuntimeError, match="Cannot reach"):
        page.get_domain_version("gone.test")


def test_a_page_without_a_payload_has_no_version(monkeypatch):
    """Reachable, but nothing that says it is the source: "" , not None."""
    monkeypatch.setattr(page.requests, "get",
                        lambda *a, **kw: _Response("<html><body>hi</body></html>"))

    assert page.get_domain_version("other.test") == ""


def test_an_unparseable_payload_is_none(monkeypatch):
    monkeypatch.setattr(page.requests, "get", lambda *a, **kw: _Response(
        '<html><div id="app" data-page="{not json"></div></html>'))

    assert page.get_domain_version("broken.test") is None


def test_a_good_page_reports_its_version(monkeypatch, sc):
    sc["payload"] = {"version": "abc123", "props": {}}

    assert page.get_domain_version("source.test") == "abc123"
    assert sc["calls"][0][0] == "https://source.test", "not /it: domain_recovery reads this page"


# ── AnimeUnity ────────────────────────────────────────────────────────────────
#
# The source itself is pinned in tests/test_animeunity.py: since the rebuild
# (#50) it shares nothing with the search above but the Inertia payload.


# ── /api/search ───────────────────────────────────────────────────────────────

from app.auth.permissions import ALL_PERMISSIONS  # noqa: E402
from tests.conftest import do_setup, make_user, session_for  # noqa: E402


@pytest.fixture
def signed_in(client, admin_credentials):
    do_setup(client, admin_credentials)
    user = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    client.cookies.clear()
    return session_for(client, user.id)


@pytest.fixture
def asked(monkeypatch):
    """Record what each source was asked for, without reaching either."""
    seen = {}
    from app.core import animeunity as au
    from app.routers import search as router

    monkeypatch.setattr(router, "configured_domain", lambda: "source.test")
    monkeypatch.setattr(router, "core_search",
                        lambda q, domain, **kw: seen.update(sc=(q, domain, kw)) or [])
    monkeypatch.setattr(au, "search", lambda q, **kw: seen.update(au=(q, kw)) or [])
    return seen


def test_the_page_and_kind_reach_streamingcommunity(client, signed_in, asked):
    response = client.get("/api/search?q=abc&page=4&media_type=movie")

    assert response.status_code == 200
    q, domain, kwargs = asked["sc"]
    assert (q, domain) == ("abc", "source.test")
    assert kwargs["page"] == 4 and kwargs["media_type"] == "movie"


def test_the_anime_filters_reach_animeunity(client, signed_in, asked):
    response = client.get(
        "/api/search?q=abc&source=animeunity&page=2&media_type=ova&dubbed=true")

    assert response.status_code == 200
    q, kwargs = asked["au"]
    assert q == "abc"
    assert (kwargs["page"], kwargs["media_type"], kwargs["dubbed"]) == (2, "ova", True)


def test_an_unknown_source_is_refused(client, signed_in, asked):
    """It used to fall through to StreamingCommunity in silence."""
    assert client.get("/api/search?q=abc&source=nowhere").status_code == 422


def test_a_page_past_the_bound_is_refused(client, signed_in, asked):
    assert client.get("/api/search?q=abc&page=0").status_code == 422
    assert client.get("/api/search?q=abc&page=9999").status_code == 422


def test_an_anime_kind_is_refused_for_streamingcommunity(client, signed_in, asked):
    """One pattern cannot express two vocabularies; the source has no OVAs."""
    assert client.get("/api/search?q=abc&media_type=ova").status_code == 422


def test_the_dub_filter_is_refused_for_streamingcommunity(client, signed_in, asked):
    assert client.get("/api/search?q=abc&dubbed=true").status_code == 422


def test_an_unasked_dub_filter_is_not_a_refusal(client, signed_in, asked):
    """False is the default every search sends, not a request for a filter."""
    assert client.get("/api/search?q=abc&dubbed=false").status_code == 200


def test_no_configured_domain_is_still_a_409(client, signed_in, monkeypatch):
    """The 409 has to survive the generic 502 wrapper below it."""
    from app.routers import search as router
    monkeypatch.setattr(router, "configured_domain", lambda: "")

    assert client.get("/api/search?q=abc").status_code == 409
    assert client.get("/api/search?q=abc&source=animeunity").status_code != 409
