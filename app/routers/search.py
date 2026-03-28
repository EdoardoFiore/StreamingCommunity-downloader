import logging

from fastapi import APIRouter, HTTPException, Query

from app.core.page import search as core_search

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
