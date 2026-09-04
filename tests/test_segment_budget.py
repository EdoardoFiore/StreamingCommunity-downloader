"""Why one download in three failed once three were running.

The report: StreamingCommunity downloads work alone, but queue three and at
least one dies. The logs show what the panel was doing to the CDN —

    Segment HTTP 503 (attempt 1/3): ...0007-0745.ts
    ... × dozens, then
    Failed to resolve 'au-u2-01.vix-content.net'
    ([Errno -3] Temporary failure in name resolution)

Two multipliers, both ours. ``max_segment_workers`` sized *each* download's
pool, so the shipped defaults (3 concurrent × 16 workers) meant 48 requests at
once against one CDN edge. And every one of those requests was a bare
``requests.get``: a fresh TCP connection, a fresh TLS handshake and a fresh DNS
lookup, thousands of times per download. The edge shed the load with 503s and
the container's resolver stopped answering at all — which ``get_req_ts`` cannot
tell apart from a source that has genuinely gone away, so the segment was
written off and the download failed on the completeness check.

So: one pooled session per download, and a budget that counts the whole
process rather than each download separately.
"""

import threading

import pytest
import requests

from app.core import m3u8
from app.core.m3u8 import M3U8_Segments


class _Response:
    def __init__(self, status_code=200, content=b"x" * 10):
        self.status_code = status_code
        self.content = content
        self.ok = 200 <= status_code < 300


class _Bar:
    def __init__(self, total=0, **kwargs):
        self.total = total
        self.n = 0

    def update(self, n=1, bytes=0):
        self.n += n

    def close(self):
        pass

    def refresh(self):
        pass


@pytest.fixture(autouse=True)
def _fresh_budget():
    """The budget is process-wide by design, so it has to be reset per test."""
    m3u8._segment_budget = None
    m3u8._segment_budget_size = 0
    yield
    m3u8._segment_budget = None
    m3u8._segment_budget_size = 0


@pytest.fixture
def workers(monkeypatch):
    """Set max_segment_workers, which now sizes the panel and not a download."""
    def _set(count):
        monkeypatch.setattr(m3u8, "get_settings", lambda: {"max_segment_workers": count})
    return _set


def _segments(tmp_path, name="a", count=8, referer=None):
    seg = M3U8_Segments("https://cdn.example.test/playlist.m3u8",
                        temp_dir=str(tmp_path / name),
                        progress_factory=lambda **kw: _Bar(**kw),
                        referer=referer)
    seg.segments = [f"https://cdn.example.test/{name}/seg-{i}.ts" for i in range(count)]
    return seg


# ── the budget ────────────────────────────────────────────────────────────────

