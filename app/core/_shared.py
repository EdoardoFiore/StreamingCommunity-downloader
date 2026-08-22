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


# ── Stream resolution ─────────────────────────────────────────────────────────
#
# There is one road to a playlist: the source's iframe, then the vixcloud embed
# page. When Cloudflare answers that page with a challenge the scraper cannot
# clear, every download of that title stops.
#
# A second road was built through vixsrc.to, which serves the same streams keyed
# by TMDB id — an id the title page hands us for free. It was removed again
# after testing: vixsrc is now a client-rendered Next.js app whose HTML carries
# no playlist, no token and no .m3u8 at all, so the data would have to come from
# reverse-engineered internal calls that break silently on any of their deploys.
#
# What is left is the seam. resolve_stream() is where a fallback plugs in, the
# resolution context (tmdb_id, media type, season, episode) is already threaded
# from every caller, and fetch_key_from_playlist() can read a key from a
# playlist that does not put it where vixcloud does. Adding a provider means
# writing one function and listing it below.

# Hosts a playlist may name as the source of its AES key. The playlist is
# published by the stream page, so an unconstrained URI would have the panel
# fetching whatever that page asked it to.
_KEY_HOSTS = ("vixcloud.co",)

# Fallback providers, tried in order when the primary fails. Each takes
# (tmdb_id, media_type, season, episode) and returns a StreamSource.
#
# Empty: with none registered, resolve_stream() re-raises the primary failure
# unchanged, which is exactly the behaviour the panel had before any of this.
_FALLBACK_PROVIDERS: tuple = ()


@dataclass
class StreamSource:
    """A resolved playlist, whichever provider produced it."""

    m3u8_url: str
    key_hex: str | None
    referer: str
    provider: str


class StreamResolutionError(RuntimeError):
    """Every road to a playlist failed.

    Carries each failure separately, because they say different things: the
    primary one is what usually needs fixing, and a fallback's is what says
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
    ``vixcloud.co/storage/enc.key`` because that is where it has always been; a
    different provider has no obligation to agree, and guessing produces a file
    full of correctly-downloaded garbage rather than an error. Unused while no
    fallback is registered, and the reason a new one would not have to solve
    this again.
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
    """Resolve a playlist: the source's own embed, then any fallback provider.

    ``primary`` is a callable returning a StreamSource. When it fails and there
    is nothing else to try — no provider registered, or no tmdb_id to try one
    with — the *original* exception is re-raised rather than a complaint about
    the missing alternative: the caller needs to know Cloudflare blocked them,
    not that a second road they never had was unavailable.
    """
    try:
        return primary()
    except Exception as primary_error:
        if not _FALLBACK_PROVIDERS or not tmdb_id:
            raise

        logger.warning("Primary stream resolution failed (%s), trying %d fallback(s)",
                       primary_error, len(_FALLBACK_PROVIDERS))
        last_error: Exception | None = None
        for provider in _FALLBACK_PROVIDERS:
            try:
                return provider(int(tmdb_id), media_type, season, episode)
            except Exception as exc:
                logger.warning("Fallback %s failed: %s", getattr(provider, "__name__", provider), exc)
                last_error = exc
        raise StreamResolutionError(primary_error, last_error) from primary_error
