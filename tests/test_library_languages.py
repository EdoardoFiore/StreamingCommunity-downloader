"""What sits in the library, and whether it is what was asked for.

The request key includes the chosen tracks on purpose — two people asking for
one film in different audio are making two requests, and merging them would give
one of them the wrong file. The destination path carries no language, though, so
both land on the same file. A library check that only asked whether the path
existed therefore told the second person their English audio was ready when what
sat there was Italian, and said so on their bell.

ffprobe is faked throughout: these tests are about the decision, and must not
depend on which binaries the machine running them happens to have.
"""

import json
import subprocess

import pytest

from app.core import probe


def _stream(codec_type, language=None):
    stream = {"index": 0, "codec_type": codec_type}
    if language is not None:
        stream["tags"] = {"language": language}
    return stream


@pytest.fixture
def ffprobe(monkeypatch, tmp_path):
    """Answer ffprobe with a chosen stream list, and pretend it exists."""
    state = {"streams": [], "returncode": 0, "stdout": None}

    def fake_run(cmd, *args, **kwargs):
        payload = state["stdout"]
        if payload is None:
            payload = json.dumps({"streams": state["streams"]}).encode()
        return subprocess.CompletedProcess(cmd, state["returncode"], payload, b"")

    monkeypatch.setattr(probe, "get_ffprobe_exe", lambda: "/usr/bin/ffprobe")
    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    return state


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "Film (2020)" / "Film (2020).mkv"
    path.parent.mkdir(parents=True)
    path.write_text("not really a film")
    return str(path)


# ── Reading a file ─────────────────────────────────────────────────────────────

def test_the_languages_of_a_file_are_read_from_its_streams(ffprobe, video):
    ffprobe["streams"] = [
        _stream("video"), _stream("audio", "ita"), _stream("audio", "eng"),
        _stream("subtitle", "ita"),
    ]

    found = probe.media_languages(video)

    assert found == {"audio": {"ita", "eng"}, "subtitles": {"ita"}}


def test_iso_639_1_tags_are_folded_into_one_spelling(ffprobe, video):
    """ffprobe, the source and the sidecar filenames disagree about how to spell
    a language; the request system speaks only one of the three."""
    ffprobe["streams"] = [_stream("audio", "it"), _stream("subtitle", "en")]

    found = probe.media_languages(video)

    assert found == {"audio": {"ita"}, "subtitles": {"eng"}}


def test_a_forced_subtitle_counts_as_its_language(ffprobe, video):
    ffprobe["streams"] = [_stream("audio", "ita"), _stream("subtitle", "forced-ita")]

    assert probe.media_languages(video)["subtitles"] == {"ita"}


def test_a_sidecar_vtt_counts_as_present(ffprobe, video):
    """Jellyfin's convention, and what this downloader writes: a subtitle can be
    beside the file rather than inside it."""
    import os

    ffprobe["streams"] = [_stream("audio", "ita")]
    stem = os.path.splitext(video)[0]
    open(f"{stem}.en.vtt", "w").close()

    assert probe.media_languages(video)["subtitles"] == {"eng"}


def test_an_unrelated_vtt_is_not_counted(ffprobe, video):
    import os

    ffprobe["streams"] = [_stream("audio", "ita")]
    open(os.path.join(os.path.dirname(video), "Altro Film.en.vtt"), "w").close()

    assert probe.media_languages(video)["subtitles"] == set()


# ── Not being able to look is not the same as finding nothing ─────────────────

def test_without_ffprobe_the_answer_is_unknown(monkeypatch, video):
    monkeypatch.setattr(probe, "get_ffprobe_exe", lambda: None)

    assert probe.media_languages(video) is None


def test_a_failing_ffprobe_is_unknown_too(ffprobe, video):
    ffprobe["returncode"] = 1

    assert probe.media_languages(video) is None


def test_unparseable_output_is_unknown(ffprobe, video):
    ffprobe["stdout"] = b"not json at all"

    assert probe.media_languages(video) is None


def test_a_missing_file_is_unknown(ffprobe, tmp_path):
    assert probe.media_languages(str(tmp_path / "nope.mkv")) is None


# ── The gap ────────────────────────────────────────────────────────────────────

def test_a_language_that_is_there_is_not_missing(ffprobe, video):
    ffprobe["streams"] = [_stream("audio", "ita"), _stream("subtitle", "ita")]

    gap = probe.missing_languages(video, ["ita"], ["ita"])

    assert gap == {"audio": [], "subtitles": []}


