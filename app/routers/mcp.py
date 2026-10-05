import asyncio

from fastapi import APIRouter, Depends, Request

from app.auth.deps import OPEN_MODE_USER, current_user, require
from app.auth.permissions import Permission
from app.config import get_settings
from app.mcp import auth as mcp_auth
from app.mcp.server import PATH

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

CAN_MANAGE = [Depends(require(Permission.MANAGE_SETTINGS))]


def _owner_name() -> str | None:
    owner = mcp_auth.token_owner()
    if owner is None:
        return None
    return "Pannello" if owner is OPEN_MODE_USER else owner.username


@router.get("/status", dependencies=CAN_MANAGE)
def get_mcp_status():
    """Whether MCP is on and who its token acts as. Never the token itself:
    only its hash is stored, and it is shown once, when generated."""
    return {
        "enabled": bool(get_settings().get("mcp_enabled")),
        "path": PATH,
        "has_token": mcp_auth.has_token(),
        # None with a token present means the owner is gone or disabled, or the
        # token predates the switch to Jellyfin: it is refused until regenerated.
        "owner": _owner_name(),
    }


@router.post("/token", dependencies=CAN_MANAGE)
async def regenerate_mcp_token(request: Request):
    """Mint a new token, owned by the caller. The old one stops working."""
    user = current_user(request)
    token = await asyncio.to_thread(mcp_auth.generate_token, user)
    return {"token": token, "owner": "Pannello" if user is OPEN_MODE_USER else user.username}


@router.delete("/token", dependencies=CAN_MANAGE)
async def revoke_mcp_token():
    await asyncio.to_thread(mcp_auth.revoke_token)
    return {"ok": True}
