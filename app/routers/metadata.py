"""Metadata for the detail view, and the TMDB key that improves it.

Separate from the domain router on purpose: that one owns data.json, this one
owns a credential in panel.db, and keeping them apart makes it obvious which
kind of setting a given endpoint is touching.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel

from app.auth import models as auth_models
from app.auth.deps import require
from app.auth.permissions import Permission
from app.core import metadata

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/metadata", tags=["metadata"])

CAN_MANAGE = [Depends(require(Permission.MANAGE_SETTINGS))]

# The same gate as search: this is the detail view of a search result.
CAN_READ = [Depends(require(Permission.REQUEST, Permission.DOWNLOAD, mode="or"))]


class TmdbSettingsUpdate(BaseModel):
    tmdb_api_key: str


@router.get("/settings", dependencies=CAN_MANAGE)
def get_metadata_settings():
    """Whether a key is set — never the key itself, as with the Jellyfin one."""
    return {"tmdb_configured": metadata.tmdb_api_key() is not None}


@router.put("/settings", dependencies=CAN_MANAGE)
def set_metadata_settings(body: TmdbSettingsUpdate):
    value = body.tmdb_api_key.strip()
    auth_models.set_setting(auth_models.SETTING_TMDB_API_KEY, value)
    # Entries cached under the old key-configured flag would otherwise keep
    # serving the keyless answer for six hours after a key was added.
    metadata.clear_cache()
    return {"tmdb_configured": bool(value)}


@router.get("/{media_type}/{title_id}", dependencies=CAN_READ)
async def get_title_metadata(
    media_type: str = Path(pattern="^(movie|tv)$"),
    title_id: str = Path(min_length=1, max_length=64),
    slug: str = Query("", max_length=200),
    version: str = Query("", max_length=200),
):
    """Plot, genres, artwork and a trailer, from whichever provider answered.

    Deliberately never 502s on a metadata miss: a detail modal with no plot is a
    modal with no plot. Returning an error here would turn "the source has no
    description for this film" into something the frontend has to handle as a
    failure, and the previous behaviour — no description at all — was already
    fine.
    """
    if not title_id.isdigit():
        raise HTTPException(status_code=400, detail="Identificativo non valido")

    try:
        return await asyncio.to_thread(
            metadata.title_metadata, media_type, title_id, slug, version
        )
    except Exception:
        logger.exception("Metadata lookup failed for %s/%s", media_type, title_id)
        return dict(metadata.EMPTY)
