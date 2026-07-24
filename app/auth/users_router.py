"""User import and permission management.

Jellyfin accounts do not get panel access by having a Jellyfin account: they are
imported explicitly here, and the "let anyone in" switch is off by default.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import models, session as sessions
from app.auth.deps import client_ip, current_user, require
from app.auth.jellyfin import JellyfinClient, JellyfinError, is_administrator
from app.auth.permissions import ALL_PERMISSIONS, Permission, from_names, sanitize, to_names

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/users",
    tags=["users"],
    dependencies=[Depends(require(Permission.MANAGE_USERS))],
)


class ImportRequest(BaseModel):
    jellyfin_user_ids: list[str] = Field(min_length=1)
    permissions: int | None = None


class UserUpdate(BaseModel):
    permissions: int | None = None
    enabled: bool | None = None


class AuthSettingsUpdate(BaseModel):
    allow_new_jellyfin_login: bool
    default_permissions: int


def _service_client(request: Request) -> JellyfinClient:
    base_url, api_key = models.jellyfin_config()
    if not base_url or not api_key:
        raise HTTPException(status_code=409, detail="Pannello non ancora configurato")
    return JellyfinClient(base_url, token=api_key, client_ip=client_ip(request))


def _guard_last_administrator(user_id: int, permissions: int | None, enabled: bool | None):
    """Refuse a change that would leave nobody able to manage users."""
    target = models.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Utente non trovato")
    if not target.has(Permission.MANAGE_USERS) or not target.enabled:
        return
    losing = (permissions is not None and not (permissions & int(Permission.MANAGE_USERS))) or (
        enabled is False
    )
    if losing and models.count_users_with(Permission.MANAGE_USERS) <= 1:
        raise HTTPException(
            status_code=409,
            detail="È l'ultimo utente che può gestire gli account: non può essere disabilitato "
                   "né perdere il permesso.",
        )


@router.get("")
def list_panel_users():
    return [user.to_public() for user in models.list_users()]


@router.get("/permissions")
def list_permissions():
    """The permission catalogue, so the UI does not hardcode names or bit values."""
    return {
        "permissions": [
            {"name": p.name, "value": int(p)}
            for p in Permission
            if p is not Permission.NONE
        ],
        "all": int(ALL_PERMISSIONS),
        "default": models.default_permissions(),
    }


@router.get("/settings")
def get_auth_settings():
    return {
        "allow_new_jellyfin_login": models.allow_new_jellyfin_login(),
        "default_permissions": models.default_permissions(),
        "default_permission_names": to_names(models.default_permissions()),
    }


@router.put("/settings")
def set_auth_settings(body: AuthSettingsUpdate):
    models.set_setting(
        models.SETTING_ALLOW_NEW_LOGIN, "1" if body.allow_new_jellyfin_login else "0"
    )
    models.set_setting(
        models.SETTING_DEFAULT_PERMISSIONS, str(sanitize(body.default_permissions))
    )
    if body.allow_new_jellyfin_login:
        logger.warning(
            "Open sign-in enabled: any Jellyfin account on the server can now use the panel"
        )
    return get_auth_settings()


@router.get("/jellyfin")
async def list_jellyfin_users(request: Request):
    """Accounts on the Jellyfin server, flagged with whether they are imported."""
    client = _service_client(request)
    try:
        jellyfin_users = await asyncio.to_thread(client.users)
    except JellyfinError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    imported = {user.jellyfin_user_id: user for user in models.list_users()}
    result = []
    for jellyfin_user in jellyfin_users:
        panel_user = imported.get(jellyfin_user["Id"])
        result.append({
            "jellyfin_user_id": jellyfin_user["Id"],
            "username": jellyfin_user.get("Name", ""),
            "is_jellyfin_admin": is_administrator(jellyfin_user),
            "disabled_on_jellyfin": bool(jellyfin_user.get("Policy", {}).get("IsDisabled")),
            "imported": panel_user is not None,
            "panel_user": panel_user.to_public() if panel_user else None,
        })
    return result


@router.post("/import", status_code=201)
async def import_users(body: ImportRequest, request: Request):
    permissions = (
        sanitize(body.permissions)
        if body.permissions is not None
        else models.default_permissions()
    )

    client = _service_client(request)
    try:
        jellyfin_users = await asyncio.to_thread(client.users)
    except JellyfinError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # Names and admin flags are taken from Jellyfin, never from the request body.
    by_id = {user["Id"]: user for user in jellyfin_users}
    unknown = [i for i in body.jellyfin_user_ids if i not in by_id]
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"Utenti non presenti su Jellyfin: {', '.join(unknown)}"
        )

    created, skipped = [], []
    for jellyfin_user_id in body.jellyfin_user_ids:
        if models.get_user_by_jellyfin_id(jellyfin_user_id) is not None:
            skipped.append(jellyfin_user_id)
            continue
        jellyfin_user = by_id[jellyfin_user_id]
        user = models.create_user(
            jellyfin_user_id=jellyfin_user_id,
            username=jellyfin_user.get("Name", jellyfin_user_id),
            permissions=permissions,
            is_jellyfin_admin=is_administrator(jellyfin_user),
        )
        created.append(user.to_public())
        logger.info("Imported Jellyfin user %s with permissions %s",
                    user.username, to_names(permissions))

    return {"imported": created, "already_present": skipped}


@router.patch("/{user_id}")
def update_user(user_id: int, body: UserUpdate, request: Request):
    _guard_last_administrator(user_id, body.permissions, body.enabled)

    if body.permissions is not None:
        models.set_permissions(user_id, body.permissions)
        # Permission changes take effect on the next request: the middleware
        # reads them from the row, not from the session.
        logger.info("Permissions for user %s set to %s",
                    user_id, to_names(sanitize(body.permissions)))
    if body.enabled is not None:
        # Disabling also drops the user's sessions, inside the same transaction.
        models.set_enabled(user_id, body.enabled)
        logger.info("User %s %s", user_id, "enabled" if body.enabled else "disabled")

    return models.get_user(user_id).to_public()


@router.post("/{user_id}/sessions/revoke")
def revoke_sessions(user_id: int):
    """Sign a user out everywhere without disabling their account."""
    if models.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="Utente non trovato")
    sessions.delete_user_sessions(user_id)
    return {"ok": True}
