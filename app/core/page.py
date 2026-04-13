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
    session = requests.Session()
    ua = get_headers()

    # Step 1: GET homepage to get cookies and Inertia version
    home = session.get(
        f"https://{domain}",
        headers={"user-agent": ua},
        timeout=10,
    )
    home.raise_for_status()

    # Extract Inertia version from data-page attribute
    inertia_version = ""
    try:
        soup = BeautifulSoup(home.text, "lxml")
        app_div = soup.find("div", {"id": "app"})
        if app_div and app_div.get("data-page"):
            inertia_version = json.loads(app_div.get("data-page")).get("version", "")
    except Exception:
        pass

    # Step 2: Search using Inertia headers + session cookies
    headers = {
        "user-agent": ua,
        "X-Inertia": "true",
        "X-Inertia-Version": inertia_version,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://{domain}",
    }

    req = session.get(
        f"https://{domain}/it/search",
        params={"q": title_search},
        headers=headers,
        timeout=10,
    )

    if not req.ok:
        raise RuntimeError(f"Search failed: HTTP {req.status_code}")

    try:
        data = req.json()
    except Exception:
        raise RuntimeError(f"Search returned invalid response (body: {req.text[:200]!r})")

    # Inertia wraps data in component props
    titles = []
    if "props" in data:
        titles = data["props"].get("titles", data["props"].get("results", []))
    elif "data" in data:
        titles = data.get("data", [])

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
        for t in titles
    ][:21]
