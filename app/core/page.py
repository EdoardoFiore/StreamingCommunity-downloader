import json
import logging

import requests
from bs4 import BeautifulSoup

from app.core.headers import get_headers

logger = logging.getLogger(__name__)


def get_domain_version(domain: str) -> str:
    """Verify domain is reachable and return site version string."""
    site_url = f"https://{domain}"
    try:
        response = requests.get(site_url, headers={"user-agent": get_headers()}, timeout=10)
        response.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"Cannot reach {domain}: {e}")

    try:
        soup = BeautifulSoup(response.text, "lxml")
        app_div = soup.find("div", {"id": "app"})
        if app_div and app_div.get("data-page"):
            return json.loads(app_div.get("data-page")).get("version", "")
    except Exception:
        pass
    return ""


def search(title_search: str, domain: str) -> list[dict]:
    req = requests.get(
        f"https://{domain}/api/search",
        params={"q": title_search},
        headers={"user-agent": get_headers()},
    )
    if not req.ok:
        raise RuntimeError(f"Search failed: HTTP {req.status_code}")
    try:
        data = req.json()
    except Exception:
        raise RuntimeError(f"Search returned invalid response (body: {req.text[:200]!r})")
    def _poster(images):
        for img in (images or []):
            if img.get("type") == "poster":
                return img.get("filename")
        return None

    return [
        {
            "name": t["name"],
            "type": t["type"],
            "id": t["id"],
            "slug": t["slug"],
            "score": t.get("score"),
            "release_date": t.get("release_date"),
            "last_air_date": t.get("last_air_date"),
            "age": t.get("age"),
            "seasons_count": t.get("seasons_count", 0),
            "poster": _poster(t.get("images", [])),
        }
        for t in data.get("data", [])
    ][:21]
