import asyncio
import json
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import DATA_FILE, get_settings, save_settings
from app.core.page import get_domain_version

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/domain", tags=["domain"])


def _read_data() -> dict:
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"domain": ""}


def _write_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)


@router.get("")
async def get_domain():
    data = _read_data()
    domain = data.get("domain", "")
    version = None
    valid = False
    if domain:
        try:
            version = await asyncio.to_thread(get_domain_version, domain)
            valid = True
        except Exception:
            valid = False
    return {"domain": domain, "valid": valid, "version": version}


class DomainUpdate(BaseModel):
    domain: str


class LibraryItem(BaseModel):
    type: Literal["film", "tv", "anime"]
    path: str


class LibrariesUpdate(BaseModel):
    libraries: list[LibraryItem]
    excluded_folders: list[str]


@router.get("/libraries")
def get_libraries():
    data = _read_data()
    return {
        "libraries": data.get("libraries", []),
        "excluded_folders": data.get("excluded_folders", []),
    }


@router.put("/libraries")
def set_libraries(body: LibrariesUpdate):
    data = _read_data()
    # Deduplicate: last entry per type wins
    seen: dict[str, dict] = {}
    for lib in body.libraries:
        seen[lib.type] = {"type": lib.type, "path": lib.path}
    data["libraries"] = list(seen.values())
    data["excluded_folders"] = body.excluded_folders
    _write_data(data)
    return {"ok": True}


class SettingsUpdate(BaseModel):
    max_concurrent_downloads: int
    max_segment_workers: int


@router.get("/settings")
def get_app_settings():
    return get_settings()


@router.put("/settings")
def set_app_settings(body: SettingsUpdate):
    if body.max_concurrent_downloads < 1 or body.max_concurrent_downloads > 32:
        raise HTTPException(status_code=400, detail="max_concurrent_downloads must be between 1 and 32")
    if body.max_segment_workers < 1 or body.max_segment_workers > 128:
        raise HTTPException(status_code=400, detail="max_segment_workers must be between 1 and 128")
    new_settings = {
        "max_concurrent_downloads": body.max_concurrent_downloads,
        "max_segment_workers": body.max_segment_workers,
    }
    save_settings(new_settings)
    from app.jobs import job_manager
    job_manager.update_max_concurrent(body.max_concurrent_downloads)
    return new_settings


@router.put("")
async def set_domain(body: DomainUpdate):
    domain = body.domain.strip()
    if not domain:
        raise HTTPException(status_code=400, detail="Domain cannot be empty")
    try:
        version = await asyncio.to_thread(get_domain_version, domain)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    data = _read_data()
    data["domain"] = domain
    _write_data(data)
    return {"domain": domain, "version": version, "valid": True}