def test_three_downloads_do_not_open_three_pools(tmp_path, workers, monkeypatch):
    """The actual defect. Concurrent downloads used to multiply the setting.

    Three downloads at four workers each opened twelve sockets; the source only
    ever agreed to four. The assertion is on requests genuinely in flight at the
    same moment, which is what the CDN counts too.
    """
    workers(4)
    in_flight = 0
    peak = 0
    guard = threading.Lock()

    def fake_get(self, url, *args, **kwargs):
        nonlocal in_flight, peak
        with guard:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            # Long enough that the threads genuinely overlap rather than
            # queueing neatly one behind another.
            threading.Event().wait(0.005)
            return _Response(200)
        finally:
            with guard:
                in_flight -= 1

    monkeypatch.setattr(requests.Session, "get", fake_get)

    downloads = [_segments(tmp_path, name, count=24) for name in ("a", "b", "c")]
    threads = [threading.Thread(target=seg.download_ts) for seg in downloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert peak <= 4, f"{peak} requests in flight, budget was 4"
    # And all three still finished — a budget that starved a download would
    # trip the completeness check instead.
    for seg in downloads:
        assert seg._missing_segments() == []


def test_one_download_still_gets_the_whole_budget(tmp_path, workers, monkeypatch):
    """Bounding the total must not slow the ordinary case to a crawl.

    A single download is the only claimant, so it may use every permit — the
    setting means what it always meant when nothing else is running.
    """
    workers(4)
    in_flight = 0
    peak = 0
    guard = threading.Lock()

    def fake_get(self, url, *args, **kwargs):
        nonlocal in_flight, peak
        with guard:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            threading.Event().wait(0.01)
            return _Response(200)
        finally:
            with guard:
                in_flight -= 1

    monkeypatch.setattr(requests.Session, "get", fake_get)

    _segments(tmp_path, count=24).download_ts()

    assert peak == 4


def test_the_budget_follows_the_setting(workers):
    """An administrator raising the limit is not made to restart the panel."""
    workers(4)
    assert m3u8.segment_budget(4)._value == 4

    workers(9)
    assert m3u8.segment_budget(9)._value == 9


def test_the_budget_is_never_zero():
    """A setting of 0 would deadlock every download rather than pause it."""
    assert m3u8.segment_budget(0)._value == 1


def test_a_waiting_thread_does_not_hold_a_permit(tmp_path, workers, monkeypatch):
    """Backoff must free the slot.

    A thread sleeping out a 503 is not using the source; holding a permit
    through the wait would let a handful of throttled segments idle the whole
    budget, which is the congestion this exists to avoid.
    """
    workers(1)
    held_during_sleep = []
    monkeypatch.setattr(m3u8.time, "sleep",
                        lambda s: held_during_sleep.append(m3u8._segment_budget._value))

    replies = [_Response(503), _Response(200)]
    monkeypatch.setattr(requests.Session, "get",
                        lambda self, url, *a, **k: replies.pop(0))

    seg = _segments(tmp_path, count=1)
    assert seg.get_req_ts("https://cdn.example.test/a/seg-0.ts") is not None

    # One permit, and it was back on the shelf while the thread waited.
    assert held_during_sleep == [1]


# ── the session ───────────────────────────────────────────────────────────────

def test_every_segment_rides_one_connection(tmp_path, workers, monkeypatch):
    """The DNS half of the bug.

    A bare requests.get resolves the host again for every segment. One session
    per download means one lookup, which is what stopped the container's
    resolver answering "Temporary failure in name resolution" under load.
    """
    workers(4)
    sessions_used = []

    def fake_get(self, url, *args, **kwargs):
        sessions_used.append(id(self))
        return _Response(200)

    monkeypatch.setattr(requests.Session, "get", fake_get)

    seg = _segments(tmp_path, count=12)
    seg.download_ts()

    assert len(sessions_used) == 12
    assert len(set(sessions_used)) == 1


def test_the_pool_is_sized_for_the_workers(workers):
    """urllib3's default of ten would have sixteen workers discarding
    connections and reopening them — the same problem, in miniature."""
    workers(16)
    seg = M3U8_Segments("https://cdn.example.test/playlist.m3u8")

    adapter = seg._session.get_adapter("https://cdn.example.test/x.ts")
    assert adapter._pool_maxsize == 16
    # Our own retry policy is the only one: urllib3's would multiply it.
    assert adapter.max_retries.total == 0


def test_a_segment_carries_the_same_agent_every_time(tmp_path, workers, monkeypatch):
    """get_headers() picks a new agent per call, so a single film used to send
    thousands of different ones down what is now one connection."""
    workers(4)
    agents = set()

    def fake_get(self, url, *args, **kwargs):
        agents.add(kwargs["headers"]["user-agent"])
        return _Response(200)

    monkeypatch.setattr(requests.Session, "get", fake_get)

    _segments(tmp_path, count=10).download_ts()

    assert len(agents) == 1


def test_a_segment_carries_the_referer(tmp_path, workers, monkeypatch):
    """Segments were the one fetch that dropped it — _headers() existed and
    was used for the playlists only."""
    workers(4)
    seen = {}

    def fake_get(self, url, *args, **kwargs):
        seen.update(kwargs["headers"])
        return _Response(200)

    monkeypatch.setattr(requests.Session, "get", fake_get)

    seg = _segments(tmp_path, count=1, referer="https://vixcloud.co/embed/1")
    seg.get_req_ts("https://cdn.example.test/a/seg-0.ts")

    assert seen["referer"] == "https://vixcloud.co/embed/1"


def test_a_segment_fetch_carries_both_timeouts(tmp_path, workers, monkeypatch):
    """A stalled read blocks a worker as thoroughly as a stalled connect, and
    only one of the two used to be bounded separately."""
    workers(4)
    seen = []

    def fake_get(self, url, *args, **kwargs):
        seen.append(kwargs.get("timeout"))
        return _Response(200)

    monkeypatch.setattr(requests.Session, "get", fake_get)

    _segments(tmp_path, count=1).get_req_ts("https://cdn.example.test/a/seg-0.ts")

    connect, read = seen[0]
    assert connect > 0 and read > 0
