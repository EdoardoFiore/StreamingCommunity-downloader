from urllib.parse import urlparse, parse_qs
import re
import json
import requests
from .headers import get_headers


def _parse_content(embed_content, url_embed):
    """Parse video metadata from embed page HTML. Shared between film.py and tv.py."""
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


def _get_m3u8_key(json_win_video, json_win_param, referer):
    """Fetch AES decryption key from vixcloud.co."""
    req = requests.get(
        "https://vixcloud.co/storage/enc.key",
        headers={"user-agent": get_headers(), "referer": referer},
    )
    if req.ok:
        return "".join([f"{c:02x}" for c in req.content])
    raise RuntimeError(f"Cannot fetch encryption key: HTTP {req.status_code}")


def _get_m3u8_url(json_win_video, json_win_param, add_b1=False):
    """Build M3U8 playlist URL for vixcloud.co."""
    base = f"https://vixcloud.co/playlist/{json_win_video['id']}"
    url = f"{base}?"
    if add_b1:
        url += "b=1&"
    url += f"token={json_win_param['token']}&expires={json_win_param['expires']}"
    if json_win_video.get("can_play_fhd"):
        url += "&h=1"
    if json_win_video.get("scz"):
        url += "&scz=1"
    url += f"&lang={json_win_video.get('lang', 'it')}"
    return url
