import logging

from fastapi import APIRouter, HTTPException, Query

from app.core.page import search as core_search
from app.core.film import get_film_languages
from app.core.tv import get_tv_languages

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("")
def search(q: str = Query(..., min_length=1), domain: str = Query(...)):
    try:
        results = core_search(q, domain)
    except Exception as e:
        logger.exception("Search error")
        raise HTTPException(status_code=502, detail=str(e))
    return results


@router.get("/languages/{title_id}")
def title_languages(
    title_id: int,
    type: str = Query(..., pattern="^(movie|tv)$"),
    domain: str = Query(...),
    slug: str = Query(default=None),
    version: str = Query(default=""),
):
    try:
        if type == "movie":
            langs = get_film_languages(title_id, domain)
        else:
            if not slug:
                raise ValueError("slug is required for tv type")
            langs = get_tv_languages(title_id, slug, domain, version)
    except Exception as e:
        logger.warning("Languages fetch error for %s %d: %s", type, title_id, e)
        raise HTTPException(status_code=502, detail=str(e))
    return langs
