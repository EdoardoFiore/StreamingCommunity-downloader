"""Why one download in three failed, and why lowering a number was not enough.

The first report: StreamingCommunity downloads work alone, but queue three and
at least one dies —

    Segment HTTP 503 (attempt 1/3): ...0007-0745.ts
    ... × dozens, then
    Failed to resolve 'au-u2-01.vix-content.net'
    ([Errno -3] Temporary failure in name resolution)

Two multipliers, both ours. ``max_segment_workers`` sized *each* download's
pool, so the shipped defaults (3 concurrent × 16 workers) meant 48 requests at
once against one CDN edge. And every one of those was a bare ``requests.get``:
a fresh TCP connection, a fresh TLS handshake and a fresh DNS lookup, thousands
of times per download. The edge shed the load with 503s and the container's
resolver stopped answering at all — which ``get_req_ts`` cannot tell apart from
a source that has genuinely gone away, so the segment was written off and the
download failed on the completeness check.

One pooled session per download fixed the resolver. The next run showed what
was left: no DNS errors at all, and 503s from the very first segments of a
*single* download with nothing else running. Sixteen at once is simply more
than this edge will serve.

So the ceiling cannot be a constant either — too high and the source sheds,
too low and a healthy one is downloaded at a crawl. The setting became a
ceiling to discover under: halve the allowance when the source pushes back,
creep up while it is serving, and honour Retry-After when it says so outright.
"""

import threading

import pytest
import requests

from app.core import m3u8
from app.core.m3u8 import M3U8_Segments


class _Response:
    def __init__(self, status_code=200, content=b"x" * 10, headers=None):
        self.status_code = status_code
        self.content = content
        self.ok = 200 <= status_code < 300
        # A real Response always has these, and get_req_ts reads Retry-After
        # off a rejection now.
        self.headers = headers or {}


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


class _Clock:
    """A hand-wound monotonic clock, so the penalty window is not a real wait."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def _fresh_budget():
    """The limiter is process-wide by design, so it has to be reset per test."""
    m3u8._segment_budget = None
    yield
    m3u8._segment_budget = None


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


# ── the ceiling is shared ─────────────────────────────────────────────────────

def test_three_downloads_do_not_open_three_pools(tmp_path, workers, monkeypatch):
    """The first defect. Concurrent downloads used to multiply the setting.

    Three downloads at four workers each opened twelve sockets; the source only
    ever agreed to four. The assertion is on requests genuinely in flight at the
    same moment, which is what the edge counts too.
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

    assert peak <= 4, f"{peak} requests in flight, ceiling was 4"
    # And all three still finished — a ceiling that starved a download would
    # trip the completeness check instead.
    for seg in downloads:
        assert seg._missing_segments() == []


