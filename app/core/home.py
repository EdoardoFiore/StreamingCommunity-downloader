"""The shelves each source puts on its own front page.

What the panel shows before anything is typed: what is trending, what was added
lately, today's top ten, the latest anime episodes. None of it is curated here —
these are the source's own lists, read out of the page it serves to any visitor.

Items come back in exactly the shape ``/api/search`` returns for that source, so
the grid, the detail modal and the request flow need no new vocabulary.
"""

import logging
import threading
import time


from app.core import animeunity, page

logger = logging.getLogger(__name__)

# A "trending" shelf fifteen minutes stale is indistinguishable from a fresh
# one; an hour is not.
_TTL = 15 * 60

# Only two keys are ever live. The bound exists so a pathological run of domain
# rotations cannot grow the dict.
_MAX_ENTRIES = 8

# How many titles of a shelf are worth rendering. The source sends thirty.
_SHELF_CAP = 30

_cache: dict[tuple, tuple[float, list]] = {}
_cache_lock = threading.Lock()


def _now() -> float:
    """Wall clock, indirected so tests can expire an entry without sleeping."""
    return time.time()


# The panel's own headings. The source's label is Italian copy it can re-word
# tomorrow, so the stable machine name is what the shelf is keyed on and this is
# what gets shown. A shelf nobody here has heard of still renders, under the
# source's own label — better a heading we did not choose than a vetrina that
# silently disappears the month the source adds one.
_LABELS = {
    ("streamingcommunity", "trending"): "Di tendenza",
    ("streamingcommunity", "latest"): "Aggiunti di recente",
    ("streamingcommunity", "top10"): "Top 10 di oggi",
    ("animeunity", "latest_episodes"): "Ultimi episodi",
    ("animeunity", "featured"): "In evidenza",
    ("animeunity", "top_week"): "Più visti della settimana",
    ("animeunity", "latest_added"): "Aggiunti di recente",
    ("animeunity", "upcoming"): "In arrivo",
}


# ── Cache ─────────────────────────────────────────────────────────────────────

def _cache_key(source: str, host: str) -> tuple:
    """Keyed on the host, not just the source.

    This is the structural half of surviving a domain rotation: the old
    domain's shelves cannot be served because they cannot be reached. The
    explicit half is clear_cache(), called where the domain is written.
    """
    return (source, host)


def _cached(key: tuple) -> list | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < _now():
            _cache.pop(key, None)
            return None
        return value


def _store(key: tuple, value: list) -> None:
    with _cache_lock:
        if len(_cache) >= _MAX_ENTRIES:
            now = _now()
            for stale in [k for k, (exp, _) in _cache.items() if exp < now]:
                _cache.pop(stale, None)
            if len(_cache) >= _MAX_ENTRIES:
                _cache.pop(next(iter(_cache)), None)
        _cache[key] = (_now() + _TTL, value)


def clear_cache(source: str | None = None) -> None:
    """Drop cached shelves, for one source or all of them."""
    with _cache_lock:
        if source is None:
            _cache.clear()
            return
        for key in [k for k in _cache if k[0] == source]:
            _cache.pop(key, None)


# ── StreamingCommunity ────────────────────────────────────────────────────────

def _sc_shelves(domain: str) -> list[dict]:
    """The home page's own sliders: trending, recently added, today's top ten."""
    props = page.fetch_site_props(domain)
    shelves = []
    for slider in props.get("sliders") or []:
        key = slider.get("name") or slider.get("label")
        if not key:
            continue
        items = [t for t in (page.normalize_title(raw)
                             for raw in (slider.get("titles") or [])[:_SHELF_CAP]) if t]
        if items:
            shelves.append(_shelf("streamingcommunity", key, items,
                                  fallback=slider.get("label")))
    return shelves


# ── AnimeUnity ────────────────────────────────────────────────────────────────

def _au_titles(rows) -> list[dict]:
    """Normalised titles, each once: two new episodes of a series are one card."""
    items, seen = [], set()
    for raw in rows if isinstance(rows, list) else []:
        item = animeunity.normalize_title(raw)
        if item and item["id"] not in seen:
            seen.add(item["id"])
            items.append(item)
    return items


def _au_shelves() -> list[dict]:
    """The front page's own lists, out of its Inertia payload (#50).

    The latest episodes are video rows — one per language — each carrying its
    episode and that episode's anime; the other lists are titles already.
    """
    props = animeunity.fetch_home_props()

    latest_rows = (props.get("latestEpisodes") or {}).get("data") or []
    latest = _au_titles([((row or {}).get("episode") or {}).get("anime")
                         for row in latest_rows if isinstance(row, dict)])
    top = props.get("topAnimes") or {}

    shelves = []
    for key, items in (
        ("latest_episodes", latest),
        ("featured", _au_titles(props.get("carouselAnimes"))),
        ("top_week", _au_titles(top.get("week") if isinstance(top, dict) else None)),
        ("latest_added", _au_titles(props.get("latestAnimes"))),
        ("upcoming", _au_titles(props.get("upcomingAnimes"))),
    ):
        if items:
            shelves.append(_shelf("animeunity", key, items[:_SHELF_CAP]))
    return shelves


# ── Assembly ──────────────────────────────────────────────────────────────────

def _shelf(source: str, key: str, items: list, fallback: str | None = None) -> dict:
    return {
        "key": key,
        "title": _LABELS.get((source, key)) or fallback or key,
        "items": items,
    }


def shelves(source: str, domain: str) -> list[dict]:
    """The front-page shelves for *source*, cached for a quarter of an hour.

    A failed fetch is **not** cached, which is where this parts company with
    ``metadata``. That one caches a miss for ten minutes because a title with no
    plot usually still has none ten minutes later. A home page that failed is a
    flap, and remembering it would blank the start page for every user for the
    duration — so the answer is an empty list now and a fresh try on the next
    page load.
    """
    key = _cache_key(source, domain)
    hit = _cached(key)
    if hit is not None:
        return hit

    try:
        result = _au_shelves() if source == "animeunity" else _sc_shelves(domain)
    except Exception:
        logger.exception("Cannot read the front page of %s", source)
        return []

    _store(key, result)
    return result
