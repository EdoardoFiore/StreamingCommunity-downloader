"""Segments: retry what is worth retrying, and never join an incomplete set.

Issue #10. Two failures that compounded each other: ``get_req_ts`` looked like
it retried three times but returned on the first attempt for everything except a
429, and ``join`` logged the resulting gaps and concatenated anyway. A film of
1816 segments finished as "done" with 984 of them saved.

The third, unreported: every attempt advanced the progress bar whether or not a
segment arrived, so the download read 100% and the stall watchdog — which
watches that same counter — could never fire.
"""

import os

import pytest
import requests

from app.core.m3u8 import M3U8_Segments


class _Response:
    def __init__(self, status_code=200, content=b"x" * 10):
        self.status_code = status_code
        self.content = content
        self.ok = 200 <= status_code < 300


class _Bar:
    """The progress bar contract M3U8_Segments actually uses."""

    def __init__(self, total=0, **kwargs):
        self.total = total
        self.n = 0
        self.bytes = 0

    def update(self, n=1, bytes=0):
        self.n += n
        self.bytes += bytes

    def close(self):
        pass

    def refresh(self):
        pass


@pytest.fixture
def no_sleep(monkeypatch):
    """Backoff must not make the suite wait for real."""
    slept = []
    monkeypatch.setattr("app.core.m3u8.time.sleep", lambda s: slept.append(s))
    return slept


@pytest.fixture
def segment_server(monkeypatch):
    """Answer every segment request from a queue, and count the calls."""
    calls: list[str] = []
    replies: list = []

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        if replies:
            reply = replies.pop(0)
        else:
            reply = _Response(200)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(requests, "get", fake_get)
    return replies, calls


def _segments(tmp_path, count=3, **kwargs):
    seg = M3U8_Segments("https://cdn.example.test/playlist.m3u8",
                        temp_dir=str(tmp_path / "segs"),
                        progress_factory=lambda **kw: _Bar(**kw), **kwargs)
    seg.segments = [f"https://cdn.example.test/seg-{i}.ts" for i in range(count)]
    return seg


# ── get_req_ts: the retry that was not one ─────────────────────────────────────

def test_a_503_is_retried_and_can_succeed(tmp_path, segment_server, no_sleep):
    """The reported bug: a 503 returned immediately, losing a segment the CDN
    would have served a moment later."""
    replies, calls = segment_server
    replies.extend([_Response(503), _Response(503), _Response(200, b"ok")])

    content = _segments(tmp_path).get_req_ts("https://cdn.example.test/seg-0.ts")

    assert content == b"ok"
    assert len(calls) == 3


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429, 408])
def test_every_transient_status_gets_the_full_three_attempts(
    tmp_path, segment_server, no_sleep, status
):
    replies, calls = segment_server
    replies.extend([_Response(status)] * 3)

    assert _segments(tmp_path).get_req_ts("https://cdn.example.test/seg-0.ts") is None
    assert len(calls) == 3
    # Three attempts, two waits: nothing sleeps after the last one.
    assert len(no_sleep) == 2


@pytest.mark.parametrize("status", [403, 404, 410])
def test_a_verdict_is_not_retried(tmp_path, segment_server, no_sleep, status):
    """A 4xx that is not 429 does not improve on the second ask, and repeating
    it across a few thousand segments only delays the same error."""
    replies, calls = segment_server
    replies.extend([_Response(status)] * 3)

    assert _segments(tmp_path).get_req_ts("https://cdn.example.test/seg-0.ts") is None
    assert len(calls) == 1
    assert no_sleep == []


def test_a_connection_error_is_still_retried(tmp_path, segment_server, no_sleep):
    """This half already worked, and the asymmetry was the tell: the same outage
    was retried when the server did not answer and given up on when it answered
    badly."""
    replies, calls = segment_server
    replies.extend([requests.ConnectionError("reset"), _Response(200, b"ok")])

    assert _segments(tmp_path).get_req_ts("https://cdn.example.test/seg-0.ts") == b"ok"
    assert len(calls) == 2


