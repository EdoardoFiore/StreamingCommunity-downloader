import logging
import os

import requests
from bs4 import BeautifulSoup

from app.core.headers import get_headers, sanitize_filename
from app.core.m3u8 import download_m3u8, fetch_master_languages, M3U8_Parser
from app.core._shared import _parse_content, _get_m3u8_key, _get_m3u8_url

logger = logging.getLogger(__name__)


def _get_iframe(id_title, domain):
    ua = get_headers()
    for path in (f"/iframe/{id_title}", f"/it/iframe/{id_title}"):
        req = requests.get(f"https://{domain}{path}", headers={"User-agent": ua})
        if req.ok:
            break
    else:
        raise RuntimeError(f"Cannot fetch iframe: HTTP {req.status_code}")

    url_embed = BeautifulSoup(req.text, "lxml").find("iframe").get("src")
    req_embed = requests.get(url_embed, headers={"User-agent": get_headers()}).text

    script = BeautifulSoup(req_embed, "lxml").find("body").find("script")
    if script is None:
        raise RuntimeError("Video not available (no script tag found in embed)")
    logger.info("Embed script (first 800 chars): %s", script.text[:800])
    return script.text, url_embed


_AUDIO_LANG_ALIASES = {
    "ita": ("it", "Italian", "Italiano"),
    "eng": ("en", "English"),
    "fra": ("fr", "French", "Français"),
    "spa": ("es", "Spanish", "Español"),
    "deu": ("de", "German", "Deutsch"),
    "por": ("pt", "Portuguese", "Português"),
    "jpn": ("ja", "Japanese", "日本語"),
}


def _get_audio_track_url(parser, lang_code: str) -> str | None:
    """Match audio track by ISO 639-2 code, ISO 639-1 code, or full name."""
    aliases = {lang_code} | set(_AUDIO_LANG_ALIASES.get(lang_code, ()))
    if parser.audio_ts:
        for obj_audio in parser.audio_ts:
            if obj_audio.get("language") in aliases or obj_audio.get("name") in aliases:
                return obj_audio.get("uri")
    return None


def _collect_audio_tracks(m3u8_url: str, referer: str, audio_languages: list[str]) -> list[dict]:
    tracks = []
    try:
        headers = {"user-agent": get_headers(), "referer": referer}
        req = requests.get(m3u8_url, headers=headers, timeout=15)
        if req.status_code == 403:
            b1_url = m3u8_url + ("&b=1" if "?" in m3u8_url else "?b=1")
            logger.warning("Master M3U8 returned 403, retrying with ?b=1 for audio track collection")
            req = requests.get(b1_url, headers=headers, timeout=15)
        if not req.ok:
            logger.warning("Could not fetch master M3U8 for audio tracks: HTTP %d", req.status_code)
            return tracks
        parser = M3U8_Parser()
        parser.parse_data(req.text)
        for lang in audio_languages:
            url = _get_audio_track_url(parser, lang)
            if url:
                tracks.append({"url": url, "language": lang})
                logger.info("Audio track found for %s: %s", lang, url[:80])
            else:
                logger.warning("Audio track not found for language: %s (available: %s)",
                               lang, [t.get("language") or t.get("name") for t in parser.audio_ts])
    except Exception as e:
        logger.warning("Could not collect audio tracks: %s", e)
    return tracks


def _collect_subtitle_tracks(m3u8_url: str, referer: str, subtitle_languages: list[str]) -> list[dict]:
    tracks = []
    try:
        req = requests.get(m3u8_url, headers={"user-agent": get_headers(), "referer": referer}, timeout=15)
        if not req.ok:
            return tracks
        parser = M3U8_Parser()
        parser.parse_data(req.text)
        for lang in subtitle_languages:
            for sub in parser.subtitle_playlist:
                if sub.get("language") == lang:
                    tracks.append({"uri": sub.get("uri"), "language": lang})
                    logger.info("Subtitle track found for %s: %s", lang, sub.get("uri", "")[:80])
                    break
    except Exception as e:
        logger.warning("Could not collect subtitle tracks: %s", e)
    return tracks


def get_film_languages(id_film: int, domain: str) -> dict:
    from urllib.parse import urlparse, parse_qs
    embed_content, url_embed = _get_iframe(id_film, domain)
    json_win_video, json_win_param = _parse_content(embed_content, url_embed)
    m3u8_url = _get_m3u8_url(json_win_video, json_win_param)
    referer = (
        f"https://vixcloud.co/embed/{json_win_video['id']}"
        f"?token={json_win_param['token']}&expires={json_win_param['expires']}"
    )
    langs = fetch_master_languages(m3u8_url, referer)
    explicit_lang = parse_qs(urlparse(url_embed).query).get("lang", [None])[0]
    langs["lang"] = explicit_lang
    return langs


def download_film(id_film: int, title_name: str, domain: str,
                  output_dir: str = "videos",
                  temp_dir: str = None,
                  progress_factory=None,
                  year: str = None,
                  cancel_event=None,
                  audio_languages: list[str] = None,
                  subtitle_languages: list[str] = None):
    audio_languages = audio_languages or ["ita"]
    subtitle_languages = subtitle_languages or []

    embed_content, embed_referer = _get_iframe(id_film, domain)
    json_win_video, json_win_param = _parse_content(embed_content, embed_referer)
    logger.info("Video ID: %s token: %.8s... embed_url: %s", json_win_video['id'], json_win_param.get('token', ''), embed_referer[:80])
    logger.info("Audio language: %s", json_win_video.get("lang", "it"))

    m3u8_url = _get_m3u8_url(json_win_video, json_win_param)
    m3u8_key = _get_m3u8_key(json_win_video, json_win_param, embed_referer)

    audio_track_urls = _collect_audio_tracks(m3u8_url, embed_referer, audio_languages)
    subtitle_track_urls = _collect_subtitle_tracks(m3u8_url, embed_referer, subtitle_languages)

    mp4_name = sanitize_filename(title_name.replace("+", " ").replace(",", ""))
    folder_name = f"{mp4_name} ({year})" if year else mp4_name
    mp4_path = os.path.join(output_dir, folder_name, folder_name + ".mp4")

    download_m3u8(
        m3u8_index=m3u8_url,
        key=m3u8_key,
        output_filename=mp4_path,
        temp_dir=temp_dir,
        progress_factory=progress_factory,
        referer=embed_referer,
        cancel_event=cancel_event,
        audio_languages=audio_languages,
        subtitle_languages=subtitle_languages,
        audio_track_urls=audio_track_urls,
        subtitle_track_urls=subtitle_track_urls,
    )

    return mp4_path
