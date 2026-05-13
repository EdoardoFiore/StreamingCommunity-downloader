import logging
import os
from urllib.parse import unquote, quote

import requests
from bs4 import BeautifulSoup

from app.core.headers import get_headers, sanitize_filename
from app.core.m3u8 import download_m3u8, fetch_master_languages, M3U8_Parser
from app.core._shared import _parse_content, _get_m3u8_key, _get_m3u8_url

logger = logging.getLogger(__name__)


def fmt_ep(n) -> str:
    """Format episode number with zero-padded integer part (e.g. '7' → '07', '7.5' → '07.5')."""
    s = str(n)
    parts = s.split(".", 1)
    return parts[0].zfill(2) if len(parts) == 1 else parts[0].zfill(2) + "." + parts[1]


def get_token(id_tv: int, domain: str) -> str:
    session = requests.Session()
    ua = get_headers()
    for path in (f"/it/watch/{id_tv}", f"/watch/{id_tv}"):
        session.get(f"https://{domain}{path}", headers={"user-agent": ua}, timeout=10)
        if "XSRF-TOKEN" in session.cookies:
            return unquote(session.cookies["XSRF-TOKEN"])
    raise RuntimeError("XSRF-TOKEN cookie not found after page visit")


def get_info_tv(id_film: int, title_name: str, site_version: str, domain: str) -> int:
    req = requests.get(
        f"https://{domain}/it/titles/{id_film}-{title_name}",
        headers={
            "X-Inertia": "true",
            "X-Inertia-Version": site_version,
            "User-Agent": get_headers(),
        },
    )
    if req.ok:
        return req.json()["props"]["title"]["seasons_count"]
    raise RuntimeError(f"Cannot fetch TV info: HTTP {req.status_code}")


def get_info_season(tv_id: int, tv_name: str, domain: str, version: str, token: str, n_stagione: int) -> list[dict]:
    req = requests.get(
        f"https://{domain}/it/titles/{tv_id}-{tv_name}/season-{n_stagione}",
        headers={
            "authority": f"{domain}",
            "referer": f"https://{domain}/it/titles/{tv_id}-{tv_name}",
            "user-agent": get_headers(),
            "x-inertia": "true",
            "x-inertia-version": version,
            "x-xsrf-token": token,
        },
    )
    if req.ok:
        return [
            {"id": ep["id"], "n": ep["number"], "name": ep["name"]}
            for ep in req.json()["props"]["loadedSeason"]["episodes"]
        ]
    raise RuntimeError(f"Cannot fetch season info: HTTP {req.status_code}")


def _get_iframe(tv_id, ep_id, domain, token):
    ua = get_headers()
    params = {"episode_id": ep_id, "next_episode": "1"}
    cookies = {"XSRF-TOKEN": token}
    for path in (f"/iframe/{tv_id}", f"/it/iframe/{tv_id}"):
        req = requests.get(
            f"https://{domain}{path}",
            params=params,
            cookies=cookies,
            headers={
                "referer": f"https://{domain}/it/watch/{tv_id}?e={ep_id}",
                "user-agent": ua,
            },
        )
        if req.ok:
            break
    else:
        raise RuntimeError(f"Cannot fetch episode iframe: HTTP {req.status_code}")

    url_embed = BeautifulSoup(req.text, "lxml").find("iframe").get("src")
    req_embed = requests.get(url_embed, headers={"User-agent": get_headers()}).text
    return BeautifulSoup(req_embed, "lxml").find("body").find("script").text, url_embed


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
        req = requests.get(m3u8_url, headers={"user-agent": get_headers(), "referer": referer}, timeout=15)
        if not req.ok:
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


def get_tv_languages(tv_id: int, slug: str, domain: str, version: str) -> dict:
    """Detect available audio/subtitle languages using episode 1x01 as sample."""
    from urllib.parse import urlparse, parse_qs
    token = get_token(tv_id, domain)
    eps = get_info_season(tv_id, slug, domain, version, token, 1)
    if not eps:
        raise RuntimeError("No episodes found in season 1")
    embed_content, url_embed = _get_iframe(tv_id, eps[0]["id"], domain, token)
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


def download_episode(
    tv_id: int,
    eps: list[dict],
    ep_index: int,
    domain: str,
    token: str,
    tv_name: str,
    season: int,
    output_dir: str = "videos",
    temp_dir: str = None,
    progress_factory=None,
    cancel_event=None,
    year: str = None,
    audio_languages: list[str] = None,
    subtitle_languages: list[str] = None,
) -> str:
    audio_languages = audio_languages or ["ita"]
    subtitle_languages = subtitle_languages or []

    ep = eps[ep_index]
    logger.info(f"Downloading S{season:02d}E{fmt_ep(ep['n'])} — {ep['name']}")

    embed_content, url_embed = _get_iframe(tv_id, ep["id"], domain, token)
    json_win_video, json_win_param = _parse_content(embed_content, url_embed)
    logger.info("Video ID: %s token: %.8s... audio lang: %s", json_win_video['id'], json_win_param.get('token', ''), json_win_video.get('lang', 'it'))

    embed_referer = (
        f"https://vixcloud.co/embed/{json_win_video['id']}"
        f"?token={json_win_param['token']}&title={quote(tv_name)}"
        f"&referer=1&expires={json_win_param['expires']}"
        f"&description=S{season}%3AE{ep['n']}+{quote(ep['name'])}&nextEpisode=1"
    )
    m3u8_url = _get_m3u8_url(json_win_video, json_win_param)
    m3u8_key = _get_m3u8_key(json_win_video, json_win_param, embed_referer)

    audio_track_urls = _collect_audio_tracks(m3u8_url, embed_referer, audio_languages)
    subtitle_track_urls = _collect_subtitle_tracks(m3u8_url, embed_referer, subtitle_languages)

    safe_name = sanitize_filename(tv_name)
    series_folder = f"{safe_name} ({year})" if year else safe_name
    mp4_name = f"{safe_name} S{season:02d}E{fmt_ep(ep['n'])}"
    mp4_path = os.path.join(output_dir, series_folder, f"Season {season:02d}", mp4_name + ".mp4")

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
