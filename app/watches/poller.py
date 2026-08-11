"""Periodic check for new episodes of followed series.

Nothing here downloads anything directly: a new episode becomes an ordinary
request, and the existing request pipeline does deduplication, the library
check, resolution and the download. The only decision this module adds is
whether that request is approved on the spot or waits for a human.
"""

import asyncio
import logging

from app.auth.permissions import Permission
from app.config import get_settings
from app.requests import models as request_models, resolver, service
from app.watches import models

logger = logging.getLogger(__name__)

# New episodes are not latency-sensitive: nobody needs to know within seconds
# that an episode dropped, and every cycle is real network traffic per series.
POLL_INTERVAL_DEFAULT_MINUTES = 240
POLL_INTERVAL_MIN_MINUTES = 15


def _interval_seconds() -> int:
    try:
        minutes = int(get_settings().get(
            "series_watch_interval_minutes", POLL_INTERVAL_DEFAULT_MINUTES
        ))
    except (TypeError, ValueError):
        minutes = POLL_INTERVAL_DEFAULT_MINUTES
    return max(minutes, POLL_INTERVAL_MIN_MINUTES) * 60


# ── Enumeration ────────────────────────────────────────────────────────────────

def episode_key(media_type: str, season, episode_number) -> str:
    if media_type == models.TV:
        return f"S{int(season or 1):02d}E{episode_number}"
    return f"E{episode_number}"


def current_episodes(watch: models.Watch) -> list[tuple[str, dict]]:
    """Everything the source is publishing right now, as ``(key, episode)``.

    The full season list is re-fetched each cycle instead of remembering how many
    seasons there were, so a brand-new season is picked up with no special case.
    """
    if watch.media_type == models.ANIME:
        from app.core import animeunity

        return [
            (episode_key(models.ANIME, None, ep["number"]), {"number": ep["number"], **ep})
            for ep in animeunity.get_episodes(watch.external_id)
        ]

    from app.core.page import get_domain_version
    from app.core.tv import get_info_season, get_info_tv, get_token

    domain = resolver.current_domain()
    version = get_domain_version(domain) or ""
    token = get_token(int(watch.external_id), domain)
    seasons_count = get_info_tv(int(watch.external_id), watch.slug or "", version, domain)

    found: list[tuple[str, dict]] = []
    for season in range(1, (seasons_count or 0) + 1):
        episodes = get_info_season(
            int(watch.external_id), watch.slug or "", domain, version, token, season
        )
        for episode in episodes or []:
            found.append((episode_key(models.TV, season, episode["n"]), {**episode, "season": season}))
    return found


# ── Decision ───────────────────────────────────────────────────────────────────

def _draft_request(watch: models.Watch, season, episode_number) -> request_models.Request:
    """A throwaway record shaped like a request, only to ask the resolver where
    the file would land. It is never stored."""
    return request_models.Request(
        id=0,
        content_key="",
        source=watch.source,
        media_type=resolver.ANIME if watch.media_type == models.ANIME else resolver.EPISODE,
        external_id=watch.external_id,
        slug=watch.slug,
        title=watch.title,
        year=watch.year,
        poster=watch.poster,
        season=season,
        episode_number=str(episode_number),
        anime_type=watch.anime_type,
        audio_languages=watch.audio_languages,
        subtitle_languages=watch.subtitle_languages,
        available_snapshot=None,
        status=request_models.PENDING,
        problem=None,
        denial_reason=None,
        requested_by=watch.created_by,
        decided_by=None,
        decided_at=None,
        job_id=None,
        output_path=None,
        created_at="",
        updated_at="",
    )


def _may_auto_download(watch: models.Watch) -> bool:
    """Whether new episodes of this series skip the approval queue.

    Either the owner can start downloads on their own anyway, or an approver has
    already said yes once for this series specifically.
    """
    if watch.auto_approve:
        return True
    permissions = models.owner_permissions(watch.id)
    if permissions is None:
        # Owner disabled or deleted: fall back to the queue.
        return False
    return bool(permissions & int(Permission.DOWNLOAD))


def process_episode(watch: models.Watch, key: str, episode: dict) -> str:
    """Handle one not-yet-seen episode. Returns what was done, for the log."""
    season = episode.get("season") if watch.media_type == models.TV else None
    number = episode.get("n") if watch.media_type == models.TV else episode.get("number")

    draft = _draft_request(watch, season, number)
    try:
        if resolver.is_in_library(draft):
            models.mark_seen(watch.id, key)
            return "already_in_library"
    except Exception:
        logger.exception("Library check failed for watch %s episode %s", watch.id, key)

    request, created = service.create_request(
        requested_by=watch.created_by,
        source=watch.source,
        media_type=draft.media_type,
        external_id=watch.external_id,
        title=watch.title,
        slug=watch.slug,
        year=watch.year,
        poster=watch.poster,
        season=season,
        episode_number=str(number),
        anime_type=watch.anime_type,
        audio_languages=watch.audio_languages,
        subtitle_languages=watch.subtitle_languages,
        watch_id=watch.id,
    )

    outcome = "queued"
    if created and request.status == request_models.PENDING and _may_auto_download(watch):
        try:
            service.approve(request.id, decided_by=watch.created_by)
            outcome = "auto_approved"
        except Exception:
            # It stays pending and an approver can still act on it.
            logger.exception("Auto-approval failed for request %s", request.id)
            outcome = "auto_approval_failed"

    models.mark_seen(watch.id, key)
    return outcome


def poll_watch(watch: models.Watch) -> dict:
    """Check one series. Returns a small summary, mostly for tests and logs."""
    episodes = current_episodes(watch)
    seen = models.seen_keys(watch.id)
    fresh = [(key, ep) for key, ep in episodes if key not in seen]

    outcomes: dict[str, int] = {}
    for key, episode in fresh:
        try:
            outcome = process_episode(watch, key, episode)
        except Exception:
            logger.exception("Failed handling %s of watch %s", key, watch.id)
            outcome = "error"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1

    if fresh:
        logger.info(
            "Watch %s (%s): %d new episode(s) %s", watch.id, watch.title, len(fresh), outcomes
        )
    return {"checked": len(episodes), "new": len(fresh), "outcomes": outcomes}


def run_poll_cycle() -> dict:
    """One pass over every active watch. Never raises: one bad series must not
    stop the others, and the loop above must keep running."""
    summary = {"watches": 0, "new": 0}
    for watch in models.list_all():
        summary["watches"] += 1
        try:
            result = poll_watch(watch)
            summary["new"] += result["new"]
        except Exception:
            logger.exception("Poll failed for watch %s (%s)", watch.id, watch.title)
        finally:
            models.touch_checked(watch.id)
    return summary


async def watch_poller_loop():
    """Background task started from the app lifespan."""
    while True:
        await asyncio.sleep(_interval_seconds())
        try:
            await asyncio.to_thread(run_poll_cycle)
        except Exception:
            logger.exception("Watch poll cycle failed")
