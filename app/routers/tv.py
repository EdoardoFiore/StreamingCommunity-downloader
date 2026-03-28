import logging

from fastapi import APIRouter, HTTPException, Query

from app.core.tv import get_info_tv, get_info_season, get_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tv", tags=["tv"])


@router.get("/{tv_id}/token")
def fetch_token(tv_id: int, domain: str = Query(...)):
    try:
        token = get_token(tv_id, domain)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"token": token}


@router.get("/{tv_id}/seasons")
def fetch_seasons(tv_id: int, slug: str = Query(...), domain: str = Query(...), version: str = Query(...)):
    try:
        count = get_info_tv(tv_id, slug, version, domain)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"seasons_count": count}


@router.get("/{tv_id}/seasons/{season}/episodes")
def fetch_episodes(
    tv_id: int,
    season: int,
    slug: str = Query(...),
    domain: str = Query(...),
    version: str = Query(...),
    token: str = Query(...),
):
    try:
        episodes = get_info_season(tv_id, slug, domain, version, token, season)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return episodes
