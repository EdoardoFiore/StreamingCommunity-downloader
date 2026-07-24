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


@router.get("/{anime_id}/episodes")
async def get_episodes(anime_id: str):
    """Fetch all episodes for an anime from AnimeUnity info_api."""
    try:
        episodes = await asyncio.to_thread(animeunity.get_episodes, anime_id)
    except Exception as e:
        logger.exception("AnimeUnity episodes error for %s", anime_id)
        raise HTTPException(status_code=502, detail=str(e))
    return episodes
