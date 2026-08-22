"""Finding the source domain again after it rotates.

The source's domain changes every few weeks. Until now that meant the panel
simply stopped working: searches failed, nothing said why, and an administrator
had to go and find the new address themselves.

A third-party page publishes the current one. This module reads it, checks the
candidate is plausible, proves it actually serves the source, and — by default —
*proposes* it rather than adopting it. That default is the whole design. The
page is editable by people we do not control, and the domain is not a cosmetic
setting: every search, every image fetch and every download referer goes to
whatever host is configured here. Adopting one because a web page said so, with
nobody looking, hands a stranger the ability to point this panel wherever they
like. `domain_auto_apply` exists for deployments that want it anyway, and is off
unless asked for.

The candidate is held in memory, not in data.json. It is discovery state, not
configuration, and a candidate frozen across a restart could be applied long
after the page it came from had changed again.
"""

import ipaddress
import logging
import os
import re
import socket
import threading
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from app import config
from app.core.headers import get_headers
from app.core.page import get_domain_version

logger = logging.getLogger(__name__)

# Where candidates come from. Overridable because the page is somebody else's
# and may be deleted, renamed, or replaced by a better source.
CANDIDATES_URL = os.getenv(
    "DOMAIN_SOURCE_URL",
    "https://telegra.ph/Link-Aggiornato-StreamingCommunity-09-29",
)

# Which registrable names may ever be adopted automatically.
#
# A module constant with an env override rather than a settings field, on
# purpose: this is the check that stops an edited page from redirecting the
# panel to an arbitrary host, and a text box in the UI that relaxes it would be
# a loaded gun. A genuine rebrand fails closed — the candidate is logged and
# reported as rejected, and an administrator types the new name in by hand,
# which is a decision made by a person.
NAME_PATTERN = re.compile(
    os.getenv("DOMAIN_NAME_PATTERN", r"^streaming(community|unity)[a-z0-9-]{0,12}$")
)

_HOST_RE = re.compile(r"^[a-z0-9.-]{4,253}$")

# Hosts that are never a public streaming site, whatever DNS says.
_LOCAL_SUFFIXES = (".local", ".internal", ".localhost", ".home", ".lan")

# The page carries a handful of links, most of them telegra.ph's own. Anything
# past this is not a candidate list, it is a different page.
_MAX_CANDIDATES = 5

# The reactive trigger fires whenever the configured domain fails to verify,
# which on a broken panel is every page load of every user. Without a floor
# between checks that becomes a small stampede against someone else's page.
_MIN_CHECK_INTERVAL = 600

_lock = threading.Lock()
_pending: dict | None = None
_last_check_at: float | None = None


def _now() -> float:
    """Monotonic clock, indirected so tests can move it without touching time."""
    return time.monotonic()


# ── Candidates ────────────────────────────────────────────────────────────────

def fetch_candidates(url: str | None = None) -> list[str]:
    """Hosts linked from the source-of-truth page, in order, deduplicated."""
    target = url or CANDIDATES_URL
    response = requests.get(target, headers={"user-agent": get_headers()}, timeout=10)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")
    hosts: list[str] = []
    for anchor in soup.find_all("a"):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        parsed = urlparse(href)
        # Keep the scheme: is_plausible refuses anything but https, and it must
        # see what the page actually published rather than an assumed default.
        host = (parsed.hostname or "").lower().strip(".")
        if not host or parsed.scheme != "https":
            continue
        if host not in hosts:
            hosts.append(host)
    return hosts[:_MAX_CANDIDATES]


# ── The guard ─────────────────────────────────────────────────────────────────

def _check_shape(host: str) -> str | None:
    """Syntactic rejection reason, or None if the host looks like a candidate."""
    if not host or not _HOST_RE.match(host):
        return "forma non valida"
    if host.endswith(_LOCAL_SUFFIXES) or host == "localhost":
        return "nome locale"

    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return "indirizzo IP"

    labels = host.split(".")
    if len(labels) == 3 and labels[0] == "www":
        labels = labels[1:]
    if len(labels) != 2:
        # The load-bearing rule. Checking only the first label would accept
        # streamingcommunity.attacker.tld, where the part that decides where the
        # traffic goes is the attacker's. Multi-part suffixes (.co.uk) are
        # rejected as collateral; typing the domain by hand still works.
        return "non è un dominio di secondo livello"

    name = labels[0]
    if not NAME_PATTERN.match(name):
        return f"nome «{name}» non riconosciuto"
    return None