def test_the_backoff_grows_and_is_jittered(tmp_path, segment_server, no_sleep):
    replies, _ = segment_server
    replies.extend([_Response(503)] * 3)

    _segments(tmp_path).get_req_ts("https://cdn.example.test/seg-0.ts")

    assert len(no_sleep) == 2
    assert no_sleep[1] > no_sleep[0]
    # Jittered rather than exact powers of two: sixteen workers backing off in
    # lockstep return to a struggling CDN together.
    assert all(s != int(s) for s in no_sleep)


# ── Progress counts segments, not attempts ─────────────────────────────────────

def test_a_failed_segment_does_not_advance_the_bar(tmp_path, segment_server, no_sleep):
    replies, _ = segment_server
    replies.extend([_Response(403)])
    seg = _segments(tmp_path, count=1)
    bar = _Bar(total=1)

    seg.save_ts(0, bar, None)

    assert bar.n == 0
    assert seg._failed_segments == {0}


def test_a_saved_segment_advances_the_bar(tmp_path, segment_server, no_sleep):
    seg = _segments(tmp_path, count=1)
    bar = _Bar(total=1)

    seg.save_ts(0, bar, None)

    assert bar.n == 1
    assert seg._failed_segments == set()


def test_empty_content_counts_as_a_failure(tmp_path, segment_server, no_sleep):
    """An empty body would otherwise write a zero-byte file, which then reads as
    a segment that is present."""
    replies, _ = segment_server
    replies.append(_Response(200, b""))
    seg = _segments(tmp_path, count=1)

    seg.save_ts(0, _Bar(total=1), None)

    assert seg._failed_segments == {0}
    assert not os.path.exists(os.path.join(seg.temp_folder, "0.ts"))


# ── The completeness gate ──────────────────────────────────────────────────────

def test_a_download_missing_segments_fails_instead_of_finishing(
    tmp_path, segment_server, no_sleep
):
    replies, _ = segment_server
    # Every attempt refused, first pass and sequential retry alike.
    replies.extend([_Response(403)] * 20)
    seg = _segments(tmp_path, count=3)

    with pytest.raises(RuntimeError, match="Download incompleto: 3 segmenti su 3"):
        seg.download_ts()


def test_a_download_that_recovers_on_the_second_pass_succeeds(
    tmp_path, segment_server, no_sleep
):
    """The sequential retry is the point of the second pass: it arrives later
    and one request at a time."""
    replies, _ = segment_server
    replies.extend([_Response(503), _Response(503), _Response(503)])  # segment 0, all attempts

    seg = _segments(tmp_path, count=1)
    seg.download_ts()

    assert seg._failed_segments == set()
    assert os.path.exists(os.path.join(seg.temp_folder, "0.ts"))


def test_join_refuses_to_write_an_incomplete_file(tmp_path, segment_server, no_sleep):
    """The corruption itself: join warned about the gaps and concatenated
    anyway, so the job ended "done" holding a truncated film."""
    seg = _segments(tmp_path, count=3)
    for index in (0, 2):
        with open(os.path.join(seg.temp_folder, f"{index}.ts"), "wb") as fh:
            fh.write(b"data")

    with pytest.raises(RuntimeError, match="Download incompleto: 1 segmenti su 3"):
        seg.join(str(tmp_path / "out.mp4"))

    assert not os.path.exists(tmp_path / "out.mp4")


def test_the_gate_reads_the_disk_not_the_bookkeeping(tmp_path, segment_server, no_sleep):
    """A run cut short never attempts the remaining segments, so _failed_segments
    is empty exactly when most of the download is absent."""
    seg = _segments(tmp_path, count=5)
    with open(os.path.join(seg.temp_folder, "0.ts"), "wb") as fh:
        fh.write(b"data")
    seg._failed_segments = set()

    assert seg._missing_segments() == [1, 2, 3, 4]


