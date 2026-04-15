import asyncio
import logging

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.core.headers import get_headers

logger = logging.getLogger(__name__)
router = APIRouter()


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


@router.get("/api/image/{domain}/{filename:path}")
async def proxy_image(domain: str, filename: str):
    """Proxy images from the streaming site to avoid hotlink protection."""
    content, content_type = await asyncio.to_thread(_fetch_image, domain, filename)
    return Response(content=content, media_type=content_type)
