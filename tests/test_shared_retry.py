"""``with_retry``: a source that is merely unwell costs a wait, not the job.

The loops this replaced inspected ``response.status_code``. A timeout raises
before there is a status, so it fell straight through the retry loop and killed
the download during stream resolution — the one failure a flapping source
produces most reliably was the one never retried. Those are the first two tests
here.
"""

import pytest
import requests

from app.core import _shared


class _Response:
    def __init__(self, status_code=200, content=b"\x00\x11\xff"):
        self.status_code = status_code
        self.content = content

    @property
    def ok(self):
        return 200 <= self.status_code < 300


@pytest.fixture
def no_sleep(monkeypatch):
    """Retries must not make the suite wait for real."""
    slept = []
    monkeypatch.setattr("app.core._shared.time.sleep", slept.append)
    return slept


def _serving(*replies):
    """A fake ``requests.get`` replaying *replies*; an Exception is raised.

    Running past the end is an error rather than a repeat, so a test that
    expects four attempts fails loudly when the code makes five.
    """
    queue = list(replies)
    calls = []

    def fake_get(url, *args, **kwargs):
        calls.append((url, kwargs))
        assert queue, f"request {len(calls)} is more than the test prepared"
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    fake_get.calls = calls
    return fake_get


def _fetch_key(monkeypatch, fake_get):
    monkeypatch.setattr(_shared.requests, "get", fake_get)
    return _shared._get_m3u8_key({}, {}, "https://vixcloud.co/embed/1")


# ── The bug ───────────────────────────────────────────────────────────────────

def test_a_timeout_is_retried_rather_than_fatal(monkeypatch, no_sleep):
    fake = _serving(requests.exceptions.Timeout(), requests.exceptions.Timeout(), _Response())

    assert _fetch_key(monkeypatch, fake) == "0011ff"
    assert len(fake.calls) == 3
    assert no_sleep == [1, 2]


def test_a_connection_error_is_retried(monkeypatch, no_sleep):
    fake = _serving(requests.exceptions.ConnectionError(), _Response())

    assert _fetch_key(monkeypatch, fake) == "0011ff"
    assert len(fake.calls) == 2


def test_the_last_transport_error_reaches_the_caller(monkeypatch, no_sleep):
    """Not swallowed into a generic message: the caller needs to know it timed out."""
    fake = _serving(*[requests.exceptions.Timeout()] * 4)

    with pytest.raises(requests.exceptions.Timeout):
        _fetch_key(monkeypatch, fake)
    assert len(fake.calls) == 4


# ── Statuses ──────────────────────────────────────────────────────────────────

def test_a_verdict_is_not_retried(monkeypatch, no_sleep):
    """A 4xx is an answer, not congestion. Repeating it only delays the same one."""
    fake = _serving(_Response(status_code=404))

    with pytest.raises(RuntimeError, match="404"):
        _fetch_key(monkeypatch, fake)
    assert len(fake.calls) == 1
    assert no_sleep == []


def test_a_server_error_is_retried_and_then_gives_up(monkeypatch, no_sleep):
    fake = _serving(*[_Response(status_code=503)] * 4)

    with pytest.raises(RuntimeError, match="503"):
        _fetch_key(monkeypatch, fake)
    assert len(fake.calls) == 4
    assert no_sleep == [1, 2, 4], "four attempts, three waits: nothing sleeps after the last"


def test_too_many_requests_is_retried(monkeypatch, no_sleep):
    """429 is the source asking for room, which is exactly what a wait gives it."""
    fake = _serving(_Response(status_code=429), _Response())

    assert _fetch_key(monkeypatch, fake) == "0011ff"
    assert len(fake.calls) == 2


# ── The second call site ──────────────────────────────────────────────────────

_PLAYLIST = (
    "#EXTM3U\n"
    '#EXT-X-KEY:METHOD=AES-128,URI="https://vixcloud.co/storage/enc.key"\n'
    "#EXTINF:6.0,\nseg0.ts\n"
)


def test_the_playlist_key_path_retries_too(monkeypatch, no_sleep):
    """Covered rather than assumed: it was the duplicate of the loop above."""
    from app.core import m3u8

    monkeypatch.setattr(m3u8, "_fetch_text_with_b1_fallback", lambda url, **kw: _PLAYLIST)
    fake = _serving(requests.exceptions.Timeout(), _Response())
    monkeypatch.setattr(_shared.requests, "get", fake)

    key = _shared.fetch_key_from_playlist("https://vixcloud.co/playlist/1", "https://ref/")

    assert key == "0011ff"
    assert len(fake.calls) == 2
