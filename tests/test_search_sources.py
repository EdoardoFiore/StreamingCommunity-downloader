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


# ── AnimeUnity ────────────────────────────────────────────────────────────────

from app.core import animeunity  # noqa: E402

_AU_HOME = ('<html><head><meta name="csrf-token" content="tok-1">'
            '</head><body></body></html>')


class _ArchiveResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _FakeScraper:
    def __init__(self, replies):
        self.gets, self.posts = [], []
        self.replies = list(replies)

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return _ArchiveResponse(text=_AU_HOME)

    def post(self, url, json=None, headers=None, **kwargs):
        self.posts.append((url, json, headers))
        assert self.replies, f"POST {len(self.posts)} is more than the test prepared"
        return self.replies.pop(0)


def _record(n, kind="TV", dub=0):
    return {
        "id": n, "slug": f"anime-{n}", "title_eng": f"Anime {n}", "type": kind,
        "imageurl": f"https://cdn.animeunity.test/{n}.jpg", "episodes_count": 12,
        "plot": "Una trama.", "genres": [{"name": "Azione"}, {"name": "Dramma"}],
        "dub": dub, "score": "8.5", "date": "2023-01-05",
    }


def _records(*records, status_code=200):
    return _ArchiveResponse({"records": list(records)}, status_code=status_code)


@pytest.fixture
def au(monkeypatch):
    """Build a fake scraper serving the given POST replies in order.

    Both the scraper and the CSRF token are module globals that outlive a test,
    so the fixture has to reach both.
    """
    monkeypatch.setattr(animeunity, "_csrf", None)

    def build(*replies):
        fake = _FakeScraper(replies)
        monkeypatch.setattr(animeunity, "_get_scraper", lambda: fake)
        return fake

    return build


def test_the_archive_endpoint_is_used_not_livesearch(au):
    """/livesearch is capped at eight records by the source, for every query."""
    fake = au(_records(_record(1)))

    animeunity.search("x")

    assert fake.posts[0][0].endswith("/archivio/get-animes")
    assert not any("livesearch" in url for url, *_ in fake.posts)


def test_the_offset_is_thirty_per_page(au):
    fake = au(_records(), _records())

    animeunity.search("x")
    animeunity.search("x", page=3)

    assert fake.posts[0][1]["offset"] == 0
    assert fake.posts[1][1]["offset"] == 60


def test_the_dub_filter_is_sent_only_when_asked(au):
    fake = au(_records(), _records())

    animeunity.search("x")
    animeunity.search("x", dubbed=True)

    assert "dubbed" not in fake.posts[0][1]
    assert fake.posts[1][1]["dubbed"] == 1


def test_the_kind_is_translated_to_the_sources_own_word(au):
    """The wire says "movie"; AnimeUnity says "Movie"."""
    fake = au(_records(), _records())

    animeunity.search("x", media_type="movie")
    animeunity.search("x", media_type="special")

    assert fake.posts[0][1]["type"] == "Movie"
    assert fake.posts[1][1]["type"] == "Special"


def test_the_source_classification_comes_back_beside_the_type(au):
    """type stays "anime" — the whole anime flow keys off it — and the source's
    own word rides in media_type. Folding one into the other is what used to
    label every anime film a TV series."""
    au(_records(_record(1, kind="Movie")))

    result = animeunity.search("x")[0]

    assert result["type"] == "anime"
    assert result["media_type"] == "Movie"


def test_plot_and_genres_are_passed_through(au):
    """The archive sends them with the search, so the detail panel costs nothing."""
    au(_records(_record(1)))

    result = animeunity.search("x")[0]

    assert result["plot"] == "Una trama."
    assert result["genres"] == ["Azione", "Dramma"]


def test_genres_survive_an_odd_shape(au):
    """Laravel casts them differently by row; a TypeError inside a search is not
    an acceptable answer to that."""
    plain, as_json, broken = _record(1), _record(2), _record(3)
    plain["genres"] = ["Azione"]
    as_json["genres"] = '[{"name": "Commedia"}]'
    broken["genres"] = None
    au(_records(plain, as_json, broken))

    results = animeunity.search("x")

    assert [r["genres"] for r in results] == [["Azione"], ["Commedia"], []]


def test_the_dub_flag_is_passed_through(au):
    au(_records(_record(1, dub=1), _record(2, dub=0)))

    assert [r["dubbed"] for r in animeunity.search("x")] == [True, False]


def test_no_results_is_an_empty_list_not_an_error(au):
    """An anime the source does not have is not a failure of the source. This
    used to raise, and reached the panel as a red box saying search had broken."""
    au(_records())

    assert animeunity.search("nothing at all") == []


def test_the_poster_is_an_absolute_url(au):
    """Unlike StreamingCommunity's bare filename: the frontend branches on it."""
    au(_records(_record(1)))

    assert animeunity.search("x")[0]["poster"] == "https://cdn.animeunity.test/1.jpg"


def test_the_same_record_twice_is_one_card(au):
    au(_records(_record(1), _record(1)))

    assert len(animeunity.search("x")) == 1


def test_a_refused_status_is_reported(au):
    au(_records(status_code=500))

    with pytest.raises(RuntimeError, match="500"):
        animeunity.search("x")


# ── The CSRF token ────────────────────────────────────────────────────────────

def test_a_stale_csrf_token_is_renewed_once(au):
    """419 is Laravel saying the token expired. Fetching a new one and sending
    the request again is re-authentication, not a retry."""
    fake = au(_records(status_code=419), _records(_record(1)))

    assert len(animeunity.search("x")) == 1
    assert len(fake.posts) == 2, "renewed once, not retried in a loop"
    assert len(fake.gets) == 2, "one home page for the first token, one for the new"


def test_a_token_refused_twice_is_not_tried_a_third_time(au):
    fake = au(_records(status_code=419), _records(status_code=419))

    with pytest.raises(RuntimeError, match="419"):
        animeunity.search("x")
    assert len(fake.posts) == 2


def test_the_csrf_token_is_not_refetched_per_search(au):
    fake = au(_records(), _records())

    animeunity.search("x")
    animeunity.search("y")

    assert len(fake.gets) == 1, "the token is good for a while and costs a page load"
    assert fake.posts[1][2]["X-CSRF-TOKEN"] == "tok-1"


def test_an_expired_token_is_refetched(au, monkeypatch):
    fake = au(_records(), _records())
    animeunity.search("x")

    monkeypatch.setattr(animeunity, "_now", lambda: animeunity.time.time() + 3600)
    animeunity.search("y")

    assert len(fake.gets) == 2


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