def test_one_download_still_gets_the_whole_ceiling(tmp_path, workers, monkeypatch):
    """Bounding the total must not slow the ordinary case to a crawl.

    A single download against a source that is serving is the only claimant, so
    it may use every slot — the setting means what it always meant when nothing
    else is running and nothing is pushing back.
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


def test_the_ceiling_follows_the_setting(workers):
    """An administrator raising the limit is not made to restart the panel."""
    workers(4)
    assert m3u8.segment_budget(4).maximum == 4

    limiter = m3u8.segment_budget(9)
    assert limiter.maximum == 9
    assert limiter.allowance == 9


def test_the_ceiling_is_never_zero():
    """A setting of 0 would deadlock every download rather than pause it."""
    assert m3u8.segment_budget(0).maximum == 1


def test_lowering_the_ceiling_lowers_the_allowance():
    """The learnt number may sit under the ceiling but never above it."""
    limiter = m3u8.segment_budget(16)
    limiter.resize(4)

    assert limiter.maximum == 4
    assert limiter.allowance == 4


def test_a_waiting_thread_does_not_hold_a_slot(tmp_path, workers, monkeypatch):
    """Backoff must free the slot.

    A thread sleeping out a 503 is not using the source; holding its slot
    through the wait would let a handful of throttled segments idle the whole
    allowance, which is the congestion this exists to avoid.
    """
    workers(1)
    in_flight_during_sleep = []
    monkeypatch.setattr(
        m3u8.time, "sleep",
        lambda s: in_flight_during_sleep.append(m3u8._segment_budget.in_flight),
    )

    replies = [_Response(503), _Response(200)]
    monkeypatch.setattr(requests.Session, "get",
                        lambda self, url, *a, **k: replies.pop(0))

    seg = _segments(tmp_path, count=1)
    assert seg.get_req_ts("https://cdn.example.test/a/seg-0.ts") is not None

    assert in_flight_during_sleep == [0]


# ── finding the source's tolerance ────────────────────────────────────────────

def test_pushback_halves_the_allowance():
    """Sixteen at once is more than this edge will serve. Ask for less."""
    clock = _Clock()
    limiter = m3u8.AdaptiveLimiter(16, now=clock)

    limiter.penalise()
    assert limiter.allowance == 8

    clock.advance(m3u8.PENALTY_INTERVAL)
    limiter.penalise()
    assert limiter.allowance == 4


def test_a_burst_of_rejections_costs_one_cut():
    """A hundred 503s arriving in the same instant describe one moment.

    Cutting per response would collapse the allowance to the floor on the first
    burst and spend the rest of the download climbing back out of it.
    """
    clock = _Clock()
    limiter = m3u8.AdaptiveLimiter(16, now=clock)

    for _ in range(100):
        limiter.penalise()

    assert limiter.allowance == 8


def test_the_allowance_has_a_floor():
    """Shrinking to nothing would stall the download rather than slow it."""
    clock = _Clock()
    limiter = m3u8.AdaptiveLimiter(16, now=clock)

    for _ in range(20):
        clock.advance(m3u8.PENALTY_INTERVAL)
        limiter.penalise()

    assert limiter.allowance == m3u8.MIN_SEGMENT_ALLOWANCE


def test_the_floor_never_exceeds_the_ceiling():
    """A panel deliberately set to 1 must not be widened back up to 2."""
    clock = _Clock()
    limiter = m3u8.AdaptiveLimiter(1, now=clock)

    limiter.penalise()

    assert limiter.allowance == 1


def test_the_allowance_climbs_back_while_the_source_serves():
    """The cut is temporary. A source that recovers gets asked for more again."""
    clock = _Clock()
    limiter = m3u8.AdaptiveLimiter(16, now=clock)
    limiter.penalise()
    assert limiter.allowance == 8

    # One extra slot per allowance served, not one per segment: the width has
    # to be earned at its current size before it widens.
    for _ in range(7):
        limiter.reward()
    assert limiter.allowance == 8

    limiter.reward()
    assert limiter.allowance == 9


def test_the_climb_stops_at_the_ceiling():
    limiter = m3u8.AdaptiveLimiter(4)

    for _ in range(100):
        limiter.reward()

    assert limiter.allowance == 4


def test_a_503_teaches_the_limiter(tmp_path, workers, monkeypatch):
    """The wiring: pushback seen by get_req_ts has to reach the limiter."""
    workers(16)
    monkeypatch.setattr(m3u8.time, "sleep", lambda s: None)
    monkeypatch.setattr(requests.Session, "get",
                        lambda self, url, *a, **k: _Response(503))

    seg = _segments(tmp_path, count=1)
    seg.get_req_ts("https://cdn.example.test/a/seg-0.ts")

    assert seg._budget.allowance < 16


def test_a_verdict_teaches_it_nothing(tmp_path, workers, monkeypatch):
    """A 404 is not congestion. Narrowing on one would punish the panel for a
    segment the source was never going to serve at any rate."""
    workers(16)
    monkeypatch.setattr(requests.Session, "get",
                        lambda self, url, *a, **k: _Response(404))

    seg = _segments(tmp_path, count=1)
    seg.get_req_ts("https://cdn.example.test/a/seg-0.ts")

    assert seg._budget.allowance == 16


def test_a_refused_connection_counts_as_pushback(tmp_path, workers, monkeypatch):
    """Under load the edge stops answering rather than answering 503, and that
    was the one form of pushback the limiter would never have heard about."""
    workers(16)
    monkeypatch.setattr(m3u8.time, "sleep", lambda s: None)

    def refuse(self, url, *a, **k):
        raise requests.ConnectionError("connection reset")

    monkeypatch.setattr(requests.Session, "get", refuse)

    seg = _segments(tmp_path, count=1)
    seg.get_req_ts("https://cdn.example.test/a/seg-0.ts")

    assert seg._budget.allowance < 16


def test_in_flight_never_outruns_a_shrunk_allowance(tmp_path, workers, monkeypatch):
    """The point of the whole thing: once the source pushes back, fewer
    requests are actually on the wire."""
    workers(8)
    peak_after_cut = 0
    in_flight = 0
    guard = threading.Lock()

    def fake_get(self, url, *args, **kwargs):
        nonlocal in_flight, peak_after_cut
        with guard:
            in_flight += 1
            if m3u8._segment_budget.allowance < 8:
                peak_after_cut = max(peak_after_cut, in_flight)
        try:
            threading.Event().wait(0.005)
            # Every segment is refused, so the allowance walks down to the floor.
            return _Response(503)
        finally:
            with guard:
                in_flight -= 1

    monkeypatch.setattr(m3u8.time, "sleep", lambda s: None)
    monkeypatch.setattr(requests.Session, "get", fake_get)

    seg = _segments(tmp_path, count=40)
    with pytest.raises(RuntimeError):
        # Nothing was served, so the completeness check is right to object.
        seg.download_ts()

    assert 0 < peak_after_cut < 8


# ── Retry-After ───────────────────────────────────────────────────────────────

def test_the_source_names_its_own_wait(tmp_path, workers, monkeypatch):
    """Guessing at a backoff is only for a source that did not say."""
    workers(4)
    slept = []
    monkeypatch.setattr(m3u8.time, "sleep", slept.append)

    replies = [_Response(503, headers={"Retry-After": "7"}), _Response(200)]
    monkeypatch.setattr(requests.Session, "get",
                        lambda self, url, *a, **k: replies.pop(0))

    _segments(tmp_path).get_req_ts("https://cdn.example.test/a/seg-0.ts")

    assert slept == [7.0]


def test_an_outlandish_wait_is_capped(tmp_path, workers, monkeypatch):
    """An hour is not a retry, it is a stalled worker. The sequential pass is
    the better place to arrive late."""
    workers(4)
    slept = []
    monkeypatch.setattr(m3u8.time, "sleep", slept.append)

    replies = [_Response(503, headers={"Retry-After": "3600"}), _Response(200)]
    monkeypatch.setattr(requests.Session, "get",
                        lambda self, url, *a, **k: replies.pop(0))

    _segments(tmp_path).get_req_ts("https://cdn.example.test/a/seg-0.ts")

    assert slept == [m3u8.MAX_RETRY_AFTER]


def test_an_http_date_falls_back_to_the_guess(tmp_path, workers, monkeypatch):
    """The date form needs a clock both ends agree on, so it is not read."""
    workers(4)
    slept = []
    monkeypatch.setattr(m3u8.time, "sleep", slept.append)

    replies = [_Response(503, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
               _Response(200)]
    monkeypatch.setattr(requests.Session, "get",
                        lambda self, url, *a, **k: replies.pop(0))

    _segments(tmp_path).get_req_ts("https://cdn.example.test/a/seg-0.ts")

    assert len(slept) == 1
    assert 0 < slept[0] <= m3u8.MAX_SEGMENT_BACKOFF * 1.5


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
