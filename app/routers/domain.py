import asyncio
import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import config
from app.auth.deps import require
from app.auth.permissions import Permission
from app.config import get_settings, save_settings
from app.core.page import get_domain_version

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/domain", tags=["domain"])

CAN_MANAGE = [Depends(require(Permission.MANAGE_SETTINGS))]

# Reading the current domain is not a settings operation: every signed-in user
# needs it to build poster URLs. Anyone who can search can already see it.
CAN_READ_DOMAIN = [
    Depends(require(Permission.REQUEST, Permission.DOWNLOAD, Permission.MANAGE_SETTINGS, mode="or"))
]


def _read_data() -> dict:
    # config.DATA_FILE is resolved at call time, not bound at import: the tests
    # point it at a temp file, and a stale binding here would have this module
    # reading one file while app.config.save_settings wrote another.
    try:
        with open(config.DATA_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"domain": ""}


_update_data = config.update_data


@router.get("", dependencies=CAN_READ_DOMAIN)
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


@router.get("/libraries", dependencies=CAN_MANAGE)
def get_libraries():
    data = _read_data()
    return {
        "libraries": data.get("libraries", []),
        "excluded_folders": data.get("excluded_folders", []),
    }


@router.put("/libraries", dependencies=CAN_MANAGE)
def set_libraries(body: LibrariesUpdate):
    # Deduplicate: last entry per type wins
    seen: dict[str, dict] = {}
    for lib in body.libraries:
        seen[lib.type] = {"type": lib.type, "path": lib.path}
    _update_data({
        "libraries": list(seen.values()),
        "excluded_folders": body.excluded_folders,
    })
    return {"ok": True}


class SettingsUpdate(BaseModel):
    max_concurrent_downloads: int
    max_segment_workers: int
    series_watch_interval_minutes: int | None = None


@router.get("/settings", dependencies=CAN_MANAGE)
def get_app_settings():
    return get_settings()


# Range checks, as (field, low, high). A settings key with no sensible bounds
# is simply absent here.
_SETTING_RANGES = (
    ("max_concurrent_downloads", 1, 32),
    ("max_segment_workers", 1, 128),
    ("series_watch_interval_minutes", 15, 1440),
)


@router.put("/settings", dependencies=CAN_MANAGE)
def set_app_settings(body: SettingsUpdate):
    # save_settings() replaces the whole `settings` dict rather than merging, so
    # every key the caller did not send has to be carried over here or it is
    # lost. Merging over get_settings() makes that structural: a key added to
    # SETTINGS_DEFAULTS later survives a PUT from an older client without
    # anyone having to remember to re-emit it.
    provided = {k: v for k, v in body.model_dump().items() if v is not None}

    for field, low, high in _SETTING_RANGES:
        value = provided.get(field)
        if value is not None and not low <= value <= high:
            raise HTTPException(
                status_code=400, detail=f"{field} must be between {low} and {high}"
            )

    new_settings = {**get_settings(), **provided}
    save_settings(new_settings)
    from app.jobs import job_manager
    job_manager.update_max_concurrent(new_settings["max_concurrent_downloads"])
    return new_settings


@router.put("", dependencies=CAN_MANAGE)
async def set_domain(body: DomainUpdate):
    domain = body.domain.strip()
    if not domain:
        raise HTTPException(status_code=400, detail="Domain cannot be empty")
    try:
        version = await asyncio.to_thread(get_domain_version, domain)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    _update_data({"domain": domain})
    return {"domain": domain, "version": version, "valid": True}
