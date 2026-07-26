"""Every outbound call on the resolve/download path must carry a timeout.

Without one, a stalled connection blocks a worker thread forever with no
exception raised — the try/except safety net around approval execution
(service._execute) only catches things that actually raise, so a hang is
invisible to it and the request sits open forever. Verified by patching
requests.get itself and asserting every call this codebase makes supplies a
timeout, rather than trusting each call site to remember one.
"""

import pytest


class _FakeResponse:
    ok = True
    status_code = 200
    text = "#EXTM3U\n"
    content = b"\x00" * 16

    def json(self):
        return {"props": {"title": {"seasons_count": 1}, "loadedSeason": {"episodes": []}}}

    def raise_for_status(self):
        pass


@pytest.fixture
def recorded_get(monkeypatch):
    calls = []

    def fake_get(url, *args, **kwargs):
        calls.append(kwargs)
        return _FakeResponse()

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    return calls


def test_get_info_tv_has_a_timeout(recorded_get):
    from app.core.tv import get_info_tv
    get_info_tv(1, "slug", "v1", "example.test")
    assert recorded_get and recorded_get[-1].get("timeout") is not None


def test_get_info_season_has_a_timeout(recorded_get):
    from app.core.tv import get_info_season
    get_info_season(1, "slug", "example.test", "v1", "token", 1)
    assert recorded_get and recorded_get[-1].get("timeout") is not None


def test_tv_get_iframe_has_a_timeout(recorded_get):
    """The fake response has no <iframe> tag, so parsing fails right after —
    that happens downstream of the call under test, so it's ignored: only the
    first requests.get (the one that used to have no timeout) is checked."""
    from app.core import tv
    with pytest.raises(Exception):
        tv._get_iframe(1, 2, "example.test", "token")
    assert recorded_get and recorded_get[0].get("timeout") is not None


def test_film_get_iframe_has_a_timeout(recorded_get):
    from app.core import film
    with pytest.raises(Exception):
        film._get_iframe(1, "example.test")
    assert recorded_get and recorded_get[0].get("timeout") is not None


def test_m3u8_fetch_text_helpers_have_a_timeout(recorded_get):
    from app.core.m3u8 import _fetch_text, _fetch_text_with_b1_fallback
    _fetch_text("https://example.test/x.m3u8")
    assert recorded_get[-1].get("timeout") is not None
    recorded_get.clear()
    _fetch_text_with_b1_fallback("https://example.test/x.m3u8")
    assert recorded_get[-1].get("timeout") is not None
