"""Plot, genres, artwork and a trailer for one title.

Three providers, tried in order, because each one fails differently:

1. **TMDB**, when an API key is configured. Italian text, a backdrop, a logo and
   a trailer — the things the source does not carry well or at all.
2. **The source's own preview endpoint**, which needs no key. Plot, genres,
   runtime and images, good enough that a panel with no TMDB key is not empty.
3. **What the title page already gave us**, since fetching it is how ``tmdb_id``
   was found in the first place.

The result has the same shape whichever answered, so the frontend never branches
on where a plot came from.

Enrichment happens when a detail modal opens, one title at a time — never per
search card. Twenty-one titles per search is twenty-one requests to somebody
else's API for data nineteen of which nobody will read.
"""

import logging
import threading
import time

import requests

from app.config import configured_domain
from app.core.headers import get_headers

logger = logging.getLogger(__name__)

TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMAGE_SIZES = {"backdrop": "w1280", "logo": "w500", "poster": "w500"}

# Cache lifetimes. A hit is stable — a film's plot does not change — while a
# miss is usually a title the source has no metadata for, and re-asking on every
# modal open would make a broken title the most expensive one in the panel.
_TTL_HIT = 6 * 60 * 60
_TTL_MISS = 10 * 60
_MAX_ENTRIES = 512

# In memory, which is the same bet the rest of the panel makes: JobManager holds
# download state the same way and the process is single by design.
_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _now() -> float:
    """Wall clock, indirected so tests can expire an entry without sleeping."""
    return time.time()


EMPTY: dict = {
    "source": "none",
    "plot": None,
    "genres": [],
    "rating": None,
    "runtime": None,
    "backdrop": None,
    "logo": None,
    "trailer_url": None,
    "tmdb_id": None,
}


# ── Cache ─────────────────────────────────────────────────────────────────────

def _cache_key(media_type: str, title_id, tmdb_configured: bool) -> tuple:
    # The key includes whether a TMDB key is configured: without it, adding one
    # would keep serving the keyless answer for six hours.
    return (media_type, str(title_id), tmdb_configured)


def _cached(key: tuple) -> dict | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < _now():
            _cache.pop(key, None)
            return None
        return value


def _store(key: tuple, value: dict) -> None:
    ttl = _TTL_HIT if value.get("source") != "none" else _TTL_MISS
    with _cache_lock:
        if len(_cache) >= _MAX_ENTRIES:
            now = _now()
            for stale in [k for k, (exp, _) in _cache.items() if exp < now]:
                _cache.pop(stale, None)
            if len(_cache) >= _MAX_ENTRIES:
                _cache.pop(next(iter(_cache)), None)
        _cache[key] = (_now() + ttl, value)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def cached_tmdb_id(media_type: str, title_id) -> int | None:
    """The TMDB id for this title if it is already known. Never does any I/O.

    The stream fallback needs a tmdb_id at download time but must not pay for a
    lookup to get one — a download that cannot resolve is worth a fallback, a
    download that is about to work is not worth an extra round trip. In practice
    the detail modal has just fetched this title's metadata, so it is warm; cold
    means None, which is exactly the behaviour there was before.
    """
    for configured in (True, False):
        hit = _cached(_cache_key(media_type, title_id, configured))
        if hit and hit.get("tmdb_id"):
            return hit["tmdb_id"]
    return None


# ── Providers ─────────────────────────────────────────────────────────────────

def tmdb_api_key() -> str | None:
    from app.auth import models as auth_models

    return (auth_models.get_setting(auth_models.SETTING_TMDB_API_KEY) or "").strip() or None


def source_preview(title_id, domain: str) -> dict:
    """The site's own metadata endpoint. No key, no scraping."""
    res = requests.post(
        f"https://{domain}/api/titles/preview/{title_id}",
        headers={"user-agent": get_headers(), "referer": f"https://{domain}/"},
        timeout=10,
    )
    if not res.ok:
        raise RuntimeError(f"Preview failed: HTTP {res.status_code}")
    return res.json()


def tmdb_details(tmdb_id: int, media_type: str, api_key: str) -> dict:
    kind = "movie" if media_type == "movie" else "tv"
    res = requests.get(
        f"{TMDB_API}/{kind}/{tmdb_id}",
        params={
            "api_key": api_key,
            "language": "it-IT",
            "append_to_response": "videos,images",
            # Italian first, then English, then language-neutral artwork: a logo
            # with no text on it is fine in any language.
            "include_image_language": "it,en,null",
        },
        headers={"accept": "application/json"},
        timeout=10,
    )
    if not res.ok:
        raise RuntimeError(f"TMDB failed: HTTP {res.status_code}")
    return res.json()


