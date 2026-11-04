"""Whether a newer release of the panel has been published.

GitHub's public API, no credential: an install asks at most a few times a day,
far below the 60 requests an hour an address gets. The repository is a constant
and never comes from a setting or a client, since it decides whose text ends up
in an administrator's browser.

The answer lives in memory. The running version only changes with a restart,
and a restart asks again shortly after boot.

Release notes are Markdown written for GitHub. They are rendered here rather
than in the browser so the escaping is pinned by tests/test_updates.py: every
piece of source text goes through ``html.escape`` and the only tags in the
result are the ones this module writes. Images are dropped to plain text on
purpose, so opening the notes never fetches anything from a third party.
"""

import asyncio
import logging
import re
import threading
import time
from html import escape

import requests

from app import __version__, config

logger = logging.getLogger(__name__)

REPOSITORY = "EdoardoFiore/StreamingCommunity-downloader"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases"

# One page is enough: an install more than 20 releases behind still learns
# there is something newer, and gets the most recent notes.
MAX_RELEASES = 20
MAX_NOTES_CHARS = 20_000

FIRST_CHECK_DELAY = 60
CHECK_INTERVAL = 6 * 3600
# "Controlla ora" is reachable by every anonymous visitor in open mode, and the
# address that runs out of GitHub's allowance is this server's.
MANUAL_CHECK_THROTTLE = 60

_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")

_lock = threading.Lock()
_releases: list[dict] = []
_checked_at: float | None = None
_attempted_at: float | None = None
_failed = False


def parse_version(value) -> tuple[int, int, int] | None:
    """The three numbers of a version, or None for anything else. Pre-release
    suffixes count as anything else: GitHub's flag already excludes those, and
    a tag that fails here must not be offered as an upgrade."""
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


# ── Release notes ────────────────────────────────────────────────────────────

