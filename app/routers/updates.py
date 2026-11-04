import logging
import os
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from app import db, updater_client, updates
from app.auth.deps import require
from app.auth.permissions import Permission
from app.jobs import job_manager

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/updates",
    tags=["updates"],
    # Whoever cannot act on a new version has no use for the badge.
    dependencies=[Depends(require(Permission.MANAGE_SETTINGS))],
)

# Held in memory, so a restart loses them. A scheduled job is not here: it is
# stored, and comes back.
_LOST_IN_A_RESTART = ("running", "queued", "paused")


def _report(info: dict) -> dict:
    return {
        **info,
        # Baked into the image. A source checkout has no container to replace,
        # so its instructions differ.
        "install": os.getenv("SCPANEL_INSTALL", "source"),
        "updater": updater_client.status(),
    }


def _backup_database() -> None:
    """Migrations only go forward, and the new version runs them on boot. One
    copy, overwritten each time: this is a way back from an update, not a backup
    strategy."""
    target = sqlite3.connect(f"{db.current_path()}.pre-update")
    try:
        db.get_conn().backup(target)
    finally:
        target.close()


@router.get("")
def get_status():
    return _report(updates.status())


@router.post("/check")
def check_now():
    return _report(updates.check_now())


@router.post("/install", status_code=202)
def install():
    # In open mode this is reachable by every visitor, who must not be able to
    # restart the panel at will or cut a download short: with nothing new there
    # is nothing to do, and a busy panel refuses.
    if not updates.status()["update_available"]:
        raise HTTPException(status_code=409, detail="Nessuna nuova versione da installare.")
    busy = sum(job["status"] in _LOST_IN_A_RESTART for job in job_manager.list_jobs())
    if busy:
        raise HTTPException(
            status_code=409,
            detail=f"Ci sono {busy} download in corso o in attesa: l'aggiornamento riavvia il "
                   "pannello e li interromperebbe. Attendi che finiscano o annullali.",
        )

    updater = updater_client.status()
    if updater is None:
        raise HTTPException(status_code=503, detail="Il servizio updater non è configurato.")
    if not updater["ready"]:
        raise HTTPException(status_code=503, detail=updater["problem"])

    try:
        _backup_database()
    except (sqlite3.Error, OSError):
        logger.exception("Database backup before the update failed")
        raise HTTPException(
            status_code=500,
            detail="Impossibile salvare una copia del database: aggiornamento annullato.",
        )

    try:
        status, payload = updater_client.start()
    except updater_client.UpdaterUnavailable:
        raise HTTPException(status_code=503, detail="Il servizio updater non risponde.")
    if status != 202:
        raise HTTPException(status_code=status, detail=payload.get("error", "Aggiornamento rifiutato."))
    return payload
