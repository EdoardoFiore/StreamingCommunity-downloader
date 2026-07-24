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


@router.get("/{tv_id}/seasons/{season}/episodes")
async def fetch_episodes(
    tv_id: int,
    season: int,
    slug: str = Query(...),
    version: str = Query(...),
    token: str = Query(...),
):
    try:
        episodes = await asyncio.to_thread(
            get_info_season, tv_id, slug, _domain(), version, token, season
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
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
