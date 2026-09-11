import json
import logging
import os
import re
import time

import requests
from bs4 import BeautifulSoup

from app.core.headers import sanitize_filename

logger = logging.getLogger(__name__)

ANIMEUNITY_HOST = os.getenv("ANIMEUNITY_HOST", "www.animeunity.so")
BATCH_SIZE = 120

# What /archivio/get-animes answers per call. Not ours to choose — it is the
# source's page size, and what one "Carica altri" is worth.
ARCHIVE_PAGE = 30

# A ceiling on what one call may normalise, for the day the source answers with
# more than a page.
MAX_RESULTS = 60

# The panel's word for a kind, and the source's own. The wire vocabulary is
# lower case so there is one canonical form; AnimeUnity capitalises its own.
TYPES = {
    "movie": "Movie", "tv": "TV", "ova": "OVA", "ona": "ONA", "special": "Special",
}

# The CSRF token stays good for a while and costs a page load to get, so it is
# not refetched per search.
_CSRF_TTL = 10 * 60

_scraper = None
_csrf: tuple[float, str] | None = None


def _now() -> float:
    """Indirected so a test can move the clock without sleeping."""
    return time.time()


def _get_scraper():
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


def fetch_home_html() -> str:
    """The AnimeUnity home page, refreshing the cached CSRF token on the way.

    Shared with the start page, which reads its shelves out of this same HTML.
    """
    global _csrf
    r = _get_scraper().get(f"https://{ANIMEUNITY_HOST}/", timeout=15)
    r.raise_for_status()
    try:
        meta = BeautifulSoup(r.text, "lxml").find("meta", {"name": "csrf-token"})
        if meta and meta.get("content"):
            _csrf = (_now() + _CSRF_TTL, meta["content"])
    except Exception as e:
        logger.debug("Error extracting CSRF token: %s", e)
    return r.text


def _csrf_token() -> str | None:
    """The cached token, fetching the home page when there is none or it is old."""
    global _csrf
    if _csrf and _csrf[0] > _now():
        return _csrf[1]
    _csrf = None
    fetch_home_html()
    return _csrf[1] if _csrf else None


def _ajax_headers(token: str | None) -> dict:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://{ANIMEUNITY_HOST}/archivio",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    if token:
        headers["X-CSRF-TOKEN"] = token
    return headers


