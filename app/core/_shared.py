from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs, urljoin
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


class MissingAudioTrackError(RuntimeError):
    """A requested audio language is not present in the source.

    Raised only when the caller asked for strict behaviour. Downloading with a
    different audio track than the one a user chose is worse than failing: the
    file looks correct, plays in the wrong language, and nobody is told.
    """

    def __init__(self, language: str, available: list[str]):
        self.language = language
        self.available = available
        super().__init__(
            f"Traccia audio '{language}' non disponibile "
            f"(presenti: {', '.join(available) or 'nessuna'})"
        )


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


# ── Alternative stream resolution ─────────────────────────────────────────────
#
# The panel had one road to a playlist: the source's iframe, then the vixcloud
# embed page. When Cloudflare answers that page with a challenge the scraper
# cannot clear, or the page changes shape, every download of every title stops
# and there is nothing to fall back on.
#
# vixsrc.to serves the same streams keyed by TMDB id, which the title page hands
# us for free. It is a second road, not a replacement: the primary is tried
# first and unchanged, and this runs only when it has already failed.

VIXSRC_HOST = "vixsrc.to"

# Hosts a playlist may name as the source of its AES key. The playlist is
# published by the stream page, so an unconstrained URI here would have the
# panel fetching whatever that page asked it to.
_KEY_HOSTS = ("vixcloud.co", "vixsrc.to")


@dataclass
class StreamSource:
    """A resolved playlist, whichever provider produced it."""

    m3u8_url: str
    key_hex: str | None
    referer: str
    provider: str  # "vixcloud" | "vixsrc"


class StreamResolutionError(RuntimeError):
    """Both roads to a playlist failed.

    Carries each failure separately, because they say different things: the
    primary one is what usually needs fixing, and the fallback's is what says
    whether there was ever a second chance. The message is one Italian sentence
    because it lands on job.error, on the notification bell and in a webhook.
    """

    def __init__(self, primary: Exception, fallback: Exception | None):
        self.primary_error = primary
        self.fallback_error = fallback
        detail = f"sorgente principale: {primary}"
        if fallback is not None:
            detail += f"; sorgente alternativa: {fallback}"
        super().__init__(f"Impossibile risolvere lo stream ({detail})")


def parse_vixsrc_page(html: str, page_url: str) -> tuple[str, dict, bool]:
    """Playlist URL, its query parameters, and whether 1080p is on offer."""
    url_match = re.search(
        r"window\.masterPlaylist\s*=\s*\{[\s\S]*?url\s*:\s*['\"]([^'\"]+)['\"]", html
    )
    if not url_match:
        raise RuntimeError(f"Nessuna playlist in {page_url}")

    params_match = re.search(
        r"window\.masterPlaylist[^:]*?params\s*:\s*\{([^}]*)\}", html, re.DOTALL
    )
    if not params_match:
        raise RuntimeError(f"Nessun token di playlist in {page_url}")

    # Same tolerant normalisation the vixcloud embed needs: this is JavaScript
    # object syntax, not JSON — single quotes and a trailing comma.
    raw = "{" + params_match.group(1).replace("\n", "").replace(" ", "") + "}"
    params = json.loads(raw.replace(",}", "}").replace("'", '"'))

    can_play_fhd = bool(
        re.search(r"window\.canPlayFHD\s*=\s*true", html)
    )
    return url_match.group(1), params, can_play_fhd


def build_vixsrc_playlist_url(url: str, params: dict, can_play_fhd: bool) -> str:
    """Attach the token to the playlist URL.

    The separator is computed rather than assumed: the URL on the page may
    already carry a query, and a second '?' produces a link the CDN rejects.
    """
    separator = "&" if urlparse(url).query else "?"
    full = f"{url}{separator}expires={params['expires']}&token={params['token']}"
    if can_play_fhd:
        full += "&h=1"
    return full


