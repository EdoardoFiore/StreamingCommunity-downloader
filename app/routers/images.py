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

# TMDB serves artwork from one host at a fixed set of widths. Both are pinned
# here rather than taken from the path: the size is part of the upstream URL and
# an unbounded one would let a caller ask TMDB for anything.
TMDB_IMAGE_HOST = "https://image.tmdb.org/t/p"
TMDB_SIZES = frozenset({"w300", "w500", "w780", "w1280", "original"})
TMDB_PATH = re.compile(r"^[A-Za-z0-9._-]{1,120}\.(jpg|png|svg)$")


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


def _fetch_tmdb_image(size: str, path: str) -> tuple[bytes, str]:
    url = f"{TMDB_IMAGE_HOST}/{size}/{path}"
    try:
        res = requests.get(url, headers={"user-agent": get_headers()}, timeout=10)
    except Exception as e:
        logger.warning("TMDB image proxy error for %s: %s", url, e)
        raise HTTPException(status_code=502, detail="Image not available")

    content_type = res.headers.get("content-type", "")
    if not (res.ok and content_type.startswith("image/")):
        logger.warning("TMDB image miss: %s -> HTTP %d ct=%r", url, res.status_code, content_type)
        raise HTTPException(status_code=res.status_code, detail="Image not found")
    return res.content, content_type


# Declared BEFORE the catch-all below, which would otherwise swallow it:
# "tmdb/w1280/abc.jpg" satisfies SAFE_FILENAME, so the request would be
# answered — by asking the source's CDN for a file it has never heard of.
@router.get("/api/image/tmdb/{size}/{path}")
async def proxy_tmdb_image(size: str, path: str):
    """Artwork from TMDB, through the panel.

    Nothing forces this: there is no CSP here today, so the browser could fetch
    image.tmdb.org directly. It goes through the panel anyway to keep the
    property the source proxy already has — a page in this panel talks to this
    panel and to nowhere else — and so that adding `img-src 'self'` later does
    not silently blank every backdrop.
    """
    if size not in TMDB_SIZES or not TMDB_PATH.match(path):
        raise HTTPException(status_code=400, detail="Immagine non valida")

    content, content_type = await asyncio.to_thread(_fetch_tmdb_image, size, path)
    return Response(
        content=content,
        media_type=content_type,
        # Artwork at a given path never changes; TMDB publishes a new path
        # instead.
        headers={"Cache-Control": "public, max-age=86400"},
    )


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
