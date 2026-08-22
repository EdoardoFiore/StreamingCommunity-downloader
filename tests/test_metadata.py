"""Title metadata: where it comes from, and what happens when it does not arrive.

The regression pin here is that ``get_info_tv`` still returns an ``int``. It is
called by the watch poller, the seasons endpoint and the batch download path,
and several other test files monkeypatch it; widening it to return the whole
props dict is the obvious refactor and would have broken all of them silently.
"""

import pytest

from app.auth import models as auth_models
from app.auth.permissions import ALL_PERMISSIONS
from app.core import metadata, tv
from tests.conftest import do_setup, make_user, session_for


PROPS = {
    "seasons_count": 3,
    "name": "Test Series",
    "tmdb_id": 1396,
    "imdb_id": "tt0903747",
    "plot": "Un professore di chimica.",
    "score": "8.9",
}


class _FakeResponse:
    def __init__(self, payload=None, ok=True, status_code=200):
        self._payload = payload if payload is not None else {"props": {"title": PROPS}}
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return self._payload


@pytest.fixture
def title_page(monkeypatch):
    calls = []

    def fake_get(url, *args, **kwargs):
        calls.append((url, kwargs))
        return _FakeResponse()

    monkeypatch.setattr(tv.requests, "get", fake_get)
    return calls


def test_the_title_props_carry_everything_the_page_had(title_page):
    props = tv.get_title_props(1, "test-series", "v1", "example.test")
    assert props["tmdb_id"] == 1396
    assert props["plot"]


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


# ── Providers ─────────────────────────────────────────────────────────────────


TMDB_PAYLOAD = {
    "overview": "Trama da TMDB.",
    "genres": [{"name": "Dramma"}, {"name": "Crime"}],
    "vote_average": 8.876,
    "backdrop_path": "/backdrop.jpg",
    "runtime": 49,
    "images": {"logos": [{"file_path": "/logo.png"}]},
    "videos": {"results": [{"site": "YouTube", "type": "Trailer", "key": "abc123"}]},
}

PREVIEW_PAYLOAD = {
    "type": "tv",
    "runtime": 45,
    "plot": "Trama dalla sorgente.",
    "genres": [{"name": "Azione"}],
    "images": [{"type": "background", "filename": "bg.jpg"}],
}


@pytest.fixture(autouse=True)
def _empty_cache():
    metadata.clear_cache()
    yield
    metadata.clear_cache()


@pytest.fixture
def outbound(monkeypatch):
    """Record every outbound call and serve canned payloads."""
    calls = {"tmdb": [], "preview": [], "props": []}

    def fake_props(title_id, slug, version, domain):
        calls["props"].append(title_id)
        return dict(PROPS)

    def fake_tmdb_get(url, *args, **kwargs):
        calls["tmdb"].append((url, kwargs))
        return _FakeResponse(TMDB_PAYLOAD)

    def fake_preview_post(url, *args, **kwargs):
        calls["preview"].append((url, kwargs))
        return _FakeResponse(PREVIEW_PAYLOAD)

    monkeypatch.setattr(tv, "get_title_props", fake_props)
    monkeypatch.setattr(metadata.requests, "get", fake_tmdb_get)
    monkeypatch.setattr(metadata.requests, "post", fake_preview_post)
    return calls


def _with_key(value="tmdb-key"):
    auth_models.set_setting(auth_models.SETTING_TMDB_API_KEY, value)


def test_tmdb_is_used_when_a_key_is_configured(client, outbound):
    _with_key()
    result = metadata.title_metadata("tv", "1", "test-series", "v1")

    assert result["source"] == "tmdb"
    assert result["plot"] == "Trama da TMDB."
    assert result["genres"] == ["Dramma", "Crime"]
    assert result["rating"] == 8.9
    assert result["trailer_url"].endswith("abc123")
    assert result["backdrop"] == "/api/image/tmdb/w1280/backdrop.jpg"
    assert result["tmdb_id"] == 1396


