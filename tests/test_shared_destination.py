"""One title, several requests, one file.

Requests for the same title in different languages are kept apart deliberately —
the content key includes the chosen tracks — but the library layout carries no
language, so they all resolve to the same path. Two things follow from that, and
both were broken: two downloads must not write the file at once, and the file
left behind has to hold what everybody asked for rather than what the last one
to finish happened to want.
"""

import threading
import time

import pytest

from app.auth.permissions import Permission
from app.core import m3u8
from app.requests import models as request_models, resolver
from tests.conftest import do_setup
from tests.test_requests import EPISODE_BODY, FILM_BODY, _create, _login, _user


# ── Only one writer per destination ────────────────────────────────────────────

@pytest.fixture
def concurrent(monkeypatch):
    """Record when each download holds the file, so overlaps are visible."""
    events: list[tuple[str, str]] = []
    guard = threading.Lock()

    def fake_download(**kwargs):
        name = kwargs["output_filename"]
        with guard:
            events.append(("enter", name))
        time.sleep(0.05)
        with guard:
            events.append(("exit", name))
        return name

    monkeypatch.setattr(m3u8, "_download_m3u8", fake_download)
    m3u8._destination_locks.clear()
    return events


def _run(paths):
    threads = [
        threading.Thread(target=m3u8.download_m3u8, kwargs={"output_filename": p})
        for p in paths
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_two_downloads_of_one_file_never_overlap(concurrent):
    """Confirmed in the field: approving the same title twice at once left a
    corrupt film that still looked like a film."""
    _run(["/library/Film (2020)/Film (2020).mp4"] * 2)

    depth = 0
    for kind, _ in concurrent:
        depth += 1 if kind == "enter" else -1
        assert depth <= 1, concurrent
    assert len(concurrent) == 4


def test_the_same_file_spelled_differently_is_still_the_same_file(concurrent):
    _run([
        "/library/Film (2020)/Film (2020).mp4",
        "/library/Film (2020)/../Film (2020)/Film (2020).mp4",
    ])

    depth = 0
    for kind, _ in concurrent:
        depth += 1 if kind == "enter" else -1
        assert depth <= 1, concurrent


def test_different_files_still_download_together(concurrent):
    """The lock is per destination: serialising everything would turn the
    download pool into a queue of one."""
    _run(["/library/A/A.mp4", "/library/B/B.mp4"])

    depths = []
    depth = 0
    for kind, _ in concurrent:
        depth += 1 if kind == "enter" else -1
        depths.append(depth)
    assert max(depths) == 2


# ── The file holds what everyone asked for ─────────────────────────────────────

def _request(**fields):
    base = dict(
        id=1, content_key="k", source="streamingcommunity", media_type="film",
        external_id="123", slug=None, title="Film", year="2020", poster=None,
        season=None, episode_number=None, anime_type=None,
        audio_languages=["ita"], subtitle_languages=[], available_snapshot=None,
        status=request_models.PENDING, problem=None, denial_reason=None,
        requested_by=1, decided_by=None, decided_at=None, job_id=None,
        output_path=None, created_at="", updated_at="",
    )
    base.update(fields)
    return request_models.Request(**base)


def test_the_union_covers_every_live_request(client, admin_credentials, source, stub_jobs):
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    carol = _user(client, "carol", Permission.REQUEST)

    _create(client, _login(client, bob), {**FILM_BODY, "audio_languages": ["ita"]})
    _create(client, _login(client, carol),
            {**FILM_BODY, "audio_languages": ["eng"], "subtitle_languages": ["ita"]})

    first = request_models.list_all()[-1]
    assert request_models.wanted_languages(first) == (["eng", "ita"], ["ita"])


def test_a_denied_request_stops_counting(client, admin_credentials, source, stub_jobs):
    """Nobody is waiting on a language that was refused."""
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    carol = _user(client, "carol", Permission.REQUEST)
    _create(client, _login(client, bob),
            {**FILM_BODY, "audio_languages": ["ita"], "subtitle_languages": []})
    _create(client, _login(client, carol),
            {**FILM_BODY, "audio_languages": ["eng"], "subtitle_languages": ["fra"]})

    english = [r for r in request_models.list_all() if r.audio_languages == ["eng"]][0]
    request_models.transition(english.id, request_models.DENIED, denial_reason="no")

    italian = [r for r in request_models.list_all() if r.audio_languages == ["ita"]][0]
    assert request_models.wanted_languages(italian) == (["ita"], [])


def test_another_episode_of_the_same_series_is_not_counted(
    client, admin_credentials, source, stub_jobs
):
    """Identity is the content the destination path is built from, episode
    included: two episodes of one series are two files."""
    do_setup(client, admin_credentials)
    bob = _user(client, "bob", Permission.REQUEST)
    csrf = _login(client, bob)
    _create(client, csrf, {**EPISODE_BODY, "episode_number": "1",
                           "audio_languages": ["ita"], "subtitle_languages": []})
    _create(client, csrf, {**EPISODE_BODY, "episode_number": "2",
                           "audio_languages": ["eng"], "subtitle_languages": []})

    first = [r for r in request_models.list_all() if r.episode_number == "1"][0]

    assert request_models.wanted_languages(first) == (["ita"], [])


# ── Widening, and the language that has since gone ────────────────────────────

def test_the_download_is_widened_to_everyones_languages(monkeypatch):
    common = {"audio_languages": ["ita"], "subtitle_languages": []}
    monkeypatch.setattr(request_models, "wanted_languages", lambda r: (["eng", "ita"], []))

    resolver._widen_to_everyone(common, _request(audio_languages=["ita"]),
                                {"audio": ["ita", "eng"], "subtitles": []})

    assert common["audio_languages"] == ["eng", "ita"]


def test_someone_elses_language_that_has_gone_does_not_fail_the_download(monkeypatch):
    """strict_audio must still fail for the caller's own missing language, but a
    language somebody else chose months ago must not fail a download nobody
    asked to be strict about."""
    common = {"audio_languages": ["ita"], "subtitle_languages": []}
    monkeypatch.setattr(request_models, "wanted_languages", lambda r: (["fra", "ita"], []))

    resolver._widen_to_everyone(common, _request(audio_languages=["ita"]),
                                {"audio": ["ita"], "subtitles": []})

    assert common["audio_languages"] == ["ita"]


def test_the_callers_own_language_is_never_filtered_away(monkeypatch):
    """It has to reach the downloader for strict_audio to report it missing;
    dropping it here would turn a loud failure into a silent substitution."""
    common = {"audio_languages": ["fra"], "subtitle_languages": []}
    monkeypatch.setattr(request_models, "wanted_languages", lambda r: (["fra"], []))

    resolver._widen_to_everyone(common, _request(audio_languages=["fra"]),
                                {"audio": ["ita"], "subtitles": []})

    assert common["audio_languages"] == ["fra"]


def test_a_track_already_in_the_file_is_not_dropped(monkeypatch, tmp_path):
    """A re-download replaces the file, so a language present but no longer
    named by any live request — the request that asked for it was denied
    afterwards — would vanish without anyone being told."""
    from app.core import probe

    path = tmp_path / "Film (2020)" / "Film (2020).mkv"
    path.parent.mkdir(parents=True)
    path.write_text("x")
    monkeypatch.setattr(resolver, "library_dir", lambda media_type: str(tmp_path))
    monkeypatch.setattr(probe, "media_languages",
                        lambda p: {"audio": {"ita", "fra"}, "subtitles": set()})
    monkeypatch.setattr(request_models, "wanted_languages", lambda r: (["eng", "ita"], []))

    common = {"audio_languages": ["eng"], "subtitle_languages": []}
    resolver._widen_to_everyone(common, _request(audio_languages=["eng"]),
                                {"audio": ["ita", "eng", "fra"], "subtitles": []})

    assert common["audio_languages"] == ["eng", "fra", "ita"]


def test_an_untagged_track_is_not_asked_for_by_name(monkeypatch, tmp_path):
    """"und" names no language; asking the source for it would find nothing."""
    from app.core import probe

    path = tmp_path / "Film (2020)" / "Film (2020).mp4"
    path.parent.mkdir(parents=True)
    path.write_text("x")
    monkeypatch.setattr(resolver, "library_dir", lambda media_type: str(tmp_path))
    monkeypatch.setattr(probe, "media_languages",
                        lambda p: {"audio": {"und"}, "subtitles": set()})
    monkeypatch.setattr(request_models, "wanted_languages", lambda r: (["eng"], []))

    common = {"audio_languages": ["eng"], "subtitle_languages": []}
    resolver._widen_to_everyone(common, _request(audio_languages=["eng"]),
                                {"audio": ["eng"], "subtitles": []})

    assert common["audio_languages"] == ["eng"]
