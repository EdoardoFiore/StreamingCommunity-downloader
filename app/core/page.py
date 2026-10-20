import json
import logging
import threading

import requests
from bs4 import BeautifulSoup

from app.core.headers import get_headers

logger = logging.getLogger(__name__)

# The source answers sixty titles to a search page. This used to keep 21 of
# them for no reason anyone recorded, which is most of a search thrown away.
_RESULT_CAP = 60


def _data_page(html: str) -> dict:
    """The Inertia payload rendered into ``<div id="app" data-page="...">``.

    The same JSON the site's XHR API answers, read out of the server-rendered
    page instead — so no asset version, no X-Inertia headers, no session cookie
    and no XSRF token to keep in step with the site.

    BeautifulSoup decodes the attribute's HTML entities itself, which is why
    there is no ``html.unescape()`` here and must not be one: the payload is
    escaped exactly once, and a second pass would turn a plot's ``&amp;amp;``
    into ``&``.

    Raises LookupError when the page carries no payload — kept distinct from
    the ValueError a malformed one raises, because ``get_domain_version``
    answers those two differently.
    """
    app_div = BeautifulSoup(html, "lxml").find("div", {"id": "app"})
    raw = app_div.get("data-page") if app_div else None
    if not raw:
        raise LookupError("the page carries no data-page payload")
    return json.loads(raw)


# The locale segment StreamingCommunity puts in front of its pages. Not every
# host has one: some clones answer 404 to /it/search and serve /search (#34).
_LOCALE = "/it"

# Which form each host answered to, remembered per host so a rotation starts
# fresh and nothing needs clearing. Only a 404 moves it, and in both
# directions, so a host that changes its layout is followed either way.
_locale_by_host: dict[str, str] = {}
_locale_lock = threading.Lock()


def locale(domain: str) -> str:
    """``/it``, or ``""`` for a host known to serve its pages without it."""
    with _locale_lock:
        return _locale_by_host.get(domain, _LOCALE)


def site_url(domain: str, path: str = "") -> str:
    """``https://{domain}/it{path}``, in the form this host answers to."""
    return f"https://{domain}{locale(domain)}{path}"


def get_localized(domain: str, path: str, **kwargs) -> requests.Response:
    """GET a page of the source, with or without the locale segment.

    The remembered form is tried first, so a host that works costs exactly the
    one request it always did. On a 404 the other form is tried once, and
    adopted only if it is not a 404 too: a title that genuinely does not exist
    answers 404 both ways, and must not flip the host.
    """
    current = locale(domain)
    response = requests.get(f"https://{domain}{current}{path}", **kwargs)
    if getattr(response, "status_code", None) != 404:
        return response
    other = "" if current else _LOCALE
    retry = requests.get(f"https://{domain}{other}{path}", **kwargs)
    if getattr(retry, "status_code", None) == 404:
        return response
    with _locale_lock:
        _locale_by_host[domain] = other
    logger.info("%s serves its pages %s the %s segment", domain,
                "with" if other else "without", _LOCALE)
    return retry


def fetch_page_props(url: str, *, params: dict | None = None, timeout: int = 10) -> dict:
    """The ``props`` of one server-rendered page.

    One request, no session: the payload is in the HTML the source hands to any
    visitor. Shared by the search and by the start page's shelves.
    """
    response = requests.get(
        url, params=params, headers={"user-agent": get_headers()}, timeout=timeout,
    )
    response.raise_for_status()
    return _data_page(response.text).get("props") or {}


def fetch_site_props(domain: str, path: str = "", *, params: dict | None = None,
                     timeout: int = 10) -> dict:
    """``fetch_page_props`` for a page of the source, locale segment or not."""
    response = get_localized(
        domain, path, params=params, headers={"user-agent": get_headers()}, timeout=timeout,
    )
    response.raise_for_status()
    return _data_page(response.text).get("props") or {}


def get_domain_version(domain: str) -> str | None:
    """Verify domain is reachable and return site version string.

    Three answers, and the callers read them differently:
    ``RuntimeError`` is unreachable, ``None`` is a page that could not be
    parsed, ``""`` is a page with no version in it. ``domain_recovery.verify()``
    treats the last two as "not the source" while ``PUT /api/domain`` accepts
    them, because an administrator typing a host in is making a decision and a
    page we do not control is not.
    """
    site_url = f"https://{domain}"
    try:
        response = requests.get(site_url, headers={"user-agent": get_headers()}, timeout=10)
        response.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"Cannot reach {domain}: {e}")

    try:
        return _data_page(response.text).get("version", "")
    except LookupError:
        return ""
    except Exception as e:
        logger.warning("Cannot extract version from %s: %s", domain, e)
        return None


def _poster(images) -> str | None:
    for img in (images or []):
        if img.get("type") == "poster":
            return img.get("filename")
    return None


def normalize_title(t: dict) -> dict | None:
    """One raw title into the shape the panel renders, or None if unusable.

    A row without an id or a name cannot be opened, requested or downloaded, so
    it is dropped rather than rendered as a card that does nothing. The start
    page reuses this, and one malformed row in a slider must not take the whole
    shelf with it.

    ``poster`` is a bare filename here: the frontend routes it through
    ``/api/image/{filename}``, where the host is resolved server-side. Only
    AnimeUnity emits an absolute URL.
    """
    # ``is None`` rather than falsiness: an id of 0 is a legal id, and dropping
    # it would silently lose a title.
    if t.get("id") is None or not str(t.get("name") or "").strip():
        return None
    return {
        "name": t["name"],
        "type": t.get("type"),
        "id": t["id"],
        "slug": t.get("slug", ""),
        "score": t.get("score"),
        "release_date": t.get("release_date"),
        "last_air_date": t.get("last_air_date"),
        "age": t.get("age"),
        "seasons_count": t.get("seasons_count", 0),
        "poster": _poster(t.get("images", [])),
    }


def search(title_search: str, domain: str, *, page: int = 1,
           media_type: str | None = None) -> list[dict]:
    """Search the source.

    ``page`` is 1-based and the source's pages are disjoint, sixty to a page.
    ``media_type`` keeps only "movie" or only "tv", and is applied **before**
    the cap: filtering afterwards would answer "no films" to a search whose
    films all sat at position 22.

    The source has no filter of its own, so this one is **per page** and the
    source groups its results by kind: measured, "the" answers sixty series and
    not one film on page 1, while its films sit on later pages. A filtered page
    coming back empty therefore means "none of that kind here", never "no more
    results" — which is why the panel keeps offering the next page while a
    filter is on.

    ``page`` and ``media_type`` are keyword-only so that ``domain`` stays the
    second positional argument and out of reach of a caller that fumbles the
    order — it is the one argument that must never come from a request.
    """
    params = {"q": title_search}
    if page > 1:
        params["page"] = page

    props = fetch_site_props(domain, "/search", params=params)
    titles = props.get("titles") or []

    if media_type:
        titles = [t for t in titles if t.get("type") == media_type]

    return [t for t in (normalize_title(raw) for raw in titles[:_RESULT_CAP]) if t]