def test_a_stale_combined_file_does_not_break_the_join(tmp_path, segment_server, no_sleep):
    """_combined.ts has no digits in its name, so it used to raise ValueError out
    of the sort key. Reachable now that a failed join leaves the folder behind."""
    seg = _segments(tmp_path, count=1)
    with open(os.path.join(seg.temp_folder, "_combined.ts"), "wb") as fh:
        fh.write(b"stale")

    # Segment 0 is missing, so this must be the completeness error and not a
    # ValueError from listing the folder.
    with pytest.raises(RuntimeError, match="Download incompleto"):
        seg.join(str(tmp_path / "out.mp4"))


# ── Giving up on a source that is not serving ──────────────────────────────────

def test_a_dead_source_gives_up_early(tmp_path, segment_server, no_sleep):
    """Retrying properly makes a dead source much slower to fail than it used to
    be, so there has to be a point where the download stops rather than grinding
    through thousands of segments to reach the same conclusion."""
    replies, calls = segment_server
    replies.extend([_Response(503)] * 20000)
    seg = _segments(tmp_path, count=1000)

    with pytest.raises(RuntimeError, match="Download interrotto"):
        seg.download_ts()

    assert seg._abort.is_set()
    assert len(calls) < 1000 * seg.max_retry


def test_a_source_that_only_throttles_is_not_written_off(tmp_path, monkeypatch, no_sleep):
    """The regression that mattered, at the scale it was reported: 1525 segments
    of which 84 were refused on the first pass.

    The first breaker aborted on a count alone — 5% of the playlist — so it
    tripped before ever reaching the sequential pass that recovers those 84. A
    download that used to complete stopped completing. The second shape counted
    a cumulative failure ratio, which fails on this same case whenever the
    refusals arrive before any success has been recorded, as they do here.
    """
    refused = set(range(84))
    attempts: dict[str, int] = {}

    def fake_get(url, *args, **kwargs):
        attempts[url] = attempts.get(url, 0) + 1
        index = int(url.rsplit("seg-", 1)[1].removesuffix(".ts"))
        if index in refused and attempts[url] <= 3:
            return _Response(503)
        return _Response(200)

    monkeypatch.setattr(requests, "get", fake_get)
    seg = _segments(tmp_path, count=1525)

    seg.download_ts()

    assert not seg._abort.is_set()
    assert seg._missing_segments() == []
    # Each refused segment really was asked three times before being written
    # off, and once more on the sequential pass — where it succeeded.
    assert attempts["https://cdn.example.test/seg-0.ts"] == 4


def test_a_success_breaks_the_failure_run(tmp_path, segment_server, no_sleep):
    """The counter is a run, not a total: anything arriving means the source is
    serving, so the count of what it refused stops being evidence of death."""
    seg = _segments(tmp_path, count=2)
    seg._failure_streak = 7

    seg._record_success()

    assert seg._failure_streak == 0


def test_a_short_playlist_still_reports_incompleteness(tmp_path, segment_server, no_sleep):
    """Below the minimum the breaker never trips, and the completeness gate is
    what catches it — the two must not leave a gap between them."""
    replies, _ = segment_server
    replies.extend([_Response(503)] * 500)
    seg = _segments(tmp_path, count=4)

    with pytest.raises(RuntimeError, match="Download incompleto"):
        seg.download_ts()

    assert not seg._abort.is_set()


def test_a_stalled_source_fails_instead_of_joining_what_arrived(
    tmp_path, segment_server, no_sleep, monkeypatch
):
    """The watchdog set its flag, broke out of the loop, and fell straight
    through to the join — so a stall produced a truncated file too."""
    def fire_at_once(self, progress_counter, quit_event, timeout_checker):
        timeout_checker()

    monkeypatch.setattr(M3U8_Segments, "timer", fire_at_once)
    seg = _segments(tmp_path, count=3)

    with pytest.raises(RuntimeError, match="nessun segmento scaricato per"):
        seg.download_ts()


# ── The happy path is untouched ────────────────────────────────────────────────

def test_a_clean_download_writes_every_segment(tmp_path, segment_server, no_sleep):
    seg = _segments(tmp_path, count=5)

    seg.download_ts()

    assert seg._missing_segments() == []
    assert sorted(os.listdir(seg.temp_folder)) == [f"{i}.ts" for i in range(5)]
