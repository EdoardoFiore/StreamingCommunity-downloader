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
