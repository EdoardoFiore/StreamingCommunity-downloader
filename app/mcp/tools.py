"""MCP tools: the panel's own endpoints, offered to an AI agent.

Every tool that changes something calls the **same handler the browser calls**,
with the token owner as the acting user (see ``app/mcp/auth.py``). Nothing is
reimplemented here, and that is the rule to keep: an earlier version rebuilt
following, season batches and requests by hand and lost, in turn, the
seed-or-roll-back that stops a follow from downloading a whole back catalogue,
the batch write-off that lets a season's summary fire, and the requester's
identity. Calling the handler gets every guard it has today and every one it
gains later.

Route dependencies do not run on a direct call, so each tool checks the
permission its route declares, against the owner's live permissions.

A refusal is raised as ``ToolError``, whose message reaches the agent; any
other exception is reported as a bare failure, its text kept in the server log.
"""

import asyncio
import inspect
import shutil
from typing import Literal

from fastapi import HTTPException
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import ValidationError
from starlette.requests import Request

from app import __version__
from app.auth.deps import OPEN_MODE_USER
from app.auth.permissions import Permission
from app.config import VIDEOS_DIR, configured_domain
from app.core import page, tv
from app.jobs import job_manager
from app.mcp import auth
from app.requests import models as request_models, router as requests_router
from app.routers import (
    anime as anime_router, domain as domain_router, downloads as downloads_router,
    home as home_router, metadata as metadata_router, search as search_router,
)
from app.watches import router as watches_router

mcp_server = MCPServer(
    name="StreamingCommunity Downloader",
    version=__version__,
    instructions=(
        "Search StreamingCommunity (films, TV series) and AnimeUnity (anime), start "
        "downloads into the Jellyfin library, follow series for new episodes, and work "
        "the request queue. Search first: download and follow tools need the id, slug "
        "and title exactly as search returns them. Every action runs as the panel user "
        "who generated the MCP token, with that user's permissions."
    ),
)

READ_ONLY = ToolAnnotations(read_only_hint=True)

# Mirrors the dependencies on the routes each tool calls.
CAN_BROWSE = (Permission.REQUEST, Permission.DOWNLOAD)
CAN_SEE_JOBS = (Permission.DOWNLOAD, Permission.MANAGE_REQUESTS)


# ── Plumbing ───────────────────────────────────────────────────────────────────

def _user(*required: Permission, mode: str = "and"):
    """The acting user, refused unless they hold ``required``."""
    user = auth.token_owner()
    if user is None:
        raise ToolError("Il proprietario del token MCP non è più valido: rigenera il token.")
    if required and not user.has(*required, mode=mode):
        names = (" o " if mode == "or" else " e ").join(p.name for p in required)
        raise ToolError(f"Permesso negato: serve {names}.")
    return user


def _request_as(user) -> Request:
    """A request carrying ``user`` the way AuthMiddleware would have set it."""
    return Request({
        "type": "http", "method": "POST", "path": "/mcp", "headers": [],
        "query_string": b"", "state": {"user": user},
    })


async def _call(handler, *args, **kwargs):
    """Run a route handler, sync or async, turning its refusals into ToolErrors."""
    try:
        if inspect.iscoroutinefunction(handler):
            return await handler(*args, **kwargs)
        return await asyncio.to_thread(handler, *args, **kwargs)
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from exc


def _model(cls, **fields):
    try:
        return cls(**fields)
    except ValidationError as exc:
        raise ToolError(str(exc)) from exc


def _domain() -> str:
    domain = configured_domain()
    if not domain:
        raise ToolError("Nessun dominio StreamingCommunity configurato nel pannello.")
    return domain


# ── Discovery ──────────────────────────────────────────────────────────────────