def test_without_a_key_tmdb_is_never_contacted(client, outbound):
    """A panel with no key must not leak title lookups to an API it cannot use."""
    result = metadata.title_metadata("tv", "1", "test-series", "v1")

    assert result["source"] == "site"
    assert result["plot"] == "Trama dalla sorgente."
    assert outbound["tmdb"] == []
    assert outbound["preview"]


def test_a_failing_tmdb_falls_through_to_the_source(client, monkeypatch, outbound):
    _with_key()

    def boom(*a, **k):
        raise RuntimeError("TMDB down")

    monkeypatch.setattr(metadata, "tmdb_details", boom)

    result = metadata.title_metadata("tv", "1", "test-series", "v1")
    assert result["source"] == "site"
    assert result["plot"] == "Trama dalla sorgente."


def test_everything_failing_is_not_an_error(client, monkeypatch):
    monkeypatch.setattr(tv, "get_title_props", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(metadata.requests, "post", lambda *a, **k: _FakeResponse(ok=False, status_code=500))

    result = metadata.title_metadata("tv", "1", "test-series", "v1")
    assert result["source"] == "none"
    assert result["plot"] is None


def test_outbound_metadata_calls_have_timeouts(client, outbound):
    _with_key()
    metadata.title_metadata("tv", "1", "test-series", "v1")
    assert outbound["tmdb"][-1][1].get("timeout") is not None

    metadata.clear_cache()
    auth_models.set_setting(auth_models.SETTING_TMDB_API_KEY, "")
    metadata.title_metadata("tv", "2", "test-series", "v1")
    assert outbound["preview"][-1][1].get("timeout") is not None


# ── Cache ─────────────────────────────────────────────────────────────────────

def test_a_second_lookup_costs_nothing(client, outbound):
    metadata.title_metadata("tv", "1", "test-series", "v1")
    metadata.title_metadata("tv", "1", "test-series", "v1")
    assert len(outbound["preview"]) == 1


def test_the_cache_expires(client, monkeypatch, outbound):
    clock = [1000.0]
    monkeypatch.setattr(metadata, "_now", lambda: clock[0])

    metadata.title_metadata("tv", "1", "test-series", "v1")
    clock[0] += metadata._TTL_HIT + 1
    metadata.title_metadata("tv", "1", "test-series", "v1")

    assert len(outbound["preview"]) == 2


def test_adding_a_key_does_not_keep_serving_the_keyless_answer(client, outbound):
    assert metadata.title_metadata("tv", "1", "test-series", "v1")["source"] == "site"
    _with_key()
    assert metadata.title_metadata("tv", "1", "test-series", "v1")["source"] == "tmdb"


def test_a_miss_expires_sooner_than_a_hit(client, monkeypatch):
    monkeypatch.setattr(tv, "get_title_props", lambda *a, **k: {})
    monkeypatch.setattr(metadata.requests, "post",
                        lambda *a, **k: _FakeResponse(ok=False, status_code=500))
    clock = [1000.0]
    monkeypatch.setattr(metadata, "_now", lambda: clock[0])

    metadata.title_metadata("tv", "9", "test-series", "v1")
    clock[0] += metadata._TTL_MISS + 1
    assert metadata._cached(metadata._cache_key("tv", "9", False)) is None


def test_cached_tmdb_id_never_does_io(client, outbound, monkeypatch):
    """The download path reads this; it must not pay for a lookup."""
    assert metadata.cached_tmdb_id("tv", "1") is None

    metadata.title_metadata("tv", "1", "test-series", "v1")

    def boom(*a, **k):
        raise AssertionError("cached_tmdb_id made a request")

    monkeypatch.setattr(metadata.requests, "get", boom)
    monkeypatch.setattr(metadata.requests, "post", boom)
    assert metadata.cached_tmdb_id("tv", "1") == 1396


# ── Endpoints ─────────────────────────────────────────────────────────────────

@pytest.fixture
def admin(client, admin_credentials):
    do_setup(client, admin_credentials)
    user = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    client.cookies.clear()
    return user, session_for(client, user.id)


def test_the_endpoint_returns_normalised_metadata(client, admin, outbound):
    res = client.get("/api/metadata/tv/1?slug=test-series&version=v1")
    assert res.status_code == 200
    assert res.json()["plot"] == "Trama dalla sorgente."


def test_a_metadata_miss_is_a_200_not_a_502(client, admin, monkeypatch):
    """A modal with no plot is a modal with no plot, not an error page."""
    monkeypatch.setattr(tv, "get_title_props",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(metadata.requests, "post",
                        lambda *a, **k: _FakeResponse(ok=False, status_code=500))

    res = client.get("/api/metadata/movie/1?slug=x&version=v1")
    assert res.status_code == 200
    assert res.json()["source"] == "none"


def test_a_bad_media_type_is_refused(client, admin):
    assert client.get("/api/metadata/anime/1").status_code == 422


def test_a_non_numeric_title_id_is_refused(client, admin):
    assert client.get("/api/metadata/tv/abc").status_code == 400


def test_the_tmdb_key_is_never_echoed_back(client, admin):
    client.put("/api/metadata/settings", json={"tmdb_api_key": "super-secret"},
               headers={"X-CSRF-Token": admin[1]})

    body = client.get("/api/metadata/settings").json()
    assert body == {"tmdb_configured": True}
    assert "super-secret" not in str(body)


# ── TMDB image proxy ──────────────────────────────────────────────────────────

@pytest.fixture
def tmdb_image(monkeypatch):
    """Serve a fake image and record the upstream URL asked for."""
    from app.routers import images

    calls = []

    class _Image:
        ok = True
        status_code = 200
        headers = {"content-type": "image/jpeg"}
        content = b"\xff\xd8\xff"

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        return _Image()

    monkeypatch.setattr(images.requests, "get", fake_get)
    return calls


def test_the_tmdb_route_is_not_swallowed_by_the_catch_all(client, admin, tmdb_image):
    """/api/image/{filename:path} matches "tmdb/w1280/x.jpg" perfectly happily.

    If it wins, the request is answered by asking the source's CDN for a file it
    has never heard of. Declaration order is the whole fix, so it is pinned.
    """
    res = client.get("/api/image/tmdb/w1280/backdrop.jpg")

    assert res.status_code == 200
    assert tmdb_image == ["https://image.tmdb.org/t/p/w1280/backdrop.jpg"]


def test_tmdb_artwork_is_cached_by_the_browser(client, admin, tmdb_image):
    res = client.get("/api/image/tmdb/w500/logo.png")
    assert "max-age" in res.headers.get("cache-control", "")


@pytest.mark.parametrize(
    "path",
    [
        "/api/image/tmdb/w9999/backdrop.jpg",  # size not on the allowlist
        "/api/image/tmdb/w1280/backdrop.sh",   # not an image extension
        "/api/image/tmdb/w1280/back drop.jpg",  # space
    ],
)
def test_bad_tmdb_image_requests_are_refused(client, admin, tmdb_image, path):
    assert client.get(path).status_code in (400, 404)
    # Nothing reached TMDB. A request carrying a slash falls through to the
    # source catch-all instead, which is its own (host-pinned) proxy — so the
    # assertion is about where the bytes were asked for, not about silence.
    assert not [url for url in tmdb_image if "image.tmdb.org" in url]


@pytest.mark.parametrize(
    "path", ["../../etc/passwd", "..%2F..%2Fetc", "sub/dir.jpg", "a" * 200 + ".jpg"]
)
def test_the_tmdb_path_validator_rejects_directly(path):
    """Checked against the regex rather than over HTTP.

    A "../.." in a URL is normalised away by the client before the request is
    sent, so going through TestClient would test the client's path handling
    rather than this validator.
    """
    from app.routers.images import TMDB_PATH

    assert not TMDB_PATH.match(path)


def test_the_tmdb_image_host_cannot_be_influenced(client, admin, tmdb_image):
    """The host is a constant: the path never gets to say where the bytes come from."""
    client.get("/api/image/tmdb/w500/logo.png")
    assert all(url.startswith("https://image.tmdb.org/t/p/") for url in tmdb_image)
