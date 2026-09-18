import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.deps import require
from app.auth.permissions import Permission
from app.config import configured_domain
from app.core import animeunity, home as core_home

logger = logging.getLogger(__name__)

# The start page is the search page before anything is typed, so it is reached
# by whoever may search: either privilege grants it.
router = APIRouter(
    prefix="/api/home",
    tags=["home"],
    dependencies=[Depends(require(Permission.REQUEST, Permission.DOWNLOAD, mode="or"))],
)


@router.get("")
async def home(
    source: str = Query(default="streamingcommunity",
                        pattern="^(streamingcommunity|animeunity)$"),
):
    """The shelves the source itself publishes on its front page."""
    if source == "animeunity":
        # AnimeUnity lives at a fixed host and needs nothing configured, so the
        # refusal below would be a lie there.
        host = animeunity.ANIMEUNITY_HOST
    else:
        host = configured_domain()
        if not host:
            raise HTTPException(status_code=409, detail="Nessun dominio configurato")

    try:
        result = await asyncio.to_thread(core_home.shelves, source, host)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Home shelves error")
        raise HTTPException(status_code=502, detail=str(e))

    return {"source": source, "shelves": result}
