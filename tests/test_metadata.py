"""Title metadata: one provider, the title page's own props.

Two regression pins here, both for mistakes that were actually made.

``get_info_tv`` must still return an ``int``. It is called by the watch poller,
the seasons endpoint and the batch download path, and several other test files
monkeypatch it; widening it to return the whole props dict is the obvious
refactor and would have broken all of them silently.

And the answer must carry the score and the trailer. It was once documented as
having neither, on the strength of reading the code rather than calling it — the
site publishes both, and an intermediate provider that never worked was hiding
them.
"""

import pytest

from app.auth.permissions import ALL_PERMISSIONS
from app.core import metadata, tv
from tests.conftest import do_setup, make_user, session_for


PROPS = {
    "seasons_count": 3,
    "name": "Test Series",
    "tmdb_id": 1396,
    "imdb_id": "tt0903747",
    "plot": "Un professore di chimica.",
    "score": "9.4",
    "trailers": [{"youtube_id": "siteTrailer"}],
    "genres": [{"name": "Dramma"}, {"name": "Crime"}],
    "images": [
        {"type": "background", "filename": "bg.jpg"},
        {"type": "logo", "filename": "logo.jpg"},
    ],
}


class _FakeResponse:
    def __init__(self, payload=None, ok=True, status_code=200):
        self._payload = payload if payload is not None else {"props": {"title": PROPS}}
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _empty_cache():
    metadata.clear_cache()
    yield
    metadata.clear_cache()


@pytest.fixture
def title_page(monkeypatch):
    calls = []

    def fake_get(url, *args, **kwargs):
        calls.append((url, kwargs))
        return _FakeResponse()

    monkeypatch.setattr(tv.requests, "get", fake_get)
    return calls


@pytest.fixture
def props(monkeypatch):
    """The provider, recording how often it is asked."""
    calls = []

    def fake_props(title_id, slug, version, domain):
        calls.append(title_id)
        return dict(PROPS)

    monkeypatch.setattr(tv, "get_title_props", fake_props)
    return calls


# ── The title page ────────────────────────────────────────────────────────────

def test_the_title_props_carry_everything_the_page_had(title_page):
    result = tv.get_title_props(1, "test-series", "v1", "example.test")
    assert result["tmdb_id"] == 1396
    assert result["plot"]


def test_get_info_tv_still_returns_an_int(title_page):
    """The poller, the seasons endpoint and the batch path all depend on this."""
    result = tv.get_info_tv(1, "test-series", "v1", "example.test")
    assert result == 3
    assert isinstance(result, int)


def test_reading_the_props_costs_one_request(title_page):
    tv.get_title_props(1, "test-series", "v1", "example.test")
    assert len(title_page) == 1


def test_the_props_request_has_a_timeout(title_page):
    tv.get_title_props(1, "test-series", "v1", "example.test")
    assert title_page[-1][1].get("timeout") is not None


def test_a_failed_page_still_raises_the_same_error(monkeypatch):
    monkeypatch.setattr(
        tv.requests, "get", lambda *a, **k: _FakeResponse(ok=False, status_code=404)
    )
    with pytest.raises(RuntimeError, match="Cannot fetch TV info"):
        tv.get_info_tv(1, "test-series", "v1", "example.test")


# ── What the provider produces ────────────────────────────────────────────────

def test_the_provider_fills_every_field(client, props):
    result = metadata.title_metadata("tv", "1", "test-series", "v1")

    assert result["source"] == "site"
    assert result["plot"] == PROPS["plot"]
    assert result["genres"] == ["Dramma", "Crime"]
    assert result["tmdb_id"] == 1396


def test_the_score_and_trailer_are_not_dropped(client, props):
    """The regression pin: the site publishes both, and they were once lost."""
    result = metadata.title_metadata("tv", "1", "test-series", "v1")

    assert result["rating"] == 9.4
    assert result["trailer_url"] == "https://www.youtube.com/watch?v=siteTrailer"
    assert result["logo"] == "/api/image/logo.jpg"
    assert result["backdrop"] == "/api/image/bg.jpg"


def test_metadata_costs_no_request_of_its_own(client, props):
    """The props are fetched to find tmdb_id anyway; nothing else is called."""
    metadata.title_metadata("tv", "1", "test-series", "v1")
    assert len(props) == 1


