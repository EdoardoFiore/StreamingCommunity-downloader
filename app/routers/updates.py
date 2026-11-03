from fastapi import APIRouter, Depends

from app import updates
from app.auth.deps import require
from app.auth.permissions import Permission

router = APIRouter(
    prefix="/api/updates",
    tags=["updates"],
    # Whoever cannot act on a new version has no use for the badge.
    dependencies=[Depends(require(Permission.MANAGE_SETTINGS))],
)


@router.get("")
def get_status():
    return updates.status()


@router.post("/check")
def check_now():
    return updates.check_now()
