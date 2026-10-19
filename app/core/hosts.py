"""The hosts besides the StreamingCommunity domain, and their manual overrides.

AnimeUnity and the video host (vixcloud) both move TLD from time to time. The
video host follows the embed URL on its own; AnimeUnity has a default. Either
can be set by hand in Settings › Sorgente, so a move the code does not foresee
is a form to fill in rather than a release to wait for.

A value saved here decides where the panel sends requests, so it is validated
as a bare hostname — no scheme, path, port or IP literal — and verified against
the service it claims to be before it is stored.
"""

import re
from urllib.parse import urlparse

import requests

from app.core.headers import get_headers

_HOSTNAME = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def normalize_host(value: str) -> str:
    """A bare lowercase hostname, or ValueError.

    Lenient about what people paste — ``https://www.animeunity.to/`` becomes
    ``www.animeunity.to`` — and strict about what is left: a path, a port or an
    IP address is refused rather than quietly dropped.
    """
    raw = (value or "").strip().lower()
    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "https") or parsed.path not in ("", "/") \
                or parsed.query or parsed.port:
            raise ValueError("Inserisci solo il nome host, ad esempio www.animeunity.to")
        raw = parsed.hostname or ""
    raw = raw.rstrip("/")
    if not _HOSTNAME.match(raw):
        raise ValueError(f"Host non valido: {value.strip()[:100]!r}")
    return raw


def verify_animeunity(host: str) -> None:
    """Raise RuntimeError unless ``host`` answers like AnimeUnity."""
    from app.core.animeunity import _get_scraper

    try:
        r = _get_scraper().get(f"https://{host}/", headers={"user-agent": get_headers()},
                               timeout=15)
        r.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"{host} non raggiungibile: {e}")
    if "animeunity" not in r.text.lower():
        raise RuntimeError(f"{host} risponde, ma non sembra AnimeUnity")


def verify_video_host(host: str) -> None:
    """Raise RuntimeError unless ``host`` serves a 16-byte AES key at /storage/enc.key."""
    try:
        r = requests.get(f"https://{host}/storage/enc.key",
                         headers={"user-agent": get_headers()}, timeout=15)
    except Exception as e:
        raise RuntimeError(f"{host} non raggiungibile: {e}")
    if not r.ok or len(r.content) != 16:
        raise RuntimeError(f"{host} non serve la chiave video (HTTP {r.status_code})")
