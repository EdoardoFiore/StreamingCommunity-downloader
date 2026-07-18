from urllib.parse import urlparse, parse_qs
import re
import json
import time
import logging
import requests
from .headers import get_headers

logger = logging.getLogger(__name__)

_scraper = None


def _get_scraper():
    """Shared cloudscraper session for vixcloud.co pages behind Cloudflare.

    Falls back to a plain requests.Session if cloudscraper is unavailable.
    """
    global _scraper
    if _scraper is None:
        try:
            import cloudscraper
            _scraper = cloudscraper.create_scraper(
                browser={"browser": "firefox", "platform": "darwin", "desktop": True}
            )
        except ImportError:
            logger.warning("cloudscraper not installed, falling back to requests.Session")
            _scraper = requests.Session()
    return _scraper


def _fetch_vixcloud_embed(url_embed, referer=None):
    """Fetch a vixcloud.co /embed/ HTML page and return its <script> text.

    vixcloud.co serves the /embed/ page behind Cloudflare, so a plain
    requests.get intermittently returns a 403 challenge page. Route it through
    the cloudscraper session, which clears the challenge. (Downstream /playlist,
    /storage/enc.key and CDN segment URLs are NOT Cloudflare-gated.)
    """
    from bs4 import BeautifulSoup
    headers = {"user-agent": get_headers()}
    if referer:
        headers["referer"] = referer
    req = _get_scraper().get(url_embed, headers=headers, timeout=15)
    req.raise_for_status()
    body = BeautifulSoup(req.text, "lxml").find("body")
    if body is None:
        raise RuntimeError("Empty embed page body from vixcloud.co")
    script = body.find("script")
    if script is None:
        raise RuntimeError("Video not available (no script tag found in embed)")
    return script.text


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
    """Fetch AES decryption key from vixcloud.co. Retries on transient 5xx errors."""
    url = "https://vixcloud.co/storage/enc.key"
    headers = {"user-agent": get_headers(), "referer": referer}
    max_retries = 4
    for attempt in range(max_retries):
        req = requests.get(url, headers=headers, timeout=15)
        if req.ok:
            return "".join([f"{c:02x}" for c in req.content])
        if req.status_code >= 500 and attempt < max_retries - 1:
            delay = 2 ** attempt  # 1s, 2s, 4s
            logger.warning("enc.key returned HTTP %d, retrying in %ds (attempt %d/%d)",
                           req.status_code, delay, attempt + 1, max_retries)
            time.sleep(delay)
            continue
        raise RuntimeError(f"Cannot fetch encryption key: HTTP {req.status_code}")
    raise RuntimeError(f"Cannot fetch encryption key after {max_retries} attempts")


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
