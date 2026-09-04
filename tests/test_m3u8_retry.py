"""Both M3U8 fetches survive the errors this CDN hands out routinely.

Issue #9: the rendition playlist was fetched with a bare requests.get, so a
single transient 500 aborted the whole download — while the identical failure on
the fetch one request earlier would have been retried. Both now go through one
helper, so the next fetch added here cannot quietly miss out.
"""

import pytest
import requests

from app.core.m3u8 import M3U8_Segments


MASTER = (
    "#EXTM3U\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\n'
    "https://cdn.example.test/1080/playlist.m3u8\n"
)
RENDITION = (
    "#EXTM3U\n"
    "#EXTINF:6.0,\n"
    "https://cdn.example.test/1080/seg-1.ts\n"
    "#EXTINF:6.0,\n"
    "https://cdn.example.test/1080/seg-2.ts\n"
)


class _Response:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text
        self.ok = 200 <= status_code < 300


@pytest.fixture
def no_sleep(monkeypatch):
    """Retries must not make the suite wait for real."""
    slept = []
    monkeypatch.setattr("app.core.m3u8.time.sleep", lambda s: slept.append(s))
    return slept


@pytest.fixture
def responses(monkeypatch):
    """Queue a reply per URL; each call pops the next one for that URL."""
    queued: dict[str, list] = {}
    calls: list[str] = []

    def fake_get(self, url, *args, **kwargs):
        calls.append(url)
        for prefix, replies in queued.items():
            if url.startswith(prefix) and replies:
                reply = replies.pop(0)
                if isinstance(reply, Exception):
                    raise reply
                return reply
        return _Response(404)

    monkeypatch.setattr(requests.Session, "get", fake_get)
    return queued, calls


def _segments(url="https://cdn.example.test/master.m3u8"):
    return M3U8_Segments(url, "/tmp/does-not-matter", referer="https://example.test/")


def test_a_transient_error_on_the_rendition_no_longer_kills_the_download(responses, no_sleep):
    queued, calls = responses
    queued["https://cdn.example.test/master.m3u8"] = [_Response(200, MASTER)]
    queued["https://cdn.example.test/1080/playlist.m3u8"] = [
        _Response(500), _Response(200, RENDITION),
    ]

    segments = _segments()
    segments.get_info()

    assert len(segments.segments) == 2
    assert calls.count("https://cdn.example.test/1080/playlist.m3u8") == 2


def test_the_rendition_falls_back_to_b1_on_403(responses, no_sleep):
    queued, calls = responses
    queued["https://cdn.example.test/master.m3u8"] = [_Response(200, MASTER)]
    queued["https://cdn.example.test/1080/playlist.m3u8?b=1"] = [_Response(200, RENDITION)]
    queued["https://cdn.example.test/1080/playlist.m3u8"] = [_Response(403)]

    segments = _segments()
    segments.get_info()

    assert len(segments.segments) == 2
    assert "https://cdn.example.test/1080/playlist.m3u8?b=1" in calls
    # A different URL is worth trying at once; the wait is for retrying the same one.
    assert no_sleep == []


def test_a_connection_error_is_retried(responses, no_sleep):
    queued, calls = responses
    queued["https://cdn.example.test/master.m3u8"] = [_Response(200, MASTER)]
    queued["https://cdn.example.test/1080/playlist.m3u8"] = [
        requests.ConnectionError("connection reset"), _Response(200, RENDITION),
    ]

    segments = _segments()
    segments.get_info()

    assert len(segments.segments) == 2


def test_a_persistent_error_still_gives_up_and_says_why(responses, no_sleep):
    queued, calls = responses
    queued["https://cdn.example.test/master.m3u8"] = [_Response(200, MASTER)]
    queued["https://cdn.example.test/1080/playlist.m3u8"] = [_Response(500)] * 3

    with pytest.raises(RuntimeError, match="rendition M3U8: HTTP 500"):
        _segments().get_info()

    assert calls.count("https://cdn.example.test/1080/playlist.m3u8") == 3
    # Three attempts, two waits: nothing sleeps after the final failure.
    assert len(no_sleep) == 2


def test_the_first_fetch_keeps_its_own_retry(responses, no_sleep):
    """The behaviour that already existed must survive the shared helper."""
    queued, calls = responses
    queued["https://cdn.example.test/master.m3u8?b=1"] = [
        _Response(200, "#EXTM3U\n#EXTINF:6.0,\nhttps://cdn.example.test/seg-1.ts\n")
    ]
    queued["https://cdn.example.test/master.m3u8"] = [_Response(403)]

    segments = _segments()
    segments.get_info()

    assert len(segments.segments) == 1
    assert "https://cdn.example.test/master.m3u8?b=1" in calls


def test_a_url_that_already_carries_b1_is_not_given_it_twice(responses, no_sleep):
    queued, calls = responses
    queued["https://cdn.example.test/master.m3u8?b=1"] = [_Response(403), _Response(403), _Response(403)]

    with pytest.raises(RuntimeError, match="playlist M3U8: HTTP 403"):
        _segments("https://cdn.example.test/master.m3u8?b=1").get_info()

    assert all(url.count("b=1") == 1 for url in calls)


def test_every_attempt_carries_a_timeout(responses, no_sleep, monkeypatch):
    """The reason this codebase has a timeout test at all: a stalled connection
    raises nothing and blocks a worker thread forever."""
    kwargs_seen = []
    real_get = requests.Session.get

    def spy(self, url, *args, **kwargs):
        kwargs_seen.append(kwargs)
        return real_get(self, url, *args, **kwargs)

    queued, _ = responses
    queued["https://cdn.example.test/master.m3u8"] = [_Response(200, MASTER)]
    queued["https://cdn.example.test/1080/playlist.m3u8"] = [
        _Response(500), _Response(200, RENDITION),
    ]
    monkeypatch.setattr(requests.Session, "get", spy)

    _segments().get_info()

    assert kwargs_seen and all(k.get("timeout") for k in kwargs_seen)
