import os
import logging
import subprocess

import requests
import ffmpeg

from app.core.m3u8 import M3U8_Parser

logger = logging.getLogger(__name__)

LANG_MAP = {
    "ita": "it", "eng": "en", "fra": "fr", "spa": "es", "deu": "de",
    "por": "pt", "jpn": "ja", "zho": "zh", "ara": "ar", "rus": "ru",
    "kor": "ko", "nld": "nl", "pol": "pl", "swe": "sv", "nor": "no",
    "dan": "da", "fin": "fi", "tur": "tr", "ell": "el", "ces": "cs",
}

# Human-readable names for subtitle track titles, keyed on ISO 639-2 base code.
_LANG_NAME = {
    "ita": "Italian", "eng": "English", "fra": "French", "fre": "French",
    "spa": "Spanish", "deu": "German", "ger": "German", "por": "Portuguese",
    "jpn": "Japanese", "rus": "Russian", "ukr": "Ukrainian", "gre": "Greek",
    "ell": "Greek", "nld": "Dutch", "pol": "Polish", "ara": "Arabic",
    "zho": "Chinese", "kor": "Korean",
}


def _normalize_lang(raw: str) -> tuple[str, bool]:
    """Return (iso_639_2_code, is_forced) from a vixcloud language token.

    vixcloud uses ISO 639-2/B codes (eng, ita, ger, fre, gre, ukr...) plus a
    'forced-<code>' variant. MKV needs a valid 3-letter code, so 'forced-ita'
    must become language=ita + forced disposition, not a literal 'forced-ita'
    (which players show as 'Undefined').
    """
    forced = raw.startswith("forced-")
    code = raw[len("forced-"):] if forced else raw
    return code or "und", forced


def remux_to_mkv(video_path: str, audio_tracks: list[dict] = None, subtitle_tracks: list[dict] = None) -> str:
    if audio_tracks is None:
        audio_tracks = []
    if subtitle_tracks is None:
        subtitle_tracks = []

    if not audio_tracks and not subtitle_tracks:
        return video_path

    output_path = os.path.splitext(video_path)[0] + ".mkv"

    # Build the ffmpeg command by hand: per-stream -map/-metadata/-disposition are
    # OUTPUT options and MUST come after all -i inputs but before the output file.
    # (ffmpeg-python's .global_args put them after the output filename, where ffmpeg
    # silently ignores them — hence subtitle language tags never applied.)
    cmd = ["ffmpeg", "-y", "-i", video_path]
    for track in audio_tracks:
        cmd += ["-i", track["path"]]
    for track in subtitle_tracks:
        cmd += ["-i", track["path"]]

    # Stream mapping
    cmd += ["-map", "0:v:0"]
    if audio_tracks:
        for i in range(len(audio_tracks)):
            cmd += ["-map", f"{i + 1}:a:0"]
    else:
        cmd += ["-map", "0:a?"]  # keep original audio if no external tracks
    for i in range(len(subtitle_tracks)):
        cmd += ["-map", f"{len(audio_tracks) + 1 + i}:0"]

    # Codecs (copy everything; mkv natively holds webvtt)
    cmd += ["-c:v", "copy", "-c:a", "copy", "-c:s", "copy"]

    # Per-stream metadata (output options, correctly positioned)
    for i, track in enumerate(audio_tracks):
        code, _ = _normalize_lang(track.get("language", "und"))
        cmd += [f"-metadata:s:a:{i}", f"language={code}"]

    for i, track in enumerate(subtitle_tracks):
        code, forced = _normalize_lang(track.get("language", "und"))
        name = _LANG_NAME.get(code, code.upper())
        if forced:
            name += " (Forced)"
        cmd += [f"-metadata:s:s:{i}", f"language={code}"]
        cmd += [f"-metadata:s:s:{i}", f"title={name}"]
        cmd += [f"-disposition:s:{i}", "forced" if forced else "0"]

    cmd += [output_path]

    logger.info("Remuxing to MKV: video + %d audio + %d subtitle tracks",
                len(audio_tracks), len(subtitle_tracks))
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace") if proc.stderr else "(no stderr)"
        if len(stderr) > 500:
            stderr = f"...{stderr[-300:]}"
        raise RuntimeError(f"FFmpeg MKV remux error: {stderr}")

    if os.path.exists(output_path) and video_path != output_path:
        try:
            os.remove(video_path)
        except OSError:
            pass

    logger.info("MKV remux complete: %s", output_path)
    return output_path


def download_subtitle_tracks(parser, allowed_languages: list[str], output_dir: str, video_stem: str, temp_dir: str = None) -> list[dict]:
    downloaded = []

    if parser is None or not parser.subtitle_playlist:
        return downloaded

    save_dir = temp_dir if temp_dir else output_dir
    os.makedirs(save_dir, exist_ok=True)

    for sub_info in parser.subtitle_playlist:
        lang_code = sub_info.get("language", "")
        if lang_code not in allowed_languages:
            continue

        lang_short = LANG_MAP.get(lang_code, lang_code)
        out_path = os.path.join(save_dir, f"{video_stem}.{lang_short}.vtt")

        logger.info("Downloading subtitle: %s -> %s", lang_code, out_path)

        try:
            req_sub_content = requests.get(sub_info.get("uri"), timeout=15)
            if not req_sub_content.ok:
                logger.warning("Failed to fetch subtitle playlist for %s", lang_code)
                continue

            sub_parse = M3U8_Parser()
            sub_parse.parse_data(req_sub_content.text)

            if sub_parse.subtitle:
                content_resp = requests.get(sub_parse.subtitle[0], timeout=30)
                if content_resp.ok:
                    with open(out_path, "wb") as f:
                        f.write(content_resp.content)
                    downloaded.append({"path": out_path, "language": lang_code})
                    logger.info("Subtitle downloaded: %s", out_path)
        except Exception as e:
            logger.warning("Failed to download subtitle %s: %s", lang_code, e)

    return downloaded


def get_audio_track_url(parser, language_name: str) -> str | None:
    if parser.audio_ts:
        for obj_audio in parser.audio_ts:
            if obj_audio.get("name") == language_name:
                return obj_audio.get("uri")
    return None
