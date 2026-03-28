import json
import logging
import os
import re

import requests
from bs4 import BeautifulSoup

from app.core.headers import get_headers
from app.core.m3u8 import download_m3u8

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


def _parse_content(embed_content):
    s = str(embed_content)

    video_id_m = re.search(r"window\.video\s*=\s*\{[^}]*?\bid\s*:\s*['\"]?(\d+)['\"]?", s, re.DOTALL)
    if not video_id_m:
        raise RuntimeError(f"Cannot find video ID in embed. Snippet: {s[:400]!r}")
    parsed_video = {"id": video_id_m.group(1)}

    win_param_m = re.search(r"params\s*:\s*\{([^}]*)\}", s, re.DOTALL)
    if not win_param_m:
        raise RuntimeError(f"Cannot find params in embed. Snippet: {s[:400]!r}")
    params_raw = win_param_m.group(1).replace("\n", "").replace(" ", "")
    json_win_param = "{" + params_raw + "}"
    json_win_param = json_win_param.replace(",}", "}").replace("'", '"')
    parsed_param = json.loads(json_win_param)

    # Extract the active stream base URL (e.g. ?ub=1) from window.streams
    streams_m = re.search(r"window\.streams\s*=\s*(\[.*?\]);", s, re.DOTALL)
    if streams_m:
        try:
            streams = json.loads(streams_m.group(1).replace("\\/", "/"))
            for stream in streams:
                if stream.get("active"):
                    parsed_video["active_stream_url"] = stream["url"]
                    break
        except Exception:
            pass

    if re.search(r"window\.canPlayFHD\s*=\s*true", s):
        parsed_video["can_play_fhd"] = True

    return parsed_video, parsed_param


def _get_m3u8_url(json_win_video, json_win_param):
    base = json_win_video.get("active_stream_url") or f"https://vixcloud.co/playlist/{json_win_video['id']}"
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}token={json_win_param['token']}&expires={json_win_param['expires']}"
    if json_win_param.get("asn"):
        url += f"&asn={json_win_param['asn']}"
    if json_win_video.get("can_play_fhd"):
        url += "&h=1"
    return url


def _get_m3u8_key(json_win_video, json_win_param, embed_referer):
    req = requests.get(
        "https://vixcloud.co/storage/enc.key",
        headers={"user-agent": get_headers(), "referer": embed_referer},
    )
    if req.ok:
        return "".join([f"{c:02x}" for c in req.content])
    raise RuntimeError(f"Cannot fetch encryption key: HTTP {req.status_code}")


def _get_m3u8_audio(json_win_video, json_win_param, embed_referer):
    m3u8_url = _get_m3u8_url(json_win_video, json_win_param)
    req = requests.get(
        m3u8_url,
        headers={"user-agent": get_headers(), "referer": embed_referer},
    )
    if req.ok:
        for row in req.text.split():
            if "audio" in str(row) and "ita" in str(row):
                return row.split(",")[-1].split('"')[-2]
        return None
    logger.warning("Audio playlist returned HTTP %d, skipping audio track", req.status_code)
    return None


def download_film(id_film: int, title_name: str, domain: str,
                  output_dir: str = "videos",
                  temp_dir: str = None,
                  progress_factory=None,
                  year: str = None,
                  cancel_event=None):
    embed_content, embed_referer = _get_iframe(id_film, domain)
    json_win_video, json_win_param = _parse_content(embed_content)
    logger.info("Video ID: %s token: %.8s... embed_url: %s", json_win_video['id'], json_win_param.get('token', ''), embed_referer[:80])

    m3u8_url = _get_m3u8_url(json_win_video, json_win_param)
    m3u8_key = _get_m3u8_key(json_win_video, json_win_param, embed_referer)
    m3u8_audio = _get_m3u8_audio(json_win_video, json_win_param, embed_referer)
    if m3u8_audio:
        logger.info("Audio track found, will merge")

    mp4_name = title_name.replace("+", " ").replace(",", "")
    folder_name = f"{mp4_name} ({year})" if year else mp4_name
    mp4_path = os.path.join(output_dir, folder_name, folder_name + ".mp4")

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
