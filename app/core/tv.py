import logging
import os
from urllib.parse import unquote, quote

import requests
from bs4 import BeautifulSoup

from app.core.headers import get_headers, sanitize_filename
from app.core.m3u8 import download_m3u8, fetch_master_languages, M3U8_Parser
from app.core._shared import (
    MissingAudioTrackError,
    _fetch_vixcloud_embed,
    _parse_content,
    _get_m3u8_key,
    _get_m3u8_url,
)
from app.core.paths import episode_path, fmt_ep  # noqa: F401  (fmt_ep re-exported)

logger = logging.getLogger(__name__)


def get_token(id_tv: int, domain: str) -> str:
    session = requests.Session()
    ua = get_headers()
    for path in (f"/it/watch/{id_tv}", f"/watch/{id_tv}"):
        session.get(f"https://{domain}{path}", headers={"user-agent": ua}, timeout=10)
        if "XSRF-TOKEN" in session.cookies:
            return unquote(session.cookies["XSRF-TOKEN"])
    raise RuntimeError("XSRF-TOKEN cookie not found after page visit")


def get_title_props(id_film: int, title_name: str, site_version: str, domain: str) -> dict:
    """The full ``props.title`` payload of a title page.

    This request was already being made to read one integer out of it, throwing
    away the plot, the genres, the images, the trailers and — the one that
    matters most — ``tmdb_id``, which is what lets the panel show real metadata
    and gives the vixsrc fallback something to resolve against.

    The same route serves films, despite living in tv.py: the site has one title
    page, and splitting this in two would mean two copies of the Inertia header
    dance.
    """
    req = requests.get(
        f"https://{domain}/it/titles/{id_film}-{title_name}",
        headers={
            "X-Inertia": "true",
            "X-Inertia-Version": site_version,
            "User-Agent": get_headers(),
        },
        timeout=15,
    )
    if not req.ok:
        raise RuntimeError(f"Cannot fetch TV info: HTTP {req.status_code}")
    return req.json()["props"]["title"]


def get_info_tv(id_film: int, title_name: str, site_version: str, domain: str) -> int:
    """How many seasons this series has.

    Signature and int return kept exactly as they were: the watch poller, the
    seasons endpoint and the batch download path all call this, and several
    tests monkeypatch it. Widening it to return the props would have been the
    obvious refactor and would have broken all of them.
    """
    return get_title_props(id_film, title_name, site_version, domain)["seasons_count"]


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
        timeout=15,
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
            timeout=15,
        )
        if req.ok:
            break
    else:
        raise RuntimeError(f"Cannot fetch episode iframe: HTTP {req.status_code}")

    url_embed = BeautifulSoup(req.text, "lxml").find("iframe").get("src")
    # vixcloud.co /embed/ is behind Cloudflare — fetch via cloudscraper.
    script_text = _fetch_vixcloud_embed(url_embed, referer=f"https://{domain}/")
    return script_text, url_embed


# Track collection is identical for films, episodes and anime — the same
# vixcloud master playlist in all three cases — so there is one
# implementation, in film.py, and strict mode cannot diverge between them.
from app.core.film import _collect_audio_tracks, _collect_subtitle_tracks  # noqa: E402


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


def get_episode_languages(tv_id: int, ep_id: int, domain: str, token: str) -> dict:
    """Languages available on one specific episode.

    get_tv_languages() samples S01E01, which is right for the browse view but
    not for verifying a request: tracks can differ per episode, and a request
    approved days later must be checked against the episode it actually names.
    """
    embed_content, url_embed = _get_iframe(tv_id, ep_id, domain, token)
    json_win_video, json_win_param = _parse_content(embed_content, url_embed)
    m3u8_url = _get_m3u8_url(json_win_video, json_win_param)
    referer = (
        f"https://vixcloud.co/embed/{json_win_video['id']}"
        f"?token={json_win_param['token']}&expires={json_win_param['expires']}"
    )
    return fetch_master_languages(m3u8_url, referer)


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
    strict_audio: bool = False,
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

    audio_track_urls = _collect_audio_tracks(
        m3u8_url, embed_referer, audio_languages, strict=strict_audio
    )
    subtitle_track_urls = _collect_subtitle_tracks(m3u8_url, embed_referer, subtitle_languages)

    mp4_path = episode_path(output_dir, tv_name, season, ep["n"], year)

    final_path = download_m3u8(
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

    # download_m3u8 returns the real output path (e.g. .mkv after remux)
    return final_path or mp4_path