@mcp_server.tool(annotations=READ_ONLY)
async def search_content(
    query: str,
    source: Literal["streamingcommunity", "animeunity", "all"] = "all",
    media_type: Literal["movie", "tv", "ova", "ona", "special"] | None = None,
    page_num: int = 1,
) -> dict:
    """Search films and TV series (StreamingCommunity) and anime (AnimeUnity).

    Args:
        query: Title to look for.
        source: Which catalogue to search; 'all' searches both.
        media_type: Optional kind filter. 'movie'/'tv' apply to both sources,
            'ova'/'ona'/'special' only to AnimeUnity.
        page_num: 1-based page. On StreamingCommunity the kind filter is applied
            per page, so an empty filtered page does not mean the end.
    """
    _user(*CAN_BROWSE, mode="or")
    sources = ["streamingcommunity", "animeunity"] if source == "all" else [source]
    results, errors = {}, {}
    for name in sources:
        kind = media_type
        if name == "streamingcommunity" and kind not in (None, "movie", "tv"):
            continue
        try:
            results[name] = await _call(
                search_router.search, q=query, source=name, page=page_num,
                media_type=kind, dubbed=False,
            )
        except ToolError as exc:
            errors[name] = str(exc)
    if not results and errors:
        raise ToolError("; ".join(f"{k}: {v}" for k, v in errors.items()))
    return {"page": page_num, "results": results, "errors": errors or None}


@mcp_server.tool(annotations=READ_ONLY)
async def get_content_details(
    title_id: int,
    slug: str,
    media_type: Literal["movie", "tv"],
) -> dict:
    """Plot, genres, rating, artwork, cast and trailer of a StreamingCommunity title.

    Anime need no call: AnimeUnity search results already carry their details.

    Args:
        title_id: The title's id, from search.
        slug: The title's slug, from search.
        media_type: 'movie' or 'tv'.
    """
    _user(*CAN_BROWSE, mode="or")
    version = await asyncio.to_thread(page.get_domain_version, _domain()) or ""
    return await _call(
        metadata_router.get_title_metadata,
        media_type=media_type, title_id=str(title_id), slug=slug, version=version,
    )


@mcp_server.tool(annotations=READ_ONLY)
async def get_series_episodes(tv_id: int, slug: str, season_number: int | None = None) -> dict:
    """Seasons and episodes of a StreamingCommunity series.

    Args:
        tv_id: The series id, from search.
        slug: The series slug, from search.
        season_number: One season only; omit for all of them.
    """
    _user(*CAN_BROWSE, mode="or")
    domain = _domain()

    def fetch():
        version = page.get_domain_version(domain) or ""
        token = tv.get_token(tv_id, domain)
        count = tv.get_info_tv(tv_id, slug, version, domain) or 0
        wanted = [season_number] if season_number is not None else range(1, count + 1)
        return count, [
            {"season_number": n,
             "episodes": tv.get_info_season(tv_id, slug, domain, version, token, n) or []}
            for n in wanted if 1 <= n <= count
        ]

    count, seasons = await asyncio.to_thread(fetch)
    return {"tv_id": tv_id, "seasons_count": count, "seasons": seasons}


@mcp_server.tool(annotations=READ_ONLY)
async def get_anime_episodes(anime_id: str) -> dict:
    """Episodes of an AnimeUnity anime.

    Args:
        anime_id: The anime id exactly as search returns it (e.g. '12-one-piece').
    """
    _user(*CAN_BROWSE, mode="or")
    episodes = await _call(anime_router.get_episodes, anime_id)
    # Only what download_anime_episode takes: the raw records carry a dozen more
    # fields each, and a long series runs to a thousand episodes.
    return {"anime_id": anime_id,
            "episodes": [{"id": e["id"], "number": e.get("number")} for e in episodes]}


@mcp_server.tool(annotations=READ_ONLY)
async def get_home_shelves(source: Literal["streamingcommunity", "animeunity"] = "streamingcommunity") -> dict:
    """The shelves (trending, new releases, …) a source shows on its front page.

    Args:
        source: Which source's front page.
    """
    _user(*CAN_BROWSE, mode="or")
    return await _call(home_router.home, source=source)


# ── Downloads ──────────────────────────────────────────────────────────────────

@mcp_server.tool()
async def download_film(
    film_id: int,
    title: str,
    year: str | None = None,
    audio_languages: list[str] = ["ita"],
    subtitle_languages: list[str] = ["ita", "eng"],
) -> dict:
    """Download a StreamingCommunity film straight into the library.

    Args:
        film_id: The film id, from search.
        title: The film title, from search.
        year: Release year, used in the folder name.
        audio_languages: Audio tracks to keep.
        subtitle_languages: Subtitle tracks to keep.
    """
    user = _user(Permission.DOWNLOAD)
    body = _model(downloads_router.FilmDownloadRequest, id=film_id, title=title, year=year,
                  audio_languages=audio_languages, subtitle_languages=subtitle_languages)
    return await _call(downloads_router.download_film, body, _request_as(user))


