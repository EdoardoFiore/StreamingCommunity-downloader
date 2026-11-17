import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from app.auth.deps import require
from app.auth.permissions import Permission
from app.core import animeunity

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/anime",
    tags=["anime"],
    dependencies=[Depends(require(Permission.REQUEST, Permission.DOWNLOAD, mode="or"))],
)


async def _call(fn, anime_id: str, what: str):
    try:
        return await asyncio.to_thread(fn, anime_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("AnimeUnity %s error for %s", what, anime_id)
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/{anime_id}")
async def get_title(anime_id: str):
    """One anime's record — plot, artwork, languages — and its related titles.

    A search card already carries most of this; a pasted link or a related
    title opened from another page has nothing else to go on.
    """
    return await _call(animeunity.get_title, anime_id, "title")


@router.get("/{anime_id}/episodes")
async def get_episodes(anime_id: str):
    """Every episode of an anime, each with the languages it is published in."""
    return await _call(animeunity.get_episodes, anime_id, "episodes")