def test_a_cover_stands_in_for_a_missing_backdrop(client, monkeypatch):
    monkeypatch.setattr(tv, "get_title_props", lambda *a, **k: {
        **PROPS, "images": [{"type": "cover", "filename": "cover.jpg"}],
    })
    assert metadata.title_metadata("tv", "1", "s", "v1")["backdrop"] == "/api/image/cover.jpg"


def test_a_title_page_that_fails_is_not_an_error(client, monkeypatch):
    monkeypatch.setattr(
        tv, "get_title_props", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )

    result = metadata.title_metadata("tv", "1", "test-series", "v1")
    assert result["source"] == "none"
    assert result["plot"] is None


def test_no_slug_means_no_lookup(client, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("asked for props without a slug")

    monkeypatch.setattr(tv, "get_title_props", boom)
    assert metadata.title_metadata("tv", "1", "", "v1")["source"] == "none"


# ── Cache ─────────────────────────────────────────────────────────────────────

def test_a_second_lookup_costs_nothing(client, props):
    metadata.title_metadata("tv", "1", "test-series", "v1")
    metadata.title_metadata("tv", "1", "test-series", "v1")
    assert len(props) == 1


def test_the_cache_expires(client, monkeypatch, props):
    clock = [1000.0]
    monkeypatch.setattr(metadata, "_now", lambda: clock[0])

    metadata.title_metadata("tv", "1", "test-series", "v1")
    clock[0] += metadata._TTL_HIT + 1
    metadata.title_metadata("tv", "1", "test-series", "v1")

    assert len(props) == 2


def test_a_miss_expires_sooner_than_a_hit(client, monkeypatch):
    monkeypatch.setattr(tv, "get_title_props", lambda *a, **k: {})
    clock = [1000.0]
    monkeypatch.setattr(metadata, "_now", lambda: clock[0])

    metadata.title_metadata("tv", "9", "test-series", "v1")
    clock[0] += metadata._TTL_MISS + 1
    assert metadata._cached(metadata._cache_key("tv", "9")) is None


def test_films_and_series_do_not_share_a_cache_entry(client, props):
    metadata.title_metadata("tv", "1", "test-series", "v1")
    metadata.title_metadata("movie", "1", "test-series", "v1")
    assert len(props) == 2


def test_cached_tmdb_id_never_does_io(client, props, monkeypatch):
    """A stream fallback would read this; it must not pay for a lookup."""
    assert metadata.cached_tmdb_id("tv", "1") is None

    metadata.title_metadata("tv", "1", "test-series", "v1")

    def boom(*a, **k):
        raise AssertionError("cached_tmdb_id made a request")

    monkeypatch.setattr(tv, "get_title_props", boom)
    assert metadata.cached_tmdb_id("tv", "1") == 1396


# ── Endpoint ──────────────────────────────────────────────────────────────────

@pytest.fixture
def admin(client, admin_credentials):
    do_setup(client, admin_credentials)
    user = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    client.cookies.clear()
    return user, session_for(client, user.id)


def test_the_endpoint_returns_normalised_metadata(client, admin, props):
    res = client.get("/api/metadata/tv/1?slug=test-series&version=v1")
    assert res.status_code == 200
    assert res.json()["plot"] == PROPS["plot"]


def test_a_metadata_miss_is_a_200_not_a_502(client, admin, monkeypatch):
    """A modal with no plot is a modal with no plot, not an error page."""
    monkeypatch.setattr(
        tv, "get_title_props", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )

    res = client.get("/api/metadata/movie/1?slug=x&version=v1")
    assert res.status_code == 200
    assert res.json()["source"] == "none"


def test_a_bad_media_type_is_refused(client, admin):
    assert client.get("/api/metadata/anime/1").status_code == 422


def test_a_non_numeric_title_id_is_refused(client, admin):
    assert client.get("/api/metadata/tv/abc").status_code == 400


def test_there_is_nothing_left_to_configure(client, admin):
    """The provider needs no credential, so there is no settings endpoint."""
    assert client.get("/api/metadata/settings").status_code in (400, 404, 422)