@mcp_server.tool()
async def download_episode(
    tv_id: int,
    slug: str,
    tv_name: str,
    season_number: int,
    episode_number: int,
    year: str | None = None,
    audio_languages: list[str] = ["ita"],
    subtitle_languages: list[str] = ["ita", "eng"],
) -> dict:
    """Download one episode of a StreamingCommunity series.

    Args:
        tv_id: The series id, from search.
        slug: The series slug, from search.
        tv_name: The series title, from search.
        season_number: Season, 1-based.
        episode_number: Episode number within the season.
        year: Series year, used in the folder name.
        audio_languages: Audio tracks to keep.
        subtitle_languages: Subtitle tracks to keep.
    """
    user = _user(Permission.DOWNLOAD)
    domain = _domain()

    def enumerate_season():
        version = page.get_domain_version(domain) or ""
        token = tv.get_token(tv_id, domain)
        return token, tv.get_info_season(tv_id, slug, domain, version, token, season_number) or []

    token, episodes = await asyncio.to_thread(enumerate_season)
    index = next((i for i, ep in enumerate(episodes) if str(ep.get("n")) == str(episode_number)), None)
    if index is None:
        raise ToolError(f"Episodio {episode_number} non trovato nella stagione {season_number}.")
    body = _model(downloads_router.EpisodeDownloadRequest, tv_id=tv_id, eps=episodes, ep_index=index,
                  token=token, tv_name=tv_name, season=season_number, year=year,
                  audio_languages=audio_languages, subtitle_languages=subtitle_languages)
    return await _call(downloads_router.download_episode, body, _request_as(user))


@mcp_server.tool()
async def download_season(
    tv_id: int,
    slug: str,
    tv_name: str,
    season_number: int,
    year: str | None = None,
    audio_languages: list[str] = ["ita"],
    subtitle_languages: list[str] = ["ita", "eng"],
) -> dict:
    """Download every episode of one season of a StreamingCommunity series.

    Args:
        tv_id: The series id, from search.
        slug: The series slug, from search.
        tv_name: The series title, from search.
        season_number: Season, 1-based.
        year: Series year, used in the folder name.
        audio_languages: Audio tracks to keep.
        subtitle_languages: Subtitle tracks to keep.
    """
    user = _user(Permission.DOWNLOAD)
    body = _model(downloads_router.SeasonDownloadRequest, tv_id=tv_id, slug=slug, tv_name=tv_name,
                  season=season_number, year=year,
                  audio_languages=audio_languages, subtitle_languages=subtitle_languages)
    return await _call(downloads_router.download_season, body, _request_as(user))


@mcp_server.tool()
async def download_anime_episode(
    anime_id: str,
    anime_name: str,
    episode_id: int,
    episode_number: str,
    anime_type: str = "tv",
    year: str | None = None,
    audio_languages: list[str] = ["ita"],
    subtitle_languages: list[str] = ["ita", "eng"],
) -> dict:
    """Download one AnimeUnity episode.

    Args:
        anime_id: The anime id, from search.
        anime_name: The anime title, from search.
        episode_id: The episode's 'id', from get_anime_episodes.
        episode_number: The episode's 'number', from get_anime_episodes.
        anime_type: 'tv' for a series, 'movie' for a film.
        year: Year, used in the folder name.
        audio_languages: Audio tracks to keep.
        subtitle_languages: Subtitle tracks to keep.
    """
    user = _user(Permission.DOWNLOAD)
    body = _model(downloads_router.AnimeDownloadRequest, anime_id=anime_id, anime_name=anime_name,
                  episode={"id": episode_id, "number": episode_number}, anime_type=anime_type,
                  year=year, audio_languages=audio_languages, subtitle_languages=subtitle_languages)
    return await _call(downloads_router.download_anime, body, _request_as(user))


# ── Jobs ───────────────────────────────────────────────────────────────────────

JobStatus = Literal["all", "scheduled", "queued", "running", "done", "error", "cancelled"]


def _job_summary(job: dict) -> dict:
    progress = job.get("progress") or {}
    return {
        "job_id": job["job_id"], "title": job["title"], "type": job["type"],
        "status": job["status"], "pct": progress.get("pct"), "speed": progress.get("speed"),
        "eta": progress.get("eta"), "error": job.get("error"),
        "output_path": job.get("output_path"), "created_at": job["created_at"],
    }


