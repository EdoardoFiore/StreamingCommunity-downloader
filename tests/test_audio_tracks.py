"""Which audio track a download ends up with, and when that is a refusal.

Never substituting a requested language is a rule of this project: a file that
plays in the wrong language looks correct and tells nobody. But "the language
you asked for is not among the ones offered" and "this source offers no separate
audio at all" are different situations, and only the first is a substitution.

A source that muxes its single soundtrack into the video rendition advertises no
audio renditions. Refusing there rejected titles at approval that the create
path had already accepted — router.py skips its own audio check when the source
offers nothing, so the two ends disagreed about the same film.
"""

import pytest
import requests

from app.core import film
from app.core._shared import MissingAudioTrackError


MUXED = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\n"
    "https://cdn.example.test/1080/playlist.m3u8\n"
)

WITH_AUDIO = (
    "#EXTM3U\n"
    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="English",LANGUAGE="en",'
    'DEFAULT=YES,URI="https://cdn.example.test/audio-en.m3u8"\n'
    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="Italiano",LANGUAGE="it",'
    'URI="https://cdn.example.test/audio-it.m3u8"\n'
    '#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,AUDIO="a"\n'
    "https://cdn.example.test/1080/playlist.m3u8\n"
)

MASTER = "https://cdn.example.test/master.m3u8"


class _Response:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text
        self.ok = 200 <= status_code < 300


@pytest.fixture
def master(monkeypatch):
    """Serve one master playlist body for every request."""
    holder = {"body": MUXED, "status": 200}

    def fake_get(url, *args, **kwargs):
        return _Response(holder["status"], holder["body"])

    monkeypatch.setattr(requests, "get", fake_get)
    return holder


def _collect(languages, strict=True):
    return film._collect_audio_tracks(MASTER, "https://example.test/", languages,
                                      strict=strict)


# ── A source with no separate audio renditions ─────────────────────────────────

def test_a_muxed_source_is_accepted_under_strict(master):
    """The reported failure: «Traccia audio 'ita' non disponibile (presenti:
    nessuna)» on approval, for a film whose only soundtrack is the muxed one."""
    assert _collect(["ita"]) == []


def test_a_muxed_source_is_accepted_whatever_was_asked_for(master):
    """Nothing was offered, so nothing was chosen, so nothing can be wrong."""
    assert _collect(["eng", "fra"]) == []


# ── A source that does offer renditions ────────────────────────────────────────

def test_a_missing_language_still_refuses(master):
    """The rule this guard exists for is untouched: renditions are on offer and
    the requested one is not among them, which is a substitution."""
    master["body"] = WITH_AUDIO

    with pytest.raises(MissingAudioTrackError) as excinfo:
        _collect(["fra"])

    assert excinfo.value.language == "fra"
    assert sorted(excinfo.value.available) == ["en", "it"]


def test_an_offered_language_is_resolved(master):
    master["body"] = WITH_AUDIO

    tracks = _collect(["ita"])

    assert tracks == [{"url": "https://cdn.example.test/audio-it.m3u8", "language": "ita"}]


def test_without_strict_a_missing_language_is_skipped(master):
    """The direct-download path picks up whatever is there rather than failing."""
    master["body"] = WITH_AUDIO

    assert _collect(["fra"], strict=False) == []


def test_an_unreachable_master_still_fails_under_strict(master):
    """Distinguishing "offers nothing" from "could not be read" matters: the
    second must not be mistaken for a muxed source and waved through."""
    master["status"] = 500

    with pytest.raises(RuntimeError, match="Master M3U8 non raggiungibile"):
        _collect(["ita"])
