import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.deps import require
from app.auth.permissions import Permission
from app.config import configured_domain
from app.core.tv import get_info_tv, get_info_season, get_token, get_tv_languages

logger = logging.getLogger(__name__)

# Browsing seasons and episodes precedes both downloading and requesting.
router = APIRouter(
    prefix="/api/tv",
    tags=["tv"],
    dependencies=[Depends(require(Permission.REQUEST, Permission.DOWNLOAD, mode="or"))],
)


def _domain() -> str:
    """The configured source host. Never taken from the caller — see config.py."""
    domain = configured_domain()
    if not domain:
        raise HTTPException(status_code=409, detail="Nessun dominio configurato")
    return domain


@router.get("/{tv_id}/token")
async def fetch_token(tv_id: int):
    try:
        token = await asyncio.to_thread(get_token, tv_id, _domain())
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"token": token}


@router.get("/{tv_id}/seasons")
async def fetch_seasons(tv_id: int, slug: str = Query(...), version: str = Query(...)):
    try:
        count = await asyncio.to_thread(get_info_tv, tv_id, slug, version, _domain())
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"seasons_count": count}


def _mark_in_library(episodes: list[dict], title: str, season: int, year: str) -> None:
    """Flag the episodes already sitting in the library.

    The title arrives from the caller because the episode list has no other
    way to know it, and it is the same value the caller would post to start
    the download. It reaches the filesystem only through
    ``paths.episode_path``, which runs it through ``sanitize_filename``: "/",
    "\\", control characters and leading dots are stripped, so a crafted title
    cannot walk out of the library directory. This only ever stats.
    """
    from app.core import container, naming, paths
    from app.requests.resolver import EPISODE, first_existing, library_dir

    output_dir = library_dir(EPISODE)
    # Resolved once above the loop rather than per path: each episode builds two
    # of them, and every build would otherwise re-read data.json.
    cont = container.configured()
    for episode in episodes:
        try:
            current = paths.episode_path(output_dir, title, season, episode["n"],
                                         year or None, container=cont)
            legacy = paths.episode_path(output_dir, title, season, episode["n"],
                                        year or None, naming.LEGACY_TEMPLATES,
                                        container=cont)
            episode["in_library"] = first_existing(current, legacy) is not None
        except Exception:
            # A library check is a convenience. It must never be the reason an
            # episode list fails to render.
            logger.exception("Library check failed for episode %s", episode.get("n"))
            episode["in_library"] = False


@router.get("/{tv_id}/seasons/{season}/episodes")
async def fetch_episodes(
    tv_id: int,
    season: int,
    slug: str = Query(...),
    version: str = Query(...),
    token: str = Query(...),
    title: str = Query("", max_length=200),
    year: str = Query("", max_length=10),
):
    try:
        episodes = await asyncio.to_thread(
            get_info_season, tv_id, slug, _domain(), version, token, season
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Stats a filesystem that may be an asleep NFS mount, so it goes off the
    # loop like the fetch above. Skipped when the caller sends no title, since
    # there is then nothing to build a path from.
    if title:
        await asyncio.to_thread(_mark_in_library, episodes, title, season, year)
    return episodes


@router.get("/{tv_id}/languages")
async def fetch_tv_languages(
    tv_id: int,
    slug: str = Query(...),
    version: str = Query(...),
):
    try:
        langs = await asyncio.to_thread(get_tv_languages, tv_id, slug, _domain(), version)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return langs
