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
    # Shapes below were read off a real title page before being relied on, as
    # the metadata rule in CLAUDE.md requires - people arrive as records, not
    # as bare strings.
    "main_actors": [{"id": 1, "name": "Bryan Cranston"}, {"id": 2, "name": "Aaron Paul"}],
    "main_directors": [{"id": 9, "name": "Vince Gilligan"}],
    "original_name": "Breaking Bad",
    "original_language": "en",
    "status": "Ended",
    "quality": "HD",
    "age": 16,
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


def test_a_domain_rotation_does_not_serve_the_old_artwork(client, monkeypatch, props):
    """Artwork URLs and the plot come from whichever domain served them. Without
    the host in the key, a rotation kept handing back the old domain's images
    for six hours, long after every one of them had stopped resolving."""
    domain = ["old.test"]
    monkeypatch.setattr(metadata, "configured_domain", lambda: domain[0])

    metadata.title_metadata("tv", "1", "test-series", "v1")
    domain[0] = "new.test"
    metadata.title_metadata("tv", "1", "test-series", "v1")

    assert len(props) == 2


def test_applying_a_candidate_clears_the_metadata(client, monkeypatch, props):
    from app.core import domain_recovery

    metadata.title_metadata("tv", "1", "test-series", "v1")

    monkeypatch.setattr(domain_recovery, "is_plausible", lambda host: (True, ""))
    monkeypatch.setattr(domain_recovery, "verify", lambda host: "v9")
    monkeypatch.setattr(domain_recovery.config, "update_data", lambda changes: None)
    domain_recovery.apply_candidate("new.test")

    metadata.title_metadata("tv", "1", "test-series", "v1")
    assert len(props) == 2


# ── Cast and the rest of the title page ───────────────────────────────────────

def test_the_cast_and_crew_come_through(client, admin, props):
    data = client.get("/api/metadata/tv/1?slug=test-series&version=v1").json()

    assert data["cast"] == ["Bryan Cranston", "Aaron Paul"]
    assert data["directors"] == ["Vince Gilligan"]


def test_the_title_page_facts_come_through(client, admin, props):
    data = client.get("/api/metadata/tv/1?slug=test-series&version=v1").json()

    assert data["original_name"] == "Breaking Bad"
    assert data["original_language"] == "en"
    assert data["status"] == "Ended"
    assert data["quality"] == "HD"
    assert data["age"] == 16


def test_people_without_a_name_are_dropped_and_duplicates_collapse(monkeypatch):
    """These are a third party's records. A malformed one must cost a name,
    not the cast row."""
    from app.core import metadata

    assert metadata._people([
        {"id": 1, "name": "Ada"}, {"id": 2}, {"id": 3, "name": ""},
        None, {"id": 4, "name": "Ada"}, {"id": 5, "name": "Grace"},
    ]) == ["Ada", "Grace"]


def test_the_cast_is_bounded():
    from app.core import metadata

    many = [{"id": i, "name": f"Attore {i}"} for i in range(100)]
    assert len(metadata._people(many)) == metadata._MAX_PEOPLE


def test_a_miss_still_answers_with_every_key(client, admin, monkeypatch):
    """The router returns dict(EMPTY) when lookup fails, so a field added to
    the payload but not to EMPTY would be missing exactly when the frontend
    has least to work with."""
    from app.core import metadata, tv

    monkeypatch.setattr(
        tv, "get_title_props", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    data = client.get("/api/metadata/movie/1?slug=x&version=v1").json()

    assert set(data) == set(metadata.EMPTY)


# ── The year (issue #21) ──────────────────────────────────────────────────────

def test_a_series_is_named_after_its_premiere_not_its_latest_season():
    """Shape read off Grey's Anatomy's real title page: release_date is the
    premiere, last_air_date the season still airing. The search payload
    carries only the second, and the folder used to be named after it."""
    props = {"type": "tv", "release_date": "2005-03-27", "last_air_date": "2025-10-09"}

    assert metadata._from_props(props)["year"] == "2005"


def test_a_series_with_no_premiere_gets_no_year_rather_than_a_wrong_one():
    """A folder without a year still matches in Jellyfin; one with the latest
    season's year matches the wrong show, or none."""
    props = {"type": "tv", "release_date": None, "last_air_date": "2025-10-09"}

    assert metadata._from_props(props)["year"] is None


def test_a_film_falls_back_to_its_only_other_date():
    props = {"type": "movie", "release_date": None, "last_air_date": "2010-07-15"}

    assert metadata._from_props(props)["year"] == "2010"


@pytest.mark.parametrize("date", ["", "n/a", "20", None])
def test_a_malformed_date_is_no_year(date):
    assert metadata._from_props({"type": "tv", "release_date": date})["year"] is None


def test_the_endpoint_carries_the_year(client, admin, monkeypatch):
    monkeypatch.setattr(tv, "get_title_props", lambda *a, **k: {
        **PROPS, "type": "tv", "release_date": "2016-01-01", "last_air_date": "2025-12-25",
    })

    data = client.get("/api/metadata/tv/1?slug=test-series&version=v1").json()

    assert data["year"] == "2016"
