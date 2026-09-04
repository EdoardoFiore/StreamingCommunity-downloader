import asyncio
import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app import config
from app.auth.deps import require
from app.auth.permissions import Permission
from app.config import get_settings, save_settings
from app.core import domain_recovery, naming
from app.core.paths import validate_library_path
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


class DomainUpdate(BaseModel):
    domain: str


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

    if not valid and get_settings().get("domain_auto_check_enabled", True):
        # The domain is dead, so start looking for its replacement now rather
        # than waiting up to six hours for the periodic check. This fires on
        # every page load of every user while the panel is broken, which is
        # exactly what the throttle inside run_check() is there to absorb.
        asyncio.create_task(asyncio.to_thread(domain_recovery.run_check))

    return {"domain": domain, "valid": valid, "version": version}


@router.get("/candidate", dependencies=CAN_READ_DOMAIN)
def get_domain_candidate():
    """The replacement domain waiting to be confirmed, if there is one.

    Readable by anyone who can read the domain: a hostname is not a secret, and
    a requester staring at a broken search benefits from being told the source
    has moved even though they cannot be the one to apply it.
    """
    return {"candidate": domain_recovery.pending()}


@router.post("/check", dependencies=CAN_MANAGE)
async def check_domain_now():
    return await asyncio.to_thread(domain_recovery.run_check, True)


@router.post("/candidate/apply", dependencies=CAN_MANAGE)
async def apply_domain_candidate(body: DomainUpdate):
    """Adopt the pending candidate.

    The domain in the body is a confirmation token, not an instruction: it must
    match what the server already found, and the value written is the server's
    own. Taking the host from the request would be exactly the "the source
    domain never comes from the client" rule this endpoint looks like it breaks.
    """
    candidate = domain_recovery.pending()
    if not candidate:
        raise HTTPException(status_code=409, detail="Nessun dominio proposto")
    if body.domain.strip() != candidate["host"]:
        raise HTTPException(
            status_code=409,
            detail="Il dominio proposto è cambiato: ricarica la pagina",
        )

    try:
        version = await asyncio.to_thread(domain_recovery.apply_candidate, candidate["host"])
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"domain": candidate["host"], "version": version, "valid": True}


@router.post("/candidate/dismiss", dependencies=CAN_MANAGE)
def dismiss_domain_candidate():
    domain_recovery.clear_pending()
    return {"ok": True}


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
        # Rejected here rather than at download time. A host path saved on a
        # Linux deployment is accepted by every layer below — a backslash is a
        # legal filename character there — and only surfaces once FFmpeg is
        # handed the name at the end of a finished download.
        try:
            path = validate_library_path(lib.path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        seen[lib.type] = {"type": lib.type, "path": path}
    _update_data({
        "libraries": list(seen.values()),
        "excluded_folders": body.excluded_folders,
    })
    return {"ok": True}


class SettingsUpdate(BaseModel):
    """Every field optional: each settings section sends only what it owns.

    The merge in set_app_settings fills in the rest from what is stored, so the
    performance section and the recovery section can save independently without
    either one having to re-send the other's values and risk overwriting a
    change made seconds earlier in the same modal.
    """

    max_concurrent_downloads: int | None = None
    max_segment_workers: int | None = None
    series_watch_interval_minutes: int | None = None
    naming_templates: dict[str, str] | None = None
    domain_auto_check_enabled: bool | None = None
    domain_auto_apply: bool | None = None
    domain_check_interval_minutes: int | None = None

    @field_validator("naming_templates")
    @classmethod
    def _check_templates(cls, value):
        """Refuse at save time what render() cannot afford to complain about.

        render() runs inside a download, after the bytes are already fetched, so
        it silently defaults rather than raising. Everything it would paper over
        has to be caught here, while there is still a person looking at it.
        """
        if value is None:
            return None
        return {slot: naming.validate(slot, template) for slot, template in value.items()}


class NamingPreviewRequest(BaseModel):
    templates: dict[str, str]


@router.post("/settings/naming-preview", dependencies=CAN_MANAGE)
def preview_naming(body: NamingPreviewRequest):
    """What each template would produce, rendered by the real engine.

    Server-side rather than reimplemented in JavaScript: two renderers drift,
    and this way an invalid template shows its actual validation error while it
    is being typed instead of at save time.
    """
    result = {}
    for slot, template in body.templates.items():
        if slot not in naming.SLOT_TOKENS:
            continue
        try:
            naming.validate(slot, template)
            result[slot] = {"preview": naming.preview(slot, template), "error": None}
        except ValueError as exc:
            result[slot] = {"preview": None, "error": str(exc)}
    return {"slots": result, "tokens": {s: list(t) for s, t in naming.SLOT_TOKENS.items()}}


@router.get("/settings/naming-defaults", dependencies=CAN_MANAGE)
def naming_defaults():
    return {"templates": dict(naming.DEFAULT_TEMPLATES),
            "tokens": {s: list(t) for s, t in naming.SLOT_TOKENS.items()}}


@router.get("/settings", dependencies=CAN_MANAGE)
def get_app_settings():
    # naming_templates is always returned complete, defaults filled in: the UI
    # renders nine fields and a partially stored dict would leave some blank,
    # which reads as "no rule" rather than "the default rule".
    return {**get_settings(), "naming_templates": naming.templates()}


# Range checks, as (field, low, high). A settings key with no sensible bounds
# is simply absent here.
_SETTING_RANGES = (
    ("max_concurrent_downloads", 1, 32),
    ("max_segment_workers", 1, 128),
    ("series_watch_interval_minutes", 15, 1440),
    # Floored well above the throttle in domain_recovery: a check any more often
    # than this is hammering somebody else's page for a domain that rotates
    # every few weeks.
    ("domain_check_interval_minutes", 30, 1440),
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
