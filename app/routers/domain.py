import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import DATA_FILE
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
def get_domain():
    data = _read_data()
    domain = data.get("domain", "")
    version = None
    valid = False
    if domain:
        try:
            version = get_domain_version(domain)
            valid = True
        except Exception:
            valid = False
    return {"domain": domain, "valid": valid, "version": version}


class DomainUpdate(BaseModel):
    domain: str


@router.put("")
def set_domain(body: DomainUpdate):
    domain = body.domain.strip()
    if not domain:
        raise HTTPException(status_code=400, detail="Domain cannot be empty")
    try:
        version = get_domain_version(domain)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    data = _read_data()
    data["domain"] = domain
    _write_data(data)
    return {"domain": domain, "version": version, "valid": True}