def _resolves_to_public(host: str) -> bool:
    """Whether every address the host resolves to is a public one.

    Without this, a candidate could point the panel's own fetches at the network
    it is running inside — the panel is a willing HTTP client with an image
    proxy attached.
    """
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        logger.info("Candidate %s does not resolve: %s", host, exc)
        return False
    if not infos:
        return False

    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            logger.warning("Candidate %s resolves to a non-public address %s", host, address)
            return False
    return True


def is_plausible(host: str) -> tuple[bool, str]:
    """Whether this host may be adopted, and why not when it may not.

    The reason is returned rather than logged and dropped: a rebranded source is
    indistinguishable from an attack from in here, and the administrator who has
    to tell them apart needs to see what was refused.
    """
    reason = _check_shape(host)
    if reason:
        return False, reason
    if not _resolves_to_public(host):
        return False, "risolve a un indirizzo non pubblico"
    return True, ""


def verify(host: str) -> str | None:
    """The site version this host serves, or None if it does not serve the source.

    Deliberately stricter than ``PUT /api/domain``, which accepts a host whose
    version string comes back empty: an administrator typing a domain in is
    making a decision, and a page we do not control is not. Here an empty
    version is not proof of anything, so it is not enough.
    """
    try:
        version = get_domain_version(host)
    except Exception as exc:
        logger.info("Candidate %s failed verification: %s", host, exc)
        return None
    return version or None


# ── Pending candidate ─────────────────────────────────────────────────────────

def pending() -> dict | None:
    with _lock:
        return dict(_pending) if _pending else None


def clear_pending() -> None:
    global _pending
    with _lock:
        _pending = None


def _set_pending(candidate: dict | None) -> None:
    global _pending
    with _lock:
        _pending = candidate


def apply_candidate(host: str) -> str:
    """Write a candidate as the configured domain, re-verifying it first.

    Returns the site version. Raises RuntimeError if the host no longer checks
    out — a candidate can go stale between being proposed and being confirmed.
    """
    ok, reason = is_plausible(host)
    if not ok:
        raise RuntimeError(f"Dominio non adottabile: {reason}")
    version = verify(host)
    if version is None:
        raise RuntimeError(f"«{host}» non risponde come la sorgente attesa")

    config.update_data({"domain": host})
    clear_pending()
    logger.info("Source domain set to %s (version %s)", host, version)
    return version


# ── The cycle ─────────────────────────────────────────────────────────────────

def _settings() -> dict:
    return config.get_settings()


def run_check(force: bool = False) -> dict:
    """One pass: is the configured domain alive, and if not, what could replace it.

    ``force`` is the "Controlla ora" button: it bypasses the throttle and looks
    for a candidate even when the current domain is working, because an
    administrator who pressed it wants an answer rather than a shrug.
    """
    global _last_check_at

    result: dict = {
        "current": config.configured_domain(),
        "current_ok": False,
        "candidate": None,
        "version": None,
        "applied": False,
        "rejected": [],
        "checked": False,
    }

    if not force:
        with _lock:
            if _last_check_at is not None and _now() - _last_check_at < _MIN_CHECK_INTERVAL:
                logger.debug("Domain check skipped: within the throttle window")
                return result

    with _lock:
        _last_check_at = _now()
    result["checked"] = True

    if result["current"]:
        result["current_ok"] = verify(result["current"]) is not None

    # A working domain does not need replacing. Only an explicit request looks
    # further, so the ordinary periodic check costs one request to the source
    # and nothing to anybody else.
    if result["current_ok"] and not force:
        _set_pending(None)
        return result

    try:
        candidates = fetch_candidates()
    except Exception as exc:
        logger.warning("Cannot read the domain source page: %s", exc)
        return result

    for host in candidates:
        if host == result["current"]:
            continue
        ok, reason = is_plausible(host)
        if not ok:
            logger.info("Rejected candidate %s: %s", host, reason)
            result["rejected"].append({"host": host, "reason": reason})
            continue
        version = verify(host)
        if version is None:
            result["rejected"].append({"host": host, "reason": "non verificato"})
            continue

        result["candidate"] = host
        result["version"] = version
        break

    if result["candidate"] is None:
        return result

    if _settings().get("domain_auto_apply"):
        try:
            apply_candidate(result["candidate"])
            result["applied"] = True
        except RuntimeError as exc:
            logger.warning("Auto-apply of %s failed: %s", result["candidate"], exc)
        return result

    _set_pending({
        "host": result["candidate"],
        "version": result["version"],
        "found_at": time.time(),
    })
    return result