@mcp_server.tool(annotations=READ_ONLY)
def list_downloads(status: JobStatus = "all", limit: int = 50) -> dict:
    """Download jobs, newest first.

    Args:
        status: Only jobs in this state.
        limit: At most this many.
    """
    _user(*CAN_SEE_JOBS, mode="or")
    jobs = [j for j in job_manager.list_jobs() if status == "all" or j["status"] == status]
    jobs.sort(key=lambda j: j["created_at"], reverse=True)
    return {"jobs": [_job_summary(j) for j in jobs[:max(limit, 0)]]}


@mcp_server.tool(annotations=READ_ONLY)
def get_download_progress(job_id: str) -> dict:
    """Live progress of one download job, phase by phase.

    Args:
        job_id: From a download tool or list_downloads.
    """
    _user(*CAN_SEE_JOBS, mode="or")
    job = next((j for j in job_manager.list_jobs() if j["job_id"] == job_id), None)
    if job is None:
        raise ToolError("Job non trovato.")
    return {**_job_summary(job), "phases": job.get("phases"), "progress": job.get("progress")}


@mcp_server.tool()
def cancel_download(job_id: str) -> dict:
    """Cancel a scheduled, queued or running download.

    Args:
        job_id: The job to cancel.
    """
    _user(*CAN_SEE_JOBS, mode="or")
    if not job_manager.cancel(job_id):
        raise ToolError("Job non trovato, o già concluso.")
    return {"job_id": job_id, "status": "cancelled"}


@mcp_server.tool()
async def retry_download(job_id: str) -> dict:
    """Run a failed download again, as a new job.

    Args:
        job_id: A job in state 'error'.
    """
    _user(Permission.DOWNLOAD)
    return await _call(downloads_router.retry, job_id)


# ── Followed series ────────────────────────────────────────────────────────────

@mcp_server.tool()
async def follow_series(
    source: Literal["streamingcommunity", "animeunity"],
    external_id: str,
    title: str,
    slug: str | None = None,
    year: str | None = None,
    anime_type: str | None = None,
    audio_languages: list[str] = ["ita"],
    subtitle_languages: list[str] = [],
) -> dict:
    """Follow a series or anime so new episodes are fetched as they are published.

    Everything already published is marked as seen: following means "from now
    on". Whether a new episode downloads by itself or waits in the request queue
    follows the token owner's permissions, as it would in the browser.

    Args:
        source: 'streamingcommunity' for a TV series, 'animeunity' for an anime.
        external_id: The id, from search.
        title: The title, from search.
        slug: The slug, from search.
        year: Year, used in the folder name.
        anime_type: AnimeUnity only: 'tv' for a series.
        audio_languages: Audio tracks to keep on every new episode.
        subtitle_languages: Subtitle tracks to keep on every new episode.
    """
    user = _user(*CAN_BROWSE, mode="or")
    body = _model(watches_router.WatchCreate, source=source,
                  media_type="anime" if source == "animeunity" else "tv",
                  external_id=external_id, title=title, slug=slug, year=year,
                  anime_type=anime_type, audio_languages=audio_languages,
                  subtitle_languages=subtitle_languages)
    return await _call(watches_router.follow_series, body, _request_as(user))


@mcp_server.tool(annotations=READ_ONLY)
async def list_followed_series() -> dict:
    """The series and anime the token owner follows."""
    user = _user(*CAN_BROWSE, mode="or")
    return await _call(watches_router.list_my_watches, _request_as(user))


@mcp_server.tool()
async def unfollow_series(watch_id: int) -> dict:
    """Stop following a series.

    Args:
        watch_id: From list_followed_series.
    """
    user = _user(*CAN_BROWSE, mode="or")
    return await _call(watches_router.unfollow_series, watch_id, _request_as(user))


@mcp_server.tool()
async def check_series_now(watch_id: int) -> dict:
    """Look for new episodes of a followed series now, instead of at the next cycle.

    Args:
        watch_id: From list_followed_series.
    """
    user = _user(*CAN_BROWSE, mode="or")
    return await _call(watches_router.check_now, watch_id, _request_as(user))


# ── Request queue ──────────────────────────────────────────────────────────────

def _queue_user(*required: Permission, mode: str = "and"):
    """``_user`` for the request queue, which does not exist without accounts —
    said as such, rather than as the permission the implicit user lacks."""
    if auth.token_owner() is OPEN_MODE_USER:
        raise ToolError("Il pannello è senza account: non c'è una coda di richieste.")
    return _user(*required, mode=mode)


