"""Title metadata: where it comes from, and what happens when it does not arrive.

The regression pin here is that ``get_info_tv`` still returns an ``int``. It is
called by the watch poller, the seasons endpoint and the batch download path,
and several other test files monkeypatch it; widening it to return the whole
props dict is the obvious refactor and would have broken all of them silently.
"""

import pytest

from app.core import tv


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
