"""Which container the downloader writes, and what FFmpeg needs to produce it.

The container used to be an accident rather than a choice. Every destination was
built as ``.mp4``, and the file turned into ``.mkv`` the moment there was a
second audio track or a subtitle to embed — so in practice almost everything
ended up Matroska, and the devices that cannot play it (older smart TVs,
Chromecast, consoles) had no way out. Issue #19.

A module of its own rather than part of ``app.core.format``, because
``format.py`` imports ``app.core.m3u8`` and ``m3u8.py`` imports
``app.core.paths``: putting the table there would make ``paths -> format`` a
cycle. Nothing here imports from ``app.core`` at all, and the one import of
``app.config`` is deferred, so this module stays safe to import from anywhere.

Nothing is ever re-encoded. What comes out of the source's HLS is H.264 video
and AAC audio, both native to either container, so a stream copy always works.
The single conversion is WebVTT to ``mov_text``, because MP4 has no WebVTT
track type — and that is a subtitle, not the picture.
"""

import logging
import mimetypes
import os

logger = logging.getLogger(__name__)

DEFAULT = "mkv"

CONTAINERS: dict[str, dict] = {
    "mkv": {
        "extension": ".mkv",
        "label": "MKV",
        "mime": "video/x-matroska",
        # Matroska holds WebVTT natively, so the downloaded sidecars go in as-is.
        "subtitle_codec": "copy",
        # No muxer-private options. mp4's movflags is not merely ignored here,
        # it is fatal: the matroska muxer does not declare the option at all,
        # and FFmpeg refuses an option its muxer has never heard of.
        "mux_options": {},
    },
    "mp4": {
        "extension": ".mp4",
        "label": "MP4",
        "mime": "video/mp4",
        # MP4 has no WebVTT track type; 3GPP timed text is what players read.
        "subtitle_codec": "mov_text",
        "mux_options": {"movflags": "+faststart"},
    },
}

# The extensions a finished download can carry. The single list the library
# check, the failure cleanup and the file manager all read, instead of the
# ".mkv" literals that used to be spread across four modules.
KNOWN_EXTENSIONS: tuple[str, ...] = tuple(
    spec["extension"] for spec in CONTAINERS.values()
)

EMBED = "embed"
EXTERNAL = "external"
SUBTITLE_MODES = (EMBED, EXTERNAL)
DEFAULT_SUBTITLE_MODE = EMBED

# Applied to every join regardless of container: the video is copied, the audio
# is normalised to AAC, and both are native to MKV and MP4 alike. Only the
# muxer flags merged in on top of this differ.
JOIN_COMMON: dict[str, str] = {
    "c:v": "copy",
    "c:a": "aac",
    "b:a": "192k",
    "af": "aresample=async=1000",
}


def _setting(key: str, allowed, default: str) -> str:
    """A stored settings string, or *default* when it is not one of *allowed*.

    Falls back rather than raising, for the same reason ``naming.templates()``
    does: this is read inside a running download, where a malformed setting
    must not be what stops it.
    """
    from app.config import get_settings

    value = get_settings().get(key)
    if isinstance(value, str) and value in allowed:
        return value
    if value not in (None, ""):
        logger.warning(
            "%s is %r, which is not one of %s; using %r",
            key, value, sorted(allowed), default,
        )
    return default


def configured() -> str:
    """The container an administrator chose in Settings."""
    return _setting("output_container", CONTAINERS, DEFAULT)


def subtitle_mode() -> str:
    """Whether subtitles are muxed in or left beside the video as ``.vtt``."""
    return _setting("subtitle_mode", SUBTITLE_MODES, DEFAULT_SUBTITLE_MODE)


def spec(name: str | None = None) -> dict:
    """The table entry for *name*, or for the configured container."""
    if name is None:
        name = configured()
    if name not in CONTAINERS:
        logger.warning("Unknown container %r; using %r", name, DEFAULT)
        name = DEFAULT
    return CONTAINERS[name]


def extension(name: str | None = None) -> str:
    """The file extension for *name*, or for the configured container."""
    return spec(name)["extension"]


def candidate_extensions(name: str | None = None) -> tuple[str, ...]:
    """Every extension a finished download could carry, likeliest first.

    The configured container leads, so the library check stats the file it
    would write today before the one an earlier setting would have written.
    """
    first = extension(name)
    return (first, *(e for e in KNOWN_EXTENSIONS if e != first))


def _spec_for_path(path: str) -> dict:
    """The table entry matching a path's extension, falling back to configured.

    Everything downstream of ``paths.py`` knows its container only from the
    name it was handed, so the extension is the carrier.
    """
    ext = os.path.splitext(path or "")[1].lower()
    for entry in CONTAINERS.values():
        if entry["extension"] == ext:
            return entry
    return spec()


def join_options(output_path: str) -> dict:
    """FFmpeg output options for the join, for whatever *output_path* names."""
    return {**JOIN_COMMON, **_spec_for_path(output_path)["mux_options"]}


def subtitle_codec(output_path: str) -> str:
    """``-c:s`` for a remux writing *output_path*."""
    return _spec_for_path(output_path)["subtitle_codec"]


def mux_options(output_path: str) -> dict:
    """Muxer-private options for a remux writing *output_path*."""
    return dict(_spec_for_path(output_path)["mux_options"])


def mime_for(path: str) -> str:
    """The media type to serve *path* as.

    Every file used to be announced as ``video/mp4``, which was already wrong
    for the MKVs the downloader produced and would now be wrong half the time.
    """
    ext = os.path.splitext(path or "")[1].lower()
    for entry in CONTAINERS.values():
        if entry["extension"] == ext:
            return entry["mime"]
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"