@mcp_server.tool()
async def submit_request(
    source: Literal["streamingcommunity", "animeunity"],
    media_type: Literal["film", "episode", "anime"],
    external_id: str,
    title: str,
    slug: str | None = None,
    year: str | None = None,
    season: int | None = None,
    episode_number: str | None = None,
    anime_type: str | None = None,
    audio_languages: list[str] = ["ita"],
    subtitle_languages: list[str] = [],
) -> dict:
    """Ask for a title through the request queue, for an approver to decide.

    The requested audio languages are checked against the source first.

    Args:
        source: Where the title comes from.
        media_type: 'film', 'episode' (needs season and episode_number) or
            'anime' (needs episode_number).
        external_id: The id, from search.
        title: The title, from search.
        slug: The slug, from search.
        year: Year.
        season: Season, for an episode.
        episode_number: Episode number, for an episode or anime.
        anime_type: AnimeUnity only: 'tv' or 'movie'.
        audio_languages: Audio tracks wanted.
        subtitle_languages: Subtitle tracks wanted.
    """
    user = _queue_user(Permission.REQUEST)
    body = _model(requests_router.CreateRequest, source=source, media_type=media_type,
                  external_id=external_id, title=title, slug=slug, year=year, season=season,
                  episode_number=episode_number, anime_type=anime_type,
                  audio_languages=audio_languages, subtitle_languages=subtitle_languages)
    return await _call(requests_router.create_request, body, _request_as(user))


RequestStatus = Literal[
    "all", request_models.PENDING, request_models.APPROVED, request_models.DOWNLOADING,
    request_models.NEEDS_ATTENTION, request_models.COMPLETED, request_models.AVAILABLE,
    request_models.DENIED, request_models.FAILED, request_models.CANCELLED,
]


@mcp_server.tool(annotations=READ_ONLY)
async def list_requests(status: RequestStatus = "all", limit: int = 50) -> dict:
    """Requests in the queue: everyone's for an approver, otherwise the owner's own.

    Args:
        status: Only requests in this state.
        limit: At most this many.
    """
    user = _queue_user(Permission.REQUEST, Permission.MANAGE_REQUESTS, mode="or")
    if user.has(Permission.MANAGE_REQUESTS):
        rows = await _call(requests_router.list_requests, None if status == "all" else status)
    else:
        rows = await _call(requests_router.list_my_requests, _request_as(user))
        rows = [r for r in rows if status == "all" or r["status"] == status]
    return {"requests": rows[:max(limit, 0)]}


@mcp_server.tool()
async def approve_request(request_id: int) -> dict:
    """Approve a pending request; the download starts in the background.

    Args:
        request_id: From list_requests.
    """
    user = _queue_user(Permission.MANAGE_REQUESTS)
    return await _call(requests_router.approve_request, request_id, _request_as(user),
                       requests_router.ApproveRequestOptions())


# ── System ─────────────────────────────────────────────────────────────────────

@mcp_server.tool(annotations=READ_ONLY)
async def get_system_status() -> dict:
    """Panel version, source domain and whether it answers, free disk space, active jobs."""
    _user(Permission.DOWNLOAD, Permission.MANAGE_SETTINGS, mode="or")
    domain = configured_domain()

    def probe():
        version = None
        if domain:
            try:
                version = page.get_domain_version(domain)
            except Exception:
                version = None
        try:
            usage = shutil.disk_usage(VIDEOS_DIR)
            storage = {"path": str(VIDEOS_DIR), "free_gb": round(usage.free / 1024**3, 1),
                       "total_gb": round(usage.total / 1024**3, 1)}
        except OSError:
            storage = {"path": str(VIDEOS_DIR), "free_gb": None, "total_gb": None}
        return version, storage

    version, storage = await asyncio.to_thread(probe)
    active = sum(1 for j in job_manager.list_jobs() if j["status"] in ("queued", "running"))
    return {
        "panel_version": __version__,
        "source_domain": domain or None,
        "source_reachable": bool(version),
        "storage": storage,
        "active_jobs": active,
    }


@mcp_server.tool(annotations=READ_ONLY)
async def list_libraries() -> dict:
    """The library folders downloads land in, and the folders excluded from checks."""
    _user(Permission.MANAGE_SETTINGS)
    return await _call(domain_router.get_libraries)
