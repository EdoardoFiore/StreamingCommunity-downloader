import json
import logging
import os
import re

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

ANIMEUNITY_HOST = os.getenv("ANIMEUNITY_HOST", "www.animeunity.so")
BATCH_SIZE = 120

_scraper = None


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


def _normalize_titles(titles: list) -> list[dict]:
    """Convert raw AnimeUnity title dicts to the normalized format used by the frontend."""
    results = []
    seen_ids = set()
    for t in titles:
        # Handle both old format (title/name) and new livesearch format (title_eng)
        title_str = (
            t.get("title_eng") or t.get("title") or t.get("name") or ""
        ).strip()
        slug = t.get("slug", "")
        id_num = t.get("id", "")
        anime_id = f"{id_num}-{slug}" if slug else str(id_num)
        if not title_str or anime_id in seen_ids:
            continue
        seen_ids.add(anime_id)
        poster = (
            t.get("imageurl") or t.get("cover") or
            t.get("poster") or t.get("image") or ""
        )
        results.append({
            "id": anime_id,
            "name": title_str,
            "type": "anime",
            "slug": slug,
            "poster": poster,
            "episodes_count": t.get("episodes_count", 0),
            "score": t.get("score") or t.get("vote"),
            "release_date": t.get("date") or t.get("release_date") or "",
        })
        if len(results) >= 21:
            break
    return results


def search(query: str) -> list[dict]:
    """
    Search for anime on AnimeUnity using the /livesearch endpoint.
    Handles CSRF token requirements for Laravel protection.
    """
    scraper = _get_scraper()
    host = ANIMEUNITY_HOST

    # Initial GET to establish session and get CSRF token
    r_home = scraper.get(f"https://{host}/", timeout=15)
    
    # Extract CSRF token from meta tag if present
    csrf_token = None
    try:
        soup = BeautifulSoup(r_home.text, "lxml")
        csrf_meta = soup.find("meta", {"name": "csrf-token"})
        if csrf_meta:
            csrf_token = csrf_meta.get("content", "")
            logger.debug("CSRF token found: %s", csrf_token[:20] if csrf_token else "None")
    except Exception as e:
        logger.debug("Error extracting CSRF token: %s", e)

    # Standard AJAX headers with browser mimicry
    headers_ajax = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://{host}/archivio",
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
    
    # Add CSRF token if available
    if csrf_token:
        headers_ajax["X-CSRF-TOKEN"] = csrf_token

    # Use the /livesearch endpoint (POST with {title: query})
    r = scraper.post(
        f"https://{host}/livesearch",
        json={"title": query},
        headers=headers_ajax,
        timeout=15,
    )

    if not r.ok:
        logger.error("Livesearch failed with status %d. CSRF token was: %s", 
                    r.status_code, "present" if csrf_token else "missing")
        raise RuntimeError(f"AnimeUnity search failed: HTTP {r.status_code}")

    try:
        data = r.json()
        records = data.get("records", [])
        if records:
            result = _normalize_titles(records)
            if result:
                return result
    except Exception as e:
        logger.error("Error parsing livesearch response: %s", e)
        raise

    raise RuntimeError(f"AnimeUnity search: no results found for '{query}'")


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

    # Fetch the vixcloud.co embed page (no Cloudflare on vixcloud.co)
    req_embed = requests.get(
        embed_url,
        headers={
            "User-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": f"https://{host}/",
        },
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
) -> str:
    """
    Full download pipeline for a single anime episode.
    - Series (anime_type="tv"): videos/AnimeName/Season 01/AnimeName S01E01.mp4
    - Movies (anime_type="movie"): videos/AnimeName (YYYY)/AnimeName.mp4
    Reuses vixcloud.co parsing and m3u8 download logic from film.py / m3u8.py.
    """
    # Import private helpers from film.py — same vixcloud.co infrastructure
    from app.core.film import _parse_content, _get_m3u8_url, _get_m3u8_key, _get_m3u8_audio
    from app.core.m3u8 import download_m3u8

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
    m3u8_audio = _get_m3u8_audio(json_win_video, json_win_param, embed_url)

    clean_name = anime_name.replace("+", " ").replace(",", "")
    
    # Determine if it's a TV series or a movie
    is_series = anime_type.lower() in ("tv", "serie", "series", "anime")
    
    if is_series:
        # TV series: follows same pattern as StreamingCommunity TV
        # videos/AnimeName/Season 01/AnimeName S01E01.mp4
        try:
            ep_num = int(float(episode_number))
        except (ValueError, TypeError):
            ep_num = 0
        
        season = 1  # For anime, assume season 1 (they typically have one season on AnimeUnity)
        ep_filename = f"{clean_name} S01E{ep_num:02d}.mp4"
        year_str = f" ({year})" if year else ""
        folder_name = f"{clean_name}{year_str}"
        mp4_path = os.path.join(output_dir, folder_name, f"Season {season:02d}", ep_filename)
    else:
        # Movies: save in folder with year for consistency
        # videos/AnimeName (YYYY)/AnimeName.mp4
        year_str = f" ({year})" if year else ""
        folder_name = f"{clean_name}{year_str}"
        mp4_path = os.path.join(output_dir, folder_name, f"{clean_name}.mp4")

    download_m3u8(
        m3u8_index=m3u8_url,
        m3u8_audio=m3u8_audio,
        key=m3u8_key,
        output_filename=mp4_path,
        temp_dir=temp_dir,
        progress_factory=progress_factory,
        referer=embed_url,
        cancel_event=cancel_event,
    )

    return mp4_path
