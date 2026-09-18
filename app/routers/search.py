import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.deps import require
from app.auth.permissions import Permission
from app.config import configured_domain
from app.core.page import search as core_search
from app.core.film import get_film_languages
from app.core.tv import get_tv_languages
from app.core import animeunity

logger = logging.getLogger(__name__)

# Searching is the entry point to both flows, so either privilege grants it.
# The language endpoint is part of the same flow: a requester has to see the
# real audio and subtitle tracks before choosing them.
router = APIRouter(
    prefix="/api/search",
    tags=["search"],
    dependencies=[Depends(require(Permission.REQUEST, Permission.DOWNLOAD, mode="or"))],
)


def _domain() -> str:
    domain = configured_domain()
    if not domain:
        raise HTTPException(status_code=409, detail="Nessun dominio configurato")
    return domain


@router.get("")
async def search(
    q: str = Query(..., min_length=1, max_length=200),
    # Constrained rather than free text: an unrecognised value used to fall
    # through to StreamingCommunity in silence, and a typo answering with films
    # is worse than a refusal.
    source: str = Query(default="streamingcommunity",
                        pattern="^(streamingcommunity|animeunity)$"),
    # 1-based, and the same number for both sources even though their pages are
    # different sizes (60 and 30): the caller asks for "the next lot" and each
    # source works out what that means. Bounded so nobody asks the source for a
    # preposterous row offset.
    page: int = Query(default=1, ge=1, le=50),
    # One canonical vocabulary on the wire, lower case; the mapping to
    # AnimeUnity's own capitalisation lives in animeunity.TYPES.
    media_type: str | None = Query(default=None, pattern="^(movie|tv|ova|ona|special)$"),
    dubbed: bool = Query(default=False),
):
    # One pattern cannot express two vocabularies, so the rest is checked here.
    # A false `dubbed` is the default and must not be refused — only a caller
    # actually asking StreamingCommunity for a filter it does not have.
    if source == "streamingcommunity":
        if media_type and media_type not in ("movie", "tv"):
            raise HTTPException(
                status_code=422,
                detail=f"Tipo '{media_type}' non valido per StreamingCommunity")
        if dubbed:
            raise HTTPException(
                status_code=422,
                detail="Il filtro doppiaggio vale solo per AnimeUnity")

    try:
        if source == "animeunity":
            results = await asyncio.to_thread(
                animeunity.search, q, page=page, media_type=media_type, dubbed=dubbed)
        else:
            results = await asyncio.to_thread(
                core_search, q, _domain(), page=page, media_type=media_type)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Search error")
        raise HTTPException(status_code=502, detail=str(e))
    return results


@router.get("/languages/{title_id}")
async def title_languages(
    title_id: int,
    type: str = Query(..., pattern="^(movie|tv)$"),
    slug: str = Query(default=None),
    version: str = Query(default=""),
):
    domain = _domain()
    try:
        if type == "movie":
            langs = await asyncio.to_thread(get_film_languages, title_id, domain)
        else:
            if not slug:
                raise ValueError("slug is required for tv type")
            langs = await asyncio.to_thread(get_tv_languages, title_id, slug, domain, version)
    except Exception as e:
        logger.warning("Languages fetch error for %s %d: %s", type, title_id, e)
        raise HTTPException(status_code=502, detail=str(e))
    return langs
