import asyncio
import logging
import re

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from app.auth.deps import require
from app.auth.permissions import Permission
from app.config import configured_domain
from app.core.headers import get_headers

logger = logging.getLogger(__name__)

# Posters accompany search results and the request queue, so approvers need
# them too even when they cannot search.
router = APIRouter(
    dependencies=[
        Depends(
            require(
                Permission.REQUEST,
                Permission.DOWNLOAD,
                Permission.MANAGE_REQUESTS,
                mode="or",
            )
        )
    ]
)

# Poster filenames come from a third-party API and end up in a URL, so they are
# constrained to what a filename can actually be.
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


def _fetch_image(domain: str, filename: str) -> tuple[bytes, str]:
    """Fetch image from CDN or main domain (blocking, runs in thread)."""
    candidates = [
        f"https://cdn.{domain}/images/{filename}",
        f"https://{domain}/images/{filename}",
    ]
    last_status = 404
    for url in candidates:
        try:
            res = requests.get(
                url,
                headers={
                    "user-agent": get_headers(),
                    "referer": f"https://{domain}/",
                },
                timeout=10,
                stream=True,
            )
            content_type = res.headers.get("content-type", "")
            logger.info("Image proxy %s -> HTTP %d ct=%s final=%s",
                        url, res.status_code, content_type, res.url)
            if res.ok and content_type.startswith("image/"):
                return res.content, content_type
            last_status = res.status_code
            logger.warning("Image proxy miss: %s -> HTTP %d ct=%r", url, res.status_code, content_type)
        except Exception as e:
            logger.warning("Image proxy error for %s: %s", url, e)

    raise HTTPException(status_code=last_status, detail="Image not found")


@router.get("/api/image/{filename:path}")
async def proxy_image(filename: str):
    """Proxy images from the streaming site to avoid hotlink protection.

    The host is the configured domain, not one supplied by the caller: this used
    to be an open proxy that would fetch any URL the client asked for.
    """
    domain = configured_domain()
    if not domain:
        raise HTTPException(status_code=409, detail="Nessun dominio configurato")
    if ".." in filename or not SAFE_FILENAME.match(filename):
        raise HTTPException(status_code=400, detail="Nome file non valido")

    content, content_type = await asyncio.to_thread(_fetch_image, domain, filename)
    return Response(content=content, media_type=content_type)
