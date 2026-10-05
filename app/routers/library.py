"""The library registry, for the people who look after the library.

Every route is MANAGE_SETTINGS: an association decides what the panel believes
is already downloaded and, when a person sets one, where the next file goes.
Nothing here touches a file — every write is a row in panel.db — which matters
because open mode grants MANAGE_SETTINGS to every visitor. A folder is only
ever a name picked from the library's own listing, checked again on the way in.
"""

import asyncio
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator

from app import library
from app.auth.deps import require
from app.auth.permissions import Permission

router = APIRouter(
    prefix="/api/library",
    tags=["library"],
    dependencies=[Depends(require(Permission.MANAGE_SETTINGS))],
)

_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,32}$")

# Wide enough for any real layout, narrow enough that a typo is refused rather
# than sending an episode to Season 4000.
_SEASON_OFFSET = (-50, 100)
_EPISODE_OFFSET = (-5000, 5000)


def _check_id(value):
    if value is None:
        return None
    text = str(value).strip()
    if not _ID_RE.match(text):
        raise ValueError("Identificativo non valido")
    return text


def _check_range(value, bounds, label):
    if value is None:
        return None
    if not bounds[0] <= value <= bounds[1]:
        raise ValueError(f"{label} fuori intervallo ({bounds[0]}…{bounds[1]})")
    return value


class AssociationCreate(BaseModel):
    source: str
    media_type: str
    external_id: str
    folder: str
    title: str | None = None
    season_offset: int = 0
    episode_offset: int = 0

    @field_validator("media_type")
    @classmethod
    def _media_type(cls, value):
        if value not in library.MEDIA_TYPES:
            raise ValueError("Tipo non valido")
        return value

    @field_validator("external_id")
    @classmethod
    def _external_id(cls, value):
        return _check_id(value)

    @field_validator("title")
    @classmethod
    def _title(cls, value):
        # Display only: it names the row in a table and reaches no path.
        return (value or "").strip()[:200] or None

    @field_validator("season_offset")
    @classmethod
    def _season_offset(cls, value):
        return _check_range(value, _SEASON_OFFSET, "Spostamento stagione")

    @field_validator("episode_offset")
    @classmethod
    def _episode_offset(cls, value):
        return _check_range(value, _EPISODE_OFFSET, "Spostamento episodi")


class AssociationEdit(BaseModel):
    external_id: str | None = None
    folder: str | None = None
    season_offset: int | None = None
    episode_offset: int | None = None

    @field_validator("external_id")
    @classmethod
    def _external_id(cls, value):
        return _check_id(value)

    @field_validator("season_offset")
    @classmethod
    def _season_offset(cls, value):
        return _check_range(value, _SEASON_OFFSET, "Spostamento stagione")

    @field_validator("episode_offset")
    @classmethod
    def _episode_offset(cls, value):
        return _check_range(value, _EPISODE_OFFSET, "Spostamento episodi")


def _public(associations: list[library.Association]) -> list[dict]:
    roots: dict[str, str] = {}
    out = []
    for a in associations:
        root = roots.setdefault(a.media_type, library.library_root(a.media_type))
        out.append(a.to_public(root))
    return out


@router.get("/associations")
async def list_associations():
    items = await asyncio.to_thread(lambda: _public(library.list_all()))
    return {"associations": items}


@router.get("/associations/title")
async def title_associations(
    source: str = Query(..., max_length=32),
    media_type: str = Query(..., max_length=16),
    external_id: str = Query(..., max_length=32),
):
    """One title's associations, rejected ones left out: the detail page's view."""
    if media_type not in library.MEDIA_TYPES or library.SOURCE_BY_KIND[media_type] != source:
        raise HTTPException(status_code=400, detail="Fonte o tipo non validi")
    items = await asyncio.to_thread(
        lambda: _public(library.for_title(source, media_type, external_id))
    )
    return {"associations": items}


@router.get("/folders")
async def list_folders(media_type: str = Query(..., max_length=16)):
    if media_type not in library.MEDIA_TYPES:
        raise HTTPException(status_code=400, detail="Tipo non valido")
    return {"folders": await asyncio.to_thread(library.library_folders, media_type)}


@router.post("/associations", status_code=201)
async def create_association(body: AssociationCreate):
    if library.SOURCE_BY_KIND[body.media_type] != body.source:
        raise HTTPException(status_code=400, detail="Fonte e tipo non corrispondono")
    try:
        created = await asyncio.to_thread(
            library.associate, body.source, body.media_type, body.external_id, body.folder,
            body.title, body.season_offset, body.episode_offset,
        )
    except library.InvalidFolder as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _public([created])[0]


@router.patch("/associations/{association_id}")
async def edit_association(association_id: int, body: AssociationEdit):
    try:
        edited = await asyncio.to_thread(
            library.edit, association_id, body.external_id, body.folder,
            body.season_offset, body.episode_offset,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except library.InvalidFolder as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _public([edited])[0]


@router.delete("/associations/{association_id}")
async def remove_association(association_id: int):
    """Removing is rejecting: the pair stays, so the panel cannot guess it back."""
    current = await asyncio.to_thread(library.get, association_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Associazione non trovata")
    await asyncio.to_thread(library.reject, association_id)
    return {"ok": True}


@router.post("/associations/{association_id}/restore")
async def restore_association(association_id: int):
    if not await asyncio.to_thread(library.restore, association_id):
        raise HTTPException(status_code=404, detail="Nessuna esclusione da ripristinare")
    return {"ok": True}


@router.post("/reconcile")
async def reconcile():
    """Look for renamed or removed folders now instead of at the next poll."""
    return await asyncio.to_thread(library.reconcile)
