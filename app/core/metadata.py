"""Plot, genres, rating, artwork and a trailer for one title.

All of it comes from the title page's own props — the Inertia payload the panel
already fetches to find ``tmdb_id``. So metadata costs no request of its own,
needs no account anywhere, and works on a fresh install with nothing configured.

TMDB was tried as a second provider and removed. Measured against the props on
real titles it was not an upgrade: the site copies its synopses from TMDB, so
the text was usually identical, while the round trip lost a trailer on one
title, a logo on another, and 1100 characters of plot on a third. Paying an API
key to fetch a worse copy of data already in hand is a bad trade, and the
"three providers" this module started with are now honestly one.

``tmdb_id`` is still read and still exposed: it is free here, and it is the
identifier a fallback stream provider would resolve against (see
``app.core._shared.resolve_stream``).

Enrichment happens when a detail modal opens, one title at a time — never per
search card. Twenty-one titles per search is twenty-one requests for data
nineteen of which nobody will read.
"""

import logging
import threading
import time

from app.config import configured_domain

logger = logging.getLogger(__name__)

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

def _cache_key(media_type: str, title_id) -> tuple:
    return (media_type, str(title_id))


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

    A stream fallback would need an id at download time but must not pay for a
    lookup to get one: a download that is about to succeed should not spend a
    round trip discovering a fallback it will not use. In practice the detail
    modal has just filled this in; cold means None, which is exactly the
    behaviour there has always been.
    """
    hit = _cached(_cache_key(media_type, title_id))
    return hit.get("tmdb_id") if hit else None


# ── Normalisation ─────────────────────────────────────────────────────────────

def _source_image(images: list | None, kind: str) -> str | None:
    for image in images or []:
        if image.get("type") == kind and image.get("filename"):
            return f"/api/image/{image['filename']}"
    return None


def _from_props(props: dict) -> dict:
    """Everything the title page carries — which is nearly everything."""
    score = props.get("score")
    trailers = props.get("trailers") or []
    youtube_id = trailers[0].get("youtube_id") if trailers else None
    return {
        "source": "site" if (props.get("plot") or props.get("images")) else "none",
        "plot": props.get("plot") or None,
        "genres": [g["name"] for g in props.get("genres") or [] if g.get("name")],
        "rating": round(float(score), 1) if score else None,
        "runtime": props.get("runtime"),
        "backdrop": (_source_image(props.get("images"), "background")
                     or _source_image(props.get("images"), "cover")),
        "logo": _source_image(props.get("images"), "logo"),
        "trailer_url": (
            f"https://www.youtube.com/watch?v={youtube_id}" if youtube_id else None
        ),
        "tmdb_id": props.get("tmdb_id"),
    }


# ── The orchestrator ──────────────────────────────────────────────────────────

def title_metadata(media_type: str, title_id, slug: str, version: str) -> dict:
    """Everything known about one title.

    Never raises for a missing answer: a detail modal with no plot is a modal
    with no plot, not an error page.
    """
    key = _cache_key(media_type, title_id)
    hit = _cached(key)
    if hit is not None:
        return hit

    result = _resolve(title_id, slug, version)
    _store(key, result)
    return result


def _resolve(title_id, slug: str, version: str) -> dict:
    domain = configured_domain()
    if not domain or not slug:
        return dict(EMPTY)

    try:
        from app.core.tv import get_title_props

        props = get_title_props(title_id, slug, version or "", domain) or {}
    except Exception as exc:
        logger.info("Cannot read title props for %s: %s", title_id, exc)
        return dict(EMPTY)

    return _from_props(props) if props else dict(EMPTY)
