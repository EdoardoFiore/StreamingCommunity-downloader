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


def _mark_in_library(episodes: list[dict], title: str, season: int, year: str,
                     tv_id=None) -> None:
    """Flag the episodes already sitting in the library.

    The title arrives from the caller because the episode list has no other
    way to know it, and it is the same value the caller would post to start
    the download. It reaches the filesystem only through
    ``paths.episode_path``, which runs it through ``sanitize_filename``: "/",
    "\\", control characters and leading dots are stripped, so a crafted title
    cannot walk out of the library directory. This only ever stats.

    ``tv_id`` finds the folders associated with this series in the library
    registry (``app.library``), wherever and under whatever name they are, and
    the ones a person has said are not its own.
    """
    from app import library
    from app.core import container
    from app.requests.resolver import (
        EPISODE, episode_candidates, first_existing, library_dir, series_years,
    )

    output_dir = library_dir(EPISODE)
    source = library.SOURCE_BY_KIND[EPISODE]
    # Read once per season rather than per episode: the registry, the series'
    # folders under other years (#24), and each associated folder's listing,
    # which Held reads once and keeps.
    cont = container.configured()
    try:
        held = library.held(source, EPISODE, tv_id, output_dir) if tv_id is not None else []
        rejected = library.rejected_folders(source, EPISODE, tv_id) if tv_id is not None else set()
    except Exception:
        logger.exception("Cannot read the associated folders of series %s", tv_id)
        held, rejected = [], set()
    try:
        years = series_years(output_dir, title, year, exclude=rejected)
    except Exception:
        logger.exception("Cannot list the series folders for %s", title)
        years = [year or None]
    for episode in episodes:
        try:
            bases = episode_candidates(output_dir, title, season, episode["n"], years,
                                       container=cont)
            episode["in_library"] = (
                first_existing(*bases) is not None
                or library.locate(held, EPISODE, season=season, episode=episode["n"]) is not None
            )
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
        await asyncio.to_thread(_mark_in_library, episodes, title, season, year, tv_id)
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
