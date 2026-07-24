"""Incremental backoff against credential guessing on the two endpoints that
accept a password without a session: POST /api/auth/jellyfin and
POST /api/auth/setup.

Keyed on (client IP, username): an attacker guessing passwords for one account
from one address backs off harder with every failure, while a legitimate user
elsewhere is never affected by someone else's failed attempts against the same
account name, and one attacker rotating through many usernames from a single
address doesn't get a free pass on any of them individually.

In-memory, like the rest of this single-process panel's live state (JobManager,
notification broadcast) — see CLAUDE.md's single-process constraint. A restart
clears it, which is an acceptable trade-off: the administrator restarting the
panel is not the attacker, and persisting a security throttle to the database
would be a lot of machinery for a threat this doesn't materially help against.
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

# A handful of attempts pass through immediately — the point is to punish
# sustained guessing, not to annoy someone who fat-fingered their password
# twice.
FREE_ATTEMPTS = 3
# From the first throttled attempt on, the cooldown doubles with every further
# failure: 2s, 4s, 8s, 16s, ... capped so a very persistent attacker cannot
# push a legitimate user's own next attempt out for hours.
BASE_DELAY = 2.0
MAX_DELAY = 300.0  # 5 minutes
# Stale entries (nobody has failed against this key in a long while) are
# dropped lazily on access rather than swept on a timer — this module has no
# background thread of its own, matching how small the state actually gets in
# self-hosted use.
FORGET_AFTER = 3600.0  # 1 hour of inactivity

_lock = threading.Lock()
_failures: dict[tuple[str, str], tuple[int, float]] = {}  # (ip, user) -> (count, last_failure)


def _key(ip: str | None, username: str) -> tuple[str, str]:
    return (ip or "unknown", (username or "").strip().lower())


def _delay_for(count: int) -> float:
    if count <= FREE_ATTEMPTS:
        return 0.0
    return min(BASE_DELAY * (2 ** (count - FREE_ATTEMPTS - 1)), MAX_DELAY)


def seconds_until_allowed(ip: str | None, username: str) -> float:
    """0 if the next attempt may proceed now, otherwise how long to wait."""
    key = _key(ip, username)
    now = time.monotonic()
    with _lock:
        entry = _failures.get(key)
        if entry is None:
            return 0.0
        count, last_failure = entry
        if now - last_failure > FORGET_AFTER:
            del _failures[key]
            return 0.0
    remaining = _delay_for(count) - (now - last_failure)
    return max(0.0, remaining)


def record_failure(ip: str | None, username: str) -> float:
    """Register a failed credential check; returns the new cooldown in seconds."""
    key = _key(ip, username)
    now = time.monotonic()
    with _lock:
        count, _ = _failures.get(key, (0, 0.0))
        count += 1
        _failures[key] = (count, now)
    delay = _delay_for(count)
    if delay:
        logger.warning(
            "Backing off login for %r from %s: %d consecutive failures, %.0fs cooldown",
            username, ip, count, delay,
        )
    return delay


def record_success(ip: str | None, username: str):
    with _lock:
        _failures.pop(_key(ip, username), None)