_INLINE = re.compile(
    r"`(?P<code>[^`]+)`"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|\[(?P<label>[^\]]+)\]\((?P<href>https?://[^\s)]+)\)"
    r"|(?P<bare>https?://[^\s<>)]+)"
)
_HEADING = re.compile(r"(#{1,6})\s+(.*?)\s*#*")
_LIST_ITEM = re.compile(r"\s*([-*]|\d+\.)\s+(.*)")
_RULE = re.compile(r"-{3,}|\*{3,}")
_ALERT = re.compile(r"\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*")
_ALERT_LABELS = {
    "NOTE": "Nota",
    "TIP": "Suggerimento",
    "IMPORTANT": "Importante",
    "WARNING": "Attenzione",
    "CAUTION": "Attenzione",
}
# Each level of a quote is a recursive call, and the notes are not ours to trust
# to stop: deeper than this, the line is just text.
_MAX_QUOTE_DEPTH = 3


def _link(href: str, label: str) -> str:
    return f'<a href="{escape(href)}" target="_blank" rel="noopener noreferrer">{label}</a>'


def _inline(text: str) -> str:
    out = []
    position = 0
    for match in _INLINE.finditer(text):
        out.append(escape(text[position:match.start()]))
        position = match.end()
        if match["code"] is not None:
            out.append(f"<code>{escape(match['code'])}</code>")
        elif match["bold"] is not None:
            out.append(f"<strong>{_inline(match['bold'])}</strong>")
        elif match["href"] is not None:
            out.append(_link(match["href"], escape(match["label"])))
        else:
            # The full stop ending a sentence is not part of the address.
            url = match["bare"].rstrip(".,;:")
            out.append(_link(url, escape(url)) + escape(match["bare"][len(url):]))
    out.append(escape(text[position:]))
    return "".join(out)


def _starts_block(line: str) -> bool:
    stripped = line.strip()
    return (
        stripped.startswith(("```", ">"))
        or _HEADING.fullmatch(stripped) is not None
        or _LIST_ITEM.fullmatch(line) is not None
        or _RULE.fullmatch(stripped) is not None
    )


def _quote(lines: list[str], depth: int) -> str:
    body = [line.strip()[1:].removeprefix(" ") for line in lines]
    alert = _ALERT.fullmatch(body[0])
    if not alert:
        return f"<blockquote>{_blocks(body, depth + 1)}</blockquote>"
    kind = alert[1]
    return (
        f'<blockquote class="release-alert release-alert-{kind.lower()}">'
        f'<p class="release-alert-title">{_ALERT_LABELS[kind]}</p>'
        f"{_blocks(body[1:], depth + 1)}</blockquote>"
    )


def _blocks(lines: list[str], depth: int) -> str:
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1

        elif stripped.startswith("```"):
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            out.append(f"<pre><code>{escape(chr(10).join(code))}</code></pre>")

        elif stripped.startswith(">") and depth < _MAX_QUOTE_DEPTH:
            start = i
            while i < len(lines) and lines[i].strip().startswith(">"):
                i += 1
            out.append(_quote(lines[start:i], depth))

        elif _RULE.fullmatch(stripped):
            i += 1
            out.append("<hr>")

        elif heading := _HEADING.fullmatch(stripped):
            i += 1
            # The version is the heading of the modal, so the notes start below it.
            tag = "h4" if len(heading[1]) <= 2 else "h5"
            out.append(f"<{tag}>{_inline(heading[2])}</{tag}>")

        elif first := _LIST_ITEM.fullmatch(line):
            tag = "ol" if first[1][0].isdigit() else "ul"
            items = []
            while i < len(lines) and lines[i].strip():
                item = _LIST_ITEM.fullmatch(lines[i])
                if item:
                    items.append(item[2])
                elif lines[i][0].isspace():
                    items[-1] += " " + lines[i].strip()
                else:
                    break
                i += 1
            rendered = "".join(f"<li>{_inline(item)}</li>" for item in items)
            out.append(f"<{tag}>{rendered}</{tag}>")

        else:
            # Always takes the first line, even one that looks like a block:
            # a quote past the depth limit lands here and has to move forward.
            paragraph = [stripped]
            i += 1
            while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
                paragraph.append(lines[i].strip())
                i += 1
            out.append(f"<p>{_inline(' '.join(paragraph))}</p>")
    return "".join(out)


def render_notes(markdown) -> str:
    """Release notes as HTML that is safe to put in the page; '' when there are none."""
    if not isinstance(markdown, str) or not markdown.strip():
        return ""
    text = markdown[:MAX_NOTES_CHARS].replace("\r\n", "\n")
    return _blocks(text.split("\n"), 0)


# ── The check ────────────────────────────────────────────────────────────────


def _newer_releases(payload, current: str) -> list[dict]:
    """The published releases newer than ``current``, newest first."""
    if not isinstance(payload, list):
        raise ValueError("GitHub answered something that is not a list of releases")
    running = parse_version(current)
    found = []
    for release in payload:
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        parsed = parse_version(tag)
        if parsed is None or (running is not None and parsed <= running):
            continue
        found.append((parsed, {
            "version": ".".join(str(part) for part in parsed),
            "published_at": release.get("published_at") or None,
            # Built, not read from the payload: the tag has just been validated
            # as digits and dots, so this can only point at our own repository.
            "url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}",
            "notes_html": render_notes(release.get("body")),
        }))
    found.sort(key=lambda pair: pair[0], reverse=True)
    return [release for _, release in found]


def status() -> dict:
    with _lock:
        return {
            "current": __version__,
            "update_available": bool(_releases),
            "latest": _releases[0]["version"] if _releases else None,
            "releases": list(_releases),
            "checked_at": _checked_at,
            "failed": _failed,
        }


def check() -> dict:
    """Ask GitHub. Blocking. A failure keeps what was last known: GitHub being
    unreachable does not make the release that was found go away."""
    global _releases, _checked_at, _attempted_at, _failed

    with _lock:
        _attempted_at = time.time()
    try:
        response = requests.get(
            RELEASES_URL,
            params={"per_page": MAX_RELEASES},
            headers={
                "Accept": "application/vnd.github+json",
                # GitHub refuses a request without one.
                "User-Agent": f"SC-Panel/{__version__}",
            },
            timeout=10,
        )
        response.raise_for_status()
        releases = _newer_releases(response.json(), __version__)
    except (requests.RequestException, ValueError) as e:
        logger.warning("Update check failed: %s", e)
        with _lock:
            _failed = True
        return status()

    with _lock:
        _releases = releases
        _checked_at = time.time()
        _failed = False
    return status()


def check_now() -> dict:
    """The button: a real check, unless one has only just been made."""
    with _lock:
        recent = _attempted_at is not None and time.time() - _attempted_at < MANUAL_CHECK_THROTTLE
    return status() if recent else check()


async def update_check_loop():
    """Sleeps first, like the other loops, so the lifespan does no network I/O;
    only briefly, because a restart is exactly when the answer may have changed."""
    await asyncio.sleep(FIRST_CHECK_DELAY)
    while True:
        if config.get_settings().get("update_check_enabled", True):
            try:
                await asyncio.to_thread(check)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Update check failed")
        await asyncio.sleep(CHECK_INTERVAL)
