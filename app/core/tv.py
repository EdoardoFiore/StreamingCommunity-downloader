import json
import logging
import os
import re
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

from app.core.headers import get_headers
from app.core.m3u8 import download_m3u8

logger = logging.getLogger(__name__)


def get_token(id_tv: int, domain: str) -> str:
    session = requests.Session()
    ua = get_headers()
    # Try both locale-prefixed and bare paths
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


def _parse_content(embed_content, url_embed):
    from urllib.parse import urlparse, parse_qs
    s = str(embed_content)

    video_id_m = re.search(r"window\.video\s*=\s*\{[^}]*?\bid\s*:\s*['\"]?(\d+)['\"]?", s, re.DOTALL)
    if not video_id_m:
        raise RuntimeError(f"Cannot find video ID in embed. Snippet: {s[:400]!r}")
    parsed_video = {"id": video_id_m.group(1)}

    qs = parse_qs(urlparse(url_embed).query)
    parsed_video["can_play_fhd"] = bool(qs.get("canPlayFHD"))
    parsed_video["scz"] = bool(qs.get("scz"))
    parsed_video["lang"] = qs.get("lang", ["it"])[0]

    win_param_m = re.search(r"params\s*:\s*\{([^}]*)\}", s, re.DOTALL)
    if not win_param_m:
        raise RuntimeError(f"Cannot find params in embed. Snippet: {s[:400]!r}")
    params_raw = win_param_m.group(1).replace("\n", "").replace(" ", "")
    json_win_param = "{" + params_raw + "}"
    json_win_param = json_win_param.replace(",}", "}").replace("'", '"')
    parsed_param = json.loads(json_win_param)

    return parsed_video, parsed_param


def _get_m3u8_url(json_win_video, json_win_param):
    base = f"https://vixcloud.co/playlist/{json_win_video['id']}"
    url = f"{base}?token={json_win_param['token']}&expires={json_win_param['expires']}"
    if json_win_video.get("can_play_fhd"):
        url += "&h=1"
    if json_win_video.get("scz"):
        url += "&scz=1"
    url += f"&lang={json_win_video.get('lang', 'it')}"
    return url


def _get_m3u8_key(json_win_video, json_win_param, referer):
    req = requests.get(
        "https://vixcloud.co/storage/enc.key",
        headers={"referer": referer},
    )
    if req.ok:
        return "".join([f"{c:02x}" for c in req.content])
    raise RuntimeError(f"Cannot fetch encryption key: HTTP {req.status_code}")


def _get_m3u8_audio(json_win_video, json_win_param, referer):
    master_url = _get_m3u8_url(json_win_video, json_win_param)
    req = requests.get(master_url, headers={"referer": referer})
    if req.ok:
        for row in req.text.split():
            if "audio" in str(row) and "ita" in str(row):
                return row.split(",")[-1].split('"')[-2]
        return None
    logger.warning("Audio playlist returned HTTP %d, skipping audio track", req.status_code)
    return None


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
) -> str:
    ep = eps[ep_index]
    logger.info(f"Downloading S{season:02d}E{ep['n']:02d} — {ep['name']}")

    embed_content, url_embed = _get_iframe(tv_id, ep["id"], domain, token)
    json_win_video, json_win_param = _parse_content(embed_content, url_embed)
    logger.info("Video ID: %s token: %.8s...", json_win_video['id'], json_win_param.get('token', ''))

    embed_referer = (
        f"https://vixcloud.co/embed/{json_win_video['id']}"
        f"?token={json_win_param['token']}&title={tv_name}"
        f"&referer=1&expires={json_win_param['expires']}"
        f"&description=S{season}%3AE{ep['n']}+{ep['name']}&nextEpisode=1"
    )
    m3u8_url = _get_m3u8_url(json_win_video, json_win_param)
    m3u8_key = _get_m3u8_key(json_win_video, json_win_param, embed_referer)
    m3u8_audio = _get_m3u8_audio(json_win_video, json_win_param, embed_referer)

    if m3u8_audio:
        logger.info("Audio track found, will merge")

    mp4_name = f"{tv_name.replace('+', '_')}_S{season:02d}E{ep['n']:02d}"
    mp4_path = os.path.join(output_dir, tv_name, f"Stagione {season:02d}", mp4_name + ".mp4")

    download_m3u8(
        m3u8_index=m3u8_url,
        m3u8_audio=m3u8_audio,
        key=m3u8_key,
        output_filename=mp4_path,
        temp_dir=temp_dir,
        progress_factory=progress_factory,
        referer=embed_referer,
        cancel_event=cancel_event,
    )

    return mp4_path
