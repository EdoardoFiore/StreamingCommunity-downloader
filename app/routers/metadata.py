"""Metadata for the detail view.

One endpoint: everything comes from the title page's own props, so there is
nothing to configure and no credential to hold.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.auth.deps import require
from app.auth.permissions import Permission
from app.core import metadata

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/metadata", tags=["metadata"])

# The same gate as search: this is the detail view of a search result.
CAN_READ = [Depends(require(Permission.REQUEST, Permission.DOWNLOAD, mode="or"))]


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