def fetch_vixsrc(tmdb_id: int, media_type: str, season=None, episode=None) -> StreamSource:
    """Resolve a playlist from vixsrc.to using the title's TMDB id."""
    if media_type == "movie":
        page_url = f"https://{VIXSRC_HOST}/movie/{tmdb_id}"
    else:
        if season is None or episode is None:
            raise RuntimeError("Stagione o episodio mancanti per la sorgente alternativa")
        page_url = f"https://{VIXSRC_HOST}/tv/{tmdb_id}/{season}/{episode}"

    # Through the shared cloudscraper session: vixsrc sits behind Cloudflare in
    # the same way vixcloud does.
    response = _get_scraper().get(
        page_url, headers={"user-agent": get_headers()}, timeout=15
    )
    response.raise_for_status()

    url, params, can_play_fhd = parse_vixsrc_page(response.text, page_url)
    playlist_url = build_vixsrc_playlist_url(url, params, can_play_fhd)
    referer = f"https://{VIXSRC_HOST}/"
    return StreamSource(
        m3u8_url=playlist_url,
        key_hex=fetch_key_from_playlist(playlist_url, referer),
        referer=referer,
        provider="vixsrc",
    )


def _key_uri_from(text: str) -> tuple[str | None, str | None]:
    """The EXT-X-KEY URI in a playlist, and the highest-bandwidth variant in it."""
    from app.core.m3u8 import M3U8_Parser

    parser = M3U8_Parser()
    parser.parse_data(text)
    keys = parser.keys if isinstance(parser.keys, dict) else {}
    if keys.get("method") and keys["method"].upper() != "NONE" and keys.get("uri"):
        return keys["uri"], None
    return None, parser.get_best_quality()


def fetch_key_from_playlist(master_url: str, referer: str) -> str | None:
    """The AES key a playlist names, or None when it is not encrypted.

    Read rather than assumed. The vixcloud path can hardcode
    ``vixcloud.co/storage/enc.key`` because that is where it has always been;
    a different provider has no obligation to agree, and guessing produces a
    file full of correctly-downloaded garbage rather than an error.
    """
    from app.core.m3u8 import _fetch_text_with_b1_fallback

    key_uri, best_variant = _key_uri_from(_fetch_text_with_b1_fallback(master_url))

    # The key usually lives on the media playlist rather than the master, so
    # follow the best variant once if the master carried none.
    if key_uri is None and best_variant:
        variant_url = urljoin(master_url, best_variant)
        key_uri, _ = _key_uri_from(_fetch_text_with_b1_fallback(variant_url))
        if key_uri:
            key_uri = urljoin(variant_url, key_uri)
    elif key_uri:
        key_uri = urljoin(master_url, key_uri)

    if not key_uri:
        return None

    parsed = urlparse(key_uri)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(
        host == allowed or host.endswith("." + allowed) for allowed in _KEY_HOSTS
    ):
        raise RuntimeError(f"Chiave di cifratura su un host non consentito: {host or key_uri}")

    headers = {"user-agent": get_headers(), "referer": referer}
    for attempt in range(4):
        req = requests.get(key_uri, headers=headers, timeout=15)
        if req.ok:
            return "".join(f"{c:02x}" for c in req.content)
        if req.status_code >= 500 and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        raise RuntimeError(f"Cannot fetch encryption key: HTTP {req.status_code}")
    raise RuntimeError("Cannot fetch encryption key after 4 attempts")


def resolve_stream(primary, *, tmdb_id=None, media_type="movie",
                   season=None, episode=None) -> StreamSource:
    """Try the source's own embed, then vixsrc.

    ``primary`` is a callable returning a StreamSource. When it fails and there
    is no tmdb_id, the *original* exception is re-raised rather than a complaint
    about the missing id: the caller needs to know Cloudflare blocked them, not
    that a second road they never had was unavailable.
    """
    try:
        return primary()
    except Exception as primary_error:
        if not tmdb_id:
            logger.info("No tmdb_id available, no fallback to try: %s", primary_error)
            raise

        logger.warning("Primary stream resolution failed (%s), trying vixsrc", primary_error)
        try:
            return fetch_vixsrc(int(tmdb_id), media_type, season, episode)
        except Exception as fallback_error:
            logger.warning("vixsrc fallback failed too: %s", fallback_error)
            raise StreamResolutionError(primary_error, fallback_error) from primary_error
