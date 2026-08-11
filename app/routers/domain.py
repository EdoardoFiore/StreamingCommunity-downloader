import asyncio
import json
import logging
import os
import shutil
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.deps import require
from app.auth.permissions import Permission
from app.config import DATA_FILE, get_settings, save_settings
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
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"domain": ""}


def _write_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)


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
    data = _read_data()
    # Deduplicate: last entry per type wins
    seen: dict[str, dict] = {}
    for lib in body.libraries:
        seen[lib.type] = {"type": lib.type, "path": lib.path}
    data["libraries"] = list(seen.values())
    data["excluded_folders"] = body.excluded_folders
    _write_data(data)
    return {"ok": True}


# Opening the settings modal repeatedly should not re-stat a possibly slow NFS
# mount every time. Short enough that a finished download shows up on reopen.
_DISK_USAGE_TTL = 20
_disk_usage_cache: dict = {"at": 0.0, "data": None}


def _compute_disk_usage() -> dict:
    now = time.monotonic()
    if _disk_usage_cache["data"] is not None and now - _disk_usage_cache["at"] < _DISK_USAGE_TTL:
        return _disk_usage_cache["data"]

    data = _read_data()
    # The libraries usually live on one mounted volume (/app/videos in Docker), so
    # stat'ing each one separately would report the same numbers three times and
    # hit the filesystem three times. st_dev collapses them to one real call.
    by_device: dict[int, dict] = {}
    entries = []
    for lib in data.get("libraries", []):
        path = lib.get("path", "")
        try:
            device = os.stat(path).st_dev
            usage = by_device.get(device)
            if usage is None:
                total, used, free = shutil.disk_usage(path)
                usage = {"total": total, "used": used, "free": free}
                by_device[device] = usage
            entries.append({"type": lib.get("type"), "path": path, **usage, "error": None})
        except OSError as e:
            # An unmounted or misconfigured library is an expected failure mode;
            # it must not blank out the volumes that are fine.
            entries.append({
                "type": lib.get("type"), "path": path,
                "total": None, "used": None, "free": None, "error": str(e),
            })

    result = {"libraries": entries}
    _disk_usage_cache["at"] = now
    _disk_usage_cache["data"] = result
    return result


@router.get("/disk-usage", dependencies=CAN_MANAGE)
async def get_disk_usage():
    return await asyncio.to_thread(_compute_disk_usage)


class SettingsUpdate(BaseModel):
    max_concurrent_downloads: int
    max_segment_workers: int
    series_watch_interval_minutes: int | None = None


@router.get("/settings", dependencies=CAN_MANAGE)
def get_app_settings():
    return get_settings()


@router.put("/settings", dependencies=CAN_MANAGE)
def set_app_settings(body: SettingsUpdate):
    if body.max_concurrent_downloads < 1 or body.max_concurrent_downloads > 32:
        raise HTTPException(status_code=400, detail="max_concurrent_downloads must be between 1 and 32")
    if body.max_segment_workers < 1 or body.max_segment_workers > 128:
        raise HTTPException(status_code=400, detail="max_segment_workers must be between 1 and 128")
    new_settings = {
        "max_concurrent_downloads": body.max_concurrent_downloads,
        "max_segment_workers": body.max_segment_workers,
    }
    if body.series_watch_interval_minutes is not None:
        if not 15 <= body.series_watch_interval_minutes <= 1440:
            raise HTTPException(
                status_code=400,
                detail="series_watch_interval_minutes must be between 15 and 1440",
            )
        new_settings["series_watch_interval_minutes"] = body.series_watch_interval_minutes
    else:
        new_settings["series_watch_interval_minutes"] = get_settings()[
            "series_watch_interval_minutes"
        ]
    save_settings(new_settings)
    from app.jobs import job_manager
    job_manager.update_max_concurrent(body.max_concurrent_downloads)
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

    data = _read_data()
    data["domain"] = domain
    _write_data(data)
    return {"domain": domain, "version": version, "valid": True}
