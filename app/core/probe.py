"""What a file already in the library actually contains.

The request system keys a request on its chosen tracks, so two people asking for
one film in different languages make two requests — deliberately, because
merging them would hand one of them the wrong file. The destination path carries
no language, though, so both land on the same file, and a library check that
only asks "does this path exist?" told the second person their English audio was
ready when what sat there was Italian.

Answering honestly means reading the file rather than its name.
"""

import json
import logging
import os
import subprocess

from app.core.ffmpeg_path import get_ffprobe_exe
from app.core.format import LANG_MAP, _normalize_lang

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 30

# ISO 639-1 back to the 639-2 codes the request system speaks. Built from the
# existing map so the two can never disagree about a language.
_TO_ISO2 = {short: long for long, short in LANG_MAP.items()}


def canonical_lang(raw: str | None) -> str:
    """One spelling for a language, whichever end it arrives from.

    Sources hand out ISO 639-2/B ("ita"), ffprobe usually gives 639-2/T, sidecar
    subtitle filenames use 639-1 ("it"), and any of them may be prefixed
    "forced-".
    """
    if not raw:
        return "und"
    code, _ = _normalize_lang(str(raw).strip().lower())
    return _TO_ISO2.get(code, code)


def _sidecar_subtitles(video_path: str) -> set[str]:
    """Subtitles stored beside the video as ``{stem}.{lang}.vtt``.

    Jellyfin's convention and the one this downloader writes, so a subtitle can
    be present without being inside the container at all.
    """
    directory = os.path.dirname(video_path) or "."
    stem = os.path.splitext(os.path.basename(video_path))[0]
    found = set()
    try:
        names = os.listdir(directory)
    except OSError:
        return found
    for name in names:
        if not name.startswith(stem + ".") or not name.endswith(".vtt"):
            continue
        middle = name[len(stem) + 1:-len(".vtt")]
        if middle:
            found.add(canonical_lang(middle))
    return found


def media_languages(path: str) -> dict | None:
    """The audio and subtitle languages of ``path``.

    Returns ``{"audio": {...}, "subtitles": {...}}``, or **None** when the file
    cannot be inspected. None means "unknown", which is not the same as "has
    nothing": a caller must not conclude a track is missing from a failure to
    look, or it would re-download the same file for ever.
    """
    ffprobe = get_ffprobe_exe()
    if ffprobe is None:
        logger.info("No ffprobe available; cannot tell which tracks %s holds", path)
        return None
    if not os.path.exists(path):
        return None

    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json",
             "-show_entries", "stream=index,codec_type:stream_tags=language", path],
            capture_output=True, timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("ffprobe failed on %s: %s", path, exc)
        return None

    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace") if proc.stderr else "(no stderr)"
        logger.warning("ffprobe returned %d on %s: %s", proc.returncode, path, stderr[-200:])
        return None

    try:
        streams = json.loads(proc.stdout or b"{}").get("streams", [])
    except (ValueError, TypeError) as exc:
        logger.warning("Could not parse ffprobe output for %s: %s", path, exc)
        return None

    audio, subtitles = set(), set()
    for stream in streams:
        language = canonical_lang((stream.get("tags") or {}).get("language"))
        if stream.get("codec_type") == "audio":
            audio.add(language)
        elif stream.get("codec_type") == "subtitle":
            subtitles.add(language)

    return {"audio": audio, "subtitles": subtitles | _sidecar_subtitles(path)}


def missing_languages(path: str, audio: list[str], subtitles: list[str]) -> dict | None:
    """Which of the requested languages ``path`` does not already carry.

    None when the file cannot be inspected — the caller decides what to do with
    not knowing, which is a different decision from finding nothing missing.

    A file holding exactly one audio track whose language is untagged counts as
    satisfying any single audio request: a source that offers no separate audio
    renditions muxes one soundtrack in and frequently labels it "und", and
    treating that as a missing language would re-download it endlessly.
    """
    present = media_languages(path)
    if present is None:
        return None

    wanted_audio = {canonical_lang(code) for code in audio or []}
    wanted_subs = {canonical_lang(code) for code in subtitles or []}

    have_audio = present["audio"]
    if len(wanted_audio) <= 1 and have_audio and have_audio <= {"und"}:
        wanted_audio = set()

    return {
        "audio": sorted(wanted_audio - have_audio),
        "subtitles": sorted(wanted_subs - present["subtitles"]),
    }