def test_a_language_that_is_not_there_is_missing(ffprobe, video):
    ffprobe["streams"] = [_stream("audio", "ita")]

    gap = probe.missing_languages(video, ["eng"], ["fra"])

    assert gap == {"audio": ["eng"], "subtitles": ["fra"]}


def test_only_the_missing_half_is_reported(ffprobe, video):
    ffprobe["streams"] = [_stream("audio", "ita"), _stream("audio", "eng")]

    gap = probe.missing_languages(video, ["ita", "eng", "fra"], [])

    assert gap == {"audio": ["fra"], "subtitles": []}


def test_one_untagged_audio_track_satisfies_one_request(ffprobe, video):
    """A source with no separate audio renditions muxes its single soundtrack in
    and often labels it 'und'. Calling that a missing language would re-download
    the same file for ever."""
    ffprobe["streams"] = [_stream("video"), _stream("audio")]

    assert probe.missing_languages(video, ["ita"], []) == {"audio": [], "subtitles": []}


def test_an_untagged_track_does_not_satisfy_two_languages(ffprobe, video):
    """One unlabelled track cannot be both."""
    ffprobe["streams"] = [_stream("audio")]

    gap = probe.missing_languages(video, ["ita", "eng"], [])

    assert gap["audio"] == ["eng", "ita"]


# ── What the request system does with it ───────────────────────────────────────

def _request(**fields):
    from app.requests import models

    base = dict(
        id=1, content_key="k", source="streamingcommunity", media_type="film",
        external_id="123", slug=None, title="Film", year="2020", poster=None,
        season=None, episode_number=None, anime_type=None,
        audio_languages=["ita"], subtitle_languages=[], available_snapshot=None,
        status=models.PENDING, problem=None, denial_reason=None, requested_by=1,
        decided_by=None, decided_at=None, job_id=None, output_path=None,
        created_at="", updated_at="",
    )
    base.update(fields)
    return models.Request(**base)


@pytest.fixture
def library(monkeypatch, tmp_path):
    from app.requests import resolver

    monkeypatch.setattr(resolver, "library_dir", lambda media_type: str(tmp_path))
    return tmp_path


def test_the_same_language_is_already_in_the_library(ffprobe, library):
    from app.requests import resolver

    ffprobe["streams"] = [_stream("audio", "ita")]
    path = library / "Film (2020)" / "Film (2020).mp4"
    path.parent.mkdir(parents=True)
    path.write_text("x")

    assert resolver.is_in_library(_request(audio_languages=["ita"])) is True


def test_a_different_language_is_not(ffprobe, library):
    """The reported case: the second request used to be marked available on the
    strength of the first one's file."""
    from app.requests import resolver

    ffprobe["streams"] = [_stream("audio", "ita")]
    path = library / "Film (2020)" / "Film (2020).mp4"
    path.parent.mkdir(parents=True)
    path.write_text("x")

    request = _request(audio_languages=["eng"])
    assert resolver.is_in_library(request) is False
    assert resolver.library_gap(request) == {"audio": ["eng"], "subtitles": []}
    # And the file is still there to be added to, rather than replaced blind.
    assert resolver.existing_file(request) == str(path)


def test_a_missing_subtitle_alone_is_enough_to_not_be_done(ffprobe, library):
    from app.requests import resolver

    ffprobe["streams"] = [_stream("audio", "ita")]
    path = library / "Film (2020)" / "Film (2020).mkv"
    path.parent.mkdir(parents=True)
    path.write_text("x")

    assert resolver.is_in_library(
        _request(audio_languages=["ita"], subtitle_languages=["eng"])
    ) is False


def test_nothing_in_the_library_is_not_a_gap(ffprobe, library):
    from app.requests import resolver

    request = _request()
    assert resolver.library_gap(request) is None
    assert resolver.is_in_library(request) is False
    assert resolver.existing_file(request) is None


def test_a_file_that_cannot_be_inspected_counts_as_done(monkeypatch, library):
    """ffprobe is optional — the bundled ffmpeg brings none. Calling every track
    missing would re-download the file on every request instead."""
    from app.requests import resolver

    monkeypatch.setattr(probe, "get_ffprobe_exe", lambda: None)
    path = library / "Film (2020)" / "Film (2020).mp4"
    path.parent.mkdir(parents=True)
    path.write_text("x")

    assert resolver.is_in_library(_request(audio_languages=["eng"])) is True
