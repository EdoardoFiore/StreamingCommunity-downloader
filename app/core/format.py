import os
import logging

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


def remux_to_mkv(video_path: str, audio_tracks: list[dict] = None, subtitle_tracks: list[dict] = None) -> str:
    if audio_tracks is None:
        audio_tracks = []
    if subtitle_tracks is None:
        subtitle_tracks = []

    if not audio_tracks and not subtitle_tracks:
        return video_path

    output_path = os.path.splitext(video_path)[0] + ".mkv"

    streams = [ffmpeg.input(video_path)]
    # -map 0:v:0  (video from first input)
    extra_args = ["-map", "0:v:0"]

    for i, track in enumerate(audio_tracks):
        streams.append(ffmpeg.input(track["path"]))
        extra_args += ["-map", f"{i + 1}:a:0"]
        extra_args += [f"-metadata:s:a:{i}", f"language={track.get('language', 'und')}"]

    if not audio_tracks:
        # Keep original audio from video stream
        extra_args += ["-map", "0:a"]

    for i, track in enumerate(subtitle_tracks):
        streams.append(ffmpeg.input(track["path"]))
        extra_args += ["-map", f"{len(audio_tracks) + (0 if audio_tracks else 1) + i}:0"]
        extra_args += [f"-metadata:s:s:{i}", f"language={track.get('language', 'und')}"]

    try:
        output = (
            ffmpeg.output(*streams, output_path, vcodec="copy", acodec="copy", scodec="copy")
            .global_args(*extra_args)
        )
        logger.info("Remuxing to MKV: video + %d audio + %d subtitle tracks",
                    len(audio_tracks), len(subtitle_tracks))
        output.run(capture_stdout=True, capture_stderr=True, quiet=True)

        if os.path.exists(output_path) and video_path != output_path:
            try:
                os.remove(video_path)
            except OSError:
                pass

        logger.info("MKV remux complete: %s", output_path)
        return output_path

    except ffmpeg.Error as e:
        stderr = e.stderr.decode(errors="replace") if e.stderr else "(no stderr)"
        if len(stderr) > 500:
            stderr = f"...{stderr[-300:]}"
        raise RuntimeError(f"FFmpeg MKV remux error: {stderr}")


def download_subtitle_tracks(parser, allowed_languages: list[str], video_dir: str, video_stem: str) -> list[dict]:
    downloaded = []

    if parser is None or not parser.subtitle_playlist:
        return downloaded

    os.makedirs(video_dir, exist_ok=True)

    for sub_info in parser.subtitle_playlist:
        lang_code = sub_info.get("language", "")
        if lang_code not in allowed_languages:
            continue

        lang_short = LANG_MAP.get(lang_code, lang_code)
        out_path = os.path.join(video_dir, f"{video_stem}.{lang_short}.vtt")

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