# ── Normalisation ─────────────────────────────────────────────────────────────

def _tmdb_image(path: str | None, kind: str) -> str | None:
    if not path:
        return None
    size = TMDB_IMAGE_SIZES.get(kind, "w500")
    return f"/api/image/tmdb/{size}{path if path.startswith('/') else '/' + path}"


def _source_image(images: list | None, kind: str) -> str | None:
    for image in images or []:
        if image.get("type") == kind and image.get("filename"):
            return f"/api/image/{image['filename']}"
    return None


def _from_tmdb(payload: dict, tmdb_id: int) -> dict:
    videos = (payload.get("videos") or {}).get("results") or []
    trailer = next(
        (
            v for v in videos
            if v.get("site") == "YouTube" and v.get("type") in ("Trailer", "Teaser")
        ),
        None,
    )
    logos = (payload.get("images") or {}).get("logos") or []
    runtime = payload.get("runtime")
    if runtime is None:
        episode_runtimes = payload.get("episode_run_time") or []
        runtime = episode_runtimes[0] if episode_runtimes else None

    return {
        "source": "tmdb",
        "plot": payload.get("overview") or None,
        "genres": [g["name"] for g in payload.get("genres") or [] if g.get("name")],
        "rating": round(payload["vote_average"], 1) if payload.get("vote_average") else None,
        "runtime": runtime,
        "backdrop": _tmdb_image(payload.get("backdrop_path"), "backdrop"),
        "logo": _tmdb_image(logos[0].get("file_path") if logos else None, "logo"),
        "trailer_url": (
            f"https://www.youtube.com/watch?v={trailer['key']}" if trailer else None
        ),
        "tmdb_id": tmdb_id,
    }


def _from_source(payload: dict, tmdb_id: int | None) -> dict:
    images = payload.get("images") or []
    return {
        "source": "site",
        "plot": payload.get("plot") or None,
        "genres": [g["name"] for g in payload.get("genres") or [] if g.get("name")],
        "rating": None,
        "runtime": payload.get("runtime"),
        "backdrop": _source_image(images, "background") or _source_image(images, "cover"),
        "logo": _source_image(images, "logo"),
        "trailer_url": None,
        "tmdb_id": tmdb_id,
    }


def _from_props(props: dict) -> dict:
    """Last resort: whatever the title page happened to carry."""
    score = props.get("score")
    trailers = props.get("trailers") or []
    youtube_id = trailers[0].get("youtube_id") if trailers else None
    return {
        "source": "site" if (props.get("plot") or props.get("images")) else "none",
        "plot": props.get("plot") or None,
        "genres": [g["name"] for g in props.get("genres") or [] if g.get("name")],
        "rating": round(float(score), 1) if score else None,
        "runtime": props.get("runtime"),
        "backdrop": _source_image(props.get("images"), "background"),
        "logo": _source_image(props.get("images"), "logo"),
        "trailer_url": (
            f"https://www.youtube.com/watch?v={youtube_id}" if youtube_id else None
        ),
        "tmdb_id": props.get("tmdb_id"),
    }


# ── The orchestrator ──────────────────────────────────────────────────────────

def title_metadata(media_type: str, title_id, slug: str, version: str) -> dict:
    """Everything known about one title, from whichever provider answered.

    Never raises for a missing answer: a detail modal with no plot is a modal
    with no plot, not an error page. Only a caller passing nonsense gets an
    exception.
    """
    api_key = tmdb_api_key()
    key = _cache_key(media_type, title_id, api_key is not None)
    hit = _cached(key)
    if hit is not None:
        return hit

    result = _resolve(media_type, title_id, slug, version, api_key)
    _store(key, result)
    return result


def _resolve(media_type: str, title_id, slug: str, version: str,
             api_key: str | None) -> dict:
    domain = configured_domain()
    props: dict = {}

    if domain and slug:
        try:
            from app.core.tv import get_title_props

            props = get_title_props(title_id, slug, version or "", domain) or {}
        except Exception as exc:
            logger.info("Cannot read title props for %s: %s", title_id, exc)

    tmdb_id = props.get("tmdb_id")

    if api_key and tmdb_id:
        try:
            return _from_tmdb(tmdb_details(int(tmdb_id), media_type, api_key), int(tmdb_id))
        except Exception as exc:
            logger.info("TMDB lookup failed for %s: %s", tmdb_id, exc)

    if domain:
        try:
            return _from_source(source_preview(title_id, domain), tmdb_id)
        except Exception as exc:
            logger.info("Source preview failed for %s: %s", title_id, exc)

    if props:
        return _from_props(props)
    return dict(EMPTY)