def _genres(value) -> list[str]:
    """Genre names, whichever shape Laravel cast them into this time.

    Rows arrive as dicts with pivot tables, sometimes as plain strings, and
    occasionally as a JSON string. The panel wants names, and the alternative to
    tolerating all three is a TypeError raised in the middle of a search.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    names = []
    for g in (value or []):
        name = g.get("name") if isinstance(g, dict) else g
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def normalize_title(t: dict) -> dict | None:
    """One raw record into the shape the panel renders, or None if unusable."""
    title_str = (t.get("title_eng") or t.get("title") or t.get("name") or "").strip()
    id_num = t.get("id")
    if not title_str or id_num is None:
        return None
    slug = t.get("slug", "") or ""
    return {
        "id": f"{id_num}-{slug}" if slug else str(id_num),
        "name": title_str,
        "type": "anime",
        # The source's own classification — Movie, TV, OVA, ONA, Special. Kept
        # beside `type` rather than in it: `type` is "anime" for every record and
        # the whole anime flow keys off that, from the detail modal to the
        # episodes endpoint to the job kind. Without this the card had nothing to
        # go on, so it labelled every film a TV series.
        "media_type": t.get("type") or "",
        "slug": slug,
        # An absolute URL, unlike StreamingCommunity's bare filename: the
        # frontend branches on that and sends only the latter through /api/image.
        "poster": (t.get("imageurl") or t.get("cover") or
                   t.get("poster") or t.get("image") or ""),
        "episodes_count": t.get("episodes_count", 0),
        # The archive endpoint sends these with the search, so the detail panel
        # costs no request of its own.
        "plot": t.get("plot") or "",
        "genres": _genres(t.get("genres")),
        # AnimeUnity keeps an Italian dub as a record of its own: same show,
        # separate id and slug, "(ITA)" in the title.
        "dubbed": bool(t.get("dub")),
        "score": t.get("score") or t.get("vote"),
        "release_date": t.get("date") or t.get("release_date") or "",
    }


def _normalize_titles(titles: list) -> list[dict]:
    """Convert raw AnimeUnity records to the normalized format the frontend renders."""
    results, seen = [], set()
    for t in titles:
        item = normalize_title(t)
        if item is None or item["id"] in seen:
            continue
        seen.add(item["id"])
        results.append(item)
        if len(results) >= MAX_RESULTS:
            break
    return results


def _post_archive(body: dict):
    """POST the archive query, renewing the CSRF token once if it has gone stale.

    A 419 is Laravel saying the token expired. Fetching a new one and sending the
    request again is **re-authentication, not a retry** — a different request
    carrying a new credential — which is why it does not go through
    ``_shared.with_retry`` and must not be folded into it.
    """
    global _csrf
    scraper = _get_scraper()
    url = f"https://{ANIMEUNITY_HOST}/archivio/get-animes"

    r = scraper.post(url, json=body, headers=_ajax_headers(_csrf_token()), timeout=20)
    if r.status_code in (403, 419):
        logger.info("AnimeUnity refused the CSRF token (HTTP %d), renewing it",
                    r.status_code)
        _csrf = None
        r = scraper.post(url, json=body, headers=_ajax_headers(_csrf_token()), timeout=20)
    return r


def search(query: str, *, page: int = 1, media_type: str | None = None,
           dubbed: bool = False) -> list[dict]:
    """Search AnimeUnity through ``/archivio/get-animes``.

    Not ``/livesearch``, which this used to call: the source caps that at
    **eight records** for every query, with no way to ask for more and no
    filters — measured, not guessed. The archive endpoint answers thirty at a
    time, takes a row offset, and applies both filters itself.

    Both filters go to the source rather than to what comes back: filtering here
    could only ever narrow one page, and would answer "no Italian dub" for a show
    whose dub sat on page two. ``page`` is 1-based and becomes the row offset.
    """
    body = {"title": query, "offset": (max(page, 1) - 1) * ARCHIVE_PAGE}
    if dubbed:
        body["dubbed"] = 1
    if media_type:
        body["type"] = TYPES.get(media_type.lower(), media_type)

    r = _post_archive(body)
    if not r.ok:
        logger.error("Archive search failed with status %d", r.status_code)
        raise RuntimeError(f"AnimeUnity search failed: HTTP {r.status_code}")

    try:
        payload = r.json()
    except Exception as e:
        logger.error("Error parsing archive response: %s", e)
        raise
    records = payload if isinstance(payload, list) else payload.get("records", [])

    # No rows is an answer, not a failure — the same one StreamingCommunity
    # gives. This used to raise, which reached the panel as a red error box
    # saying the search had broken when the query simply matched nothing.
    return _normalize_titles(records)


def get_episodes(anime_id: str) -> list[dict]:
    """
    Fetch all episodes for an anime using the AnimeUnity info_api.
    Uses BATCH_SIZE-chunked requests to handle long series.
    Returns list of {id, number} dicts.
    """
    scraper = _get_scraper()
    host = ANIMEUNITY_HOST

    r = scraper.get(f"https://{host}/info_api/{anime_id}", timeout=15)
    r.raise_for_status()
    episodes_count = r.json().get("episodes_count", 0)

    if not episodes_count:
        return []

    episodes = []
    for start in range(0, episodes_count + 1, BATCH_SIZE):
        end = min(start + BATCH_SIZE - 1, episodes_count)
        batch_r = scraper.get(
            f"https://{host}/info_api/{anime_id}/0",
            params={"start_range": start, "end_range": end},
            timeout=15,
        )
        if batch_r.ok:
            episodes.extend(batch_r.json().get("episodes", []))

    return episodes


def _get_embed_content(episode_id) -> tuple[str, str]:
    """
    Fetch the vixcloud.co embed page for an episode.
    Returns (script_text, embed_url) — same shape as film._get_iframe().
    """
    scraper = _get_scraper()
    host = ANIMEUNITY_HOST

    # AnimeUnity returns the vixcloud.co embed URL as plain text
    r = scraper.get(f"https://{host}/embed-url/{episode_id}", timeout=15)
    r.raise_for_status()
    embed_url = r.text.strip()

    if not embed_url.startswith("http"):
        raise RuntimeError(f"Unexpected embed-url response: {embed_url[:100]!r}")

    logger.info("Episode %s embed URL: %s", episode_id, embed_url[:80])

    # Fetch the vixcloud.co embed page. vixcloud.co now serves this /embed/ HTML
    # page behind Cloudflare, so plain requests.get returns a 403 challenge page.
    # Use the cloudscraper session to clear the challenge. (The downstream
    # /playlist, /storage/enc.key and CDN segment URLs are NOT Cloudflare-gated
    # and still work with plain requests.)
    req_embed = scraper.get(
        embed_url,
        headers={"Referer": f"https://{host}/"},
        timeout=15,
    )
    req_embed.raise_for_status()

    soup = BeautifulSoup(req_embed.text, "lxml")
    body = soup.find("body")
    if not body:
        raise RuntimeError("Empty embed page body")
    script = body.find("script")
    if not script:
        raise RuntimeError("No script tag found in vixcloud.co embed page")

    logger.info("Embed script (first 400 chars): %s", script.text[:400])
    return script.text, embed_url


def get_episode_languages(episode_id) -> dict:
    """Audio and subtitle languages available on one anime episode."""
    from app.core._shared import _parse_content, _get_m3u8_url
    from app.core.m3u8 import fetch_master_languages

    embed_content, embed_url = _get_embed_content(episode_id)
    json_win_video, json_win_param = _parse_content(embed_content, embed_url)
    return fetch_master_languages(_get_m3u8_url(json_win_video, json_win_param), embed_url)


def download_anime_episode(
    anime_id: str,
    episode: dict,
    anime_name: str,
    anime_type: str = "tv",
    output_dir: str = "videos",
    temp_dir: str = None,
    progress_factory=None,
    cancel_event=None,
    year: str = None,
    audio_languages: list[str] = None,
    subtitle_languages: list[str] = None,
    strict_audio: bool = False,
) -> str:
    """
    Full download pipeline for a single anime episode.
    - Series (anime_type="tv"): videos/AnimeName/Season 01/AnimeName S01E01.mp4
    - Movies (anime_type="movie"): videos/AnimeName (YYYY)/AnimeName.mp4
    """
    audio_languages = audio_languages or ["ita"]
    subtitle_languages = subtitle_languages or []

    from app.core.film import _collect_audio_tracks, _collect_subtitle_tracks
    from app.core._shared import _parse_content, _get_m3u8_url, _get_m3u8_key
    from app.core.m3u8 import download_m3u8
    from app.core.paths import anime_path

    episode_id = episode["id"]
    episode_number = str(episode.get("number", "0"))

    embed_content, embed_url = _get_embed_content(episode_id)
    json_win_video, json_win_param = _parse_content(embed_content, embed_url)

    logger.info(
        "Anime episode %s — video_id=%s token=%.8s...",
        episode_number, json_win_video.get("id"), json_win_param.get("token", ""),
    )

    m3u8_url = _get_m3u8_url(json_win_video, json_win_param)
    m3u8_key = _get_m3u8_key(json_win_video, json_win_param, embed_url)

    audio_track_urls = _collect_audio_tracks(
        m3u8_url, embed_url, audio_languages, strict=strict_audio
    )
    subtitle_track_urls = _collect_subtitle_tracks(m3u8_url, embed_url, subtitle_languages)

    mp4_path = anime_path(output_dir, anime_name, episode_number, anime_type, year)

    final_path = download_m3u8(
        m3u8_index=m3u8_url,
        key=m3u8_key,
        output_filename=mp4_path,
        temp_dir=temp_dir,
        progress_factory=progress_factory,
        referer=embed_url,
        cancel_event=cancel_event,
        audio_languages=audio_languages,
        subtitle_languages=subtitle_languages,
        audio_track_urls=audio_track_urls,
        subtitle_track_urls=subtitle_track_urls,
    )

    # download_m3u8 returns the real output path (e.g. .mkv after remux)
    return final_path or mp4_path
