"""Resolving a request against the source, at the moment it is approved.

Days can pass between asking and approving. In that time the source domain can
change, the link can die and the available audio tracks can change. All of that
is re-checked here, against the *current* configuration — never against values
the client sent or values frozen at request time.
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Callable

from app.config import DATA_FILE, VIDEOS_DIR
from app.core import animeunity, paths, probe
from app.requests import models

logger = logging.getLogger(__name__)

FILM = "film"
EPISODE = "episode"
ANIME = "anime"

LIBRARY_TYPES = {FILM: "film", EPISODE: "tv", ANIME: "anime"}


class ResolutionError(Exception):
    """Resolution failed in a way a human has to look at."""

    code = "error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    @property
    def problem(self) -> str:
        return f"{self.code}: {self.message}"


class LinkDead(ResolutionError):
    code = "link_dead"


class MissingTracks(ResolutionError):
    code = "missing_audio"


class NotConfigured(ResolutionError):
    code = "not_configured"


# ── Server-side configuration ──────────────────────────────────────────────────

def _read_data() -> dict:
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def current_domain() -> str:
    """The configured source domain.

    Read here rather than taken from the request body: the client must not be
    able to point the panel at an arbitrary host, and a domain saved with a
    request would be stale by the time it is approved.
    """
    domain = (_read_data().get("domain") or "").strip()
    if not domain:
        raise NotConfigured("Nessun dominio configurato nelle impostazioni")
    return domain


def library_dir(media_type: str) -> str:
    for library in _read_data().get("libraries", []):
        if library.get("type") == LIBRARY_TYPES.get(media_type):
            return library["path"]
    return str(VIDEOS_DIR)


def destination_path(request: models.Request) -> str:
    """Where this request's file would land, using the downloader's own layout."""
    output_dir = library_dir(request.media_type)
    if request.media_type == FILM:
        return paths.film_path(output_dir, request.title, request.year)
    if request.media_type == EPISODE:
        return paths.episode_path(
            output_dir, request.title, request.season or 1,
            request.episode_number or "1", request.year,
        )
    return paths.anime_path(
        output_dir, request.title, request.episode_number or "1",
        request.anime_type or "tv", request.year,
    )


def existing_file(request: models.Request) -> str | None:
    """The file already occupying this request's destination, if any."""
    path = destination_path(request)
    if os.path.exists(path):
        return path
    # The downloader remuxes to .mkv when there are several audio tracks.
    mkv = os.path.splitext(path)[0] + ".mkv"
    return mkv if os.path.exists(mkv) else None


def library_gap(request: models.Request) -> dict | None:
    """Which requested languages the file in the library does not carry.

    ``None`` when there is no file, or when there is one that cannot be
    inspected. Both mean "download it"; they differ only in what is already
    there, which the caller sorts out with ``existing_file``.

    This is why the check cannot simply ask whether the path exists. The
    destination carries no language — two people asking for one film in
    different audio make two requests that land on the same file — so an
    existence test told the second of them their track was ready when what sat
    there was the first one's.
    """
    path = existing_file(request)
    if path is None:
        return None
    gap = probe.missing_languages(path, request.audio_languages, request.subtitle_languages)
    if gap is None:
        # Cannot look inside: ffprobe is optional and the bundled ffmpeg does
        # not bring one. Treating an unreadable file as complete is the older
        # behaviour, and the alternative — calling every track missing — would
        # re-download the same file for ever.
        logger.info("Cannot inspect %s; assuming it satisfies the request", path)
        return {"audio": [], "subtitles": []}
    return gap


def is_in_library(request: models.Request) -> bool:
    """Whether the request is already satisfied — the file *and* its tracks."""
    gap = library_gap(request)
    return gap is not None and not gap["audio"] and not gap["subtitles"]


# ── Resolution ─────────────────────────────────────────────────────────────────

@dataclass
class Resolution:
    available: dict
    submit: Callable[[], str]


def _widen_to_everyone(common: dict, request: models.Request, available: dict):
    """Download what everyone asked for, not only what this request asked for.

    Requests for one title in different languages are kept apart on purpose, but
    they all resolve to the same path, so whoever finished last decided what the
    file held and quietly took the others' languages away. The union leaves a
    file that satisfies all of them.

    The extras are filtered against what the source offers *now*, and the
    caller's own choice is never filtered. That difference is the point:
    ``strict_audio`` must still fail loudly when the language this requester
    chose has gone, while a language somebody else picked months ago must not
    fail a download nobody else asked to be strict about.
    """
    audio, subtitles = models.wanted_languages(request)
    offered_audio = {str(c).lower() for c in (available.get("audio") or [])}
    offered_subs = {str(c).lower() for c in (available.get("subtitles") or [])}

    mine_audio, mine_subs = request.audio_languages, request.subtitle_languages
    if offered_audio:
        audio = [l for l in audio if l in mine_audio or l.lower() in offered_audio]
    if offered_subs:
        subtitles = [l for l in subtitles if l in mine_subs or l.lower() in offered_subs]

    common["audio_languages"] = audio
    common["subtitle_languages"] = subtitles
    if audio != mine_audio or subtitles != mine_subs:
        logger.info(
            "Request %s widened to what everyone wants: audio %s, subtitles %s",
            request.id, audio, subtitles,
        )


def _check_audio(request: models.Request, available: dict):
    """Refuse to proceed when a requested audio track is gone.

    Falling back to whatever is on offer would produce a file that looks right
    and plays in the wrong language, with nobody told. That has to be a person's
    decision, so it becomes a needs_attention request instead.
    """
    offered = {str(code).lower() for code in (available.get("audio") or [])}
    if not offered:
        # A source with a single embedded audio track advertises none; there is
        # nothing to pick and nothing to get wrong.
        return
    missing = [lang for lang in request.audio_languages if lang.lower() not in offered]
    if missing:
        raise MissingTracks(
            f"richiesto {', '.join(missing)} — disponibile {', '.join(sorted(offered)) or 'niente'}"
        )


def available_tracks(request: models.Request) -> dict:
    """Audio and subtitle languages currently on offer for this request."""
    if request.source == "animeunity":
        episode = _find_anime_episode(request)
        return animeunity.get_episode_languages(episode["id"])

    from app.core.film import get_film_languages
    from app.core.tv import get_episode_languages, get_info_season, get_token

    domain = current_domain()
    if request.media_type == FILM:
        return get_film_languages(int(request.external_id), domain)

    token = get_token(int(request.external_id), domain)
    episode = _find_episode(request, domain, token)
    return get_episode_languages(int(request.external_id), episode["id"], domain, token)


def _find_anime_episode(request: models.Request) -> dict:
    episodes = animeunity.get_episodes(request.external_id)
    wanted = str(request.episode_number)
    for episode in episodes:
        if str(episode.get("number")) == wanted:
            return episode
    raise LinkDead(f"episodio {wanted} non più presente su AnimeUnity")


def _season_episodes(request: models.Request, domain: str, token: str) -> list[dict]:
    from app.core.page import get_domain_version
    from app.core.tv import get_info_season

    version = get_domain_version(domain) or ""
    episodes = get_info_season(
        int(request.external_id), request.slug or "", domain, version, token,
        request.season or 1,
    )
    if not episodes:
        raise LinkDead(f"stagione {request.season} non più disponibile")
    return episodes


def _find_episode(request: models.Request, domain: str, token: str) -> dict:
    for episode in _season_episodes(request, domain, token):
        if str(episode["n"]) == str(request.episode_number):
            return episode
    raise LinkDead(
        f"episodio S{request.season}E{request.episode_number} non più presente sulla fonte"
    )


def _episode_index(episodes: list[dict], episode_number) -> int:
    for index, episode in enumerate(episodes):
        if str(episode["n"]) == str(episode_number):
            return index
    raise LinkDead(f"episodio {episode_number} non più presente sulla fonte")


def resolve(request: models.Request) -> Resolution:
    """Re-resolve the source and prepare the download, or explain why not.

    Raises ResolutionError subclasses; everything else is wrapped as LinkDead,
    because from the approver's point of view an unparseable source page and a
    404 are the same problem.
    """
    from app.jobs import job_manager

    common = dict(
        year=request.year,
        audio_languages=request.audio_languages,
        subtitle_languages=request.subtitle_languages,
        strict_audio=True,
    )

    try:
        if request.source == "animeunity":
            episode = _find_anime_episode(request)
            available = animeunity.get_episode_languages(episode["id"])
            _check_audio(request, available)
            _widen_to_everyone(common, request, available)

            def submit():
                return job_manager.submit_anime_episode(
                    request.external_id, episode, request.title,
                    anime_type=request.anime_type or "tv", **common,
                )

            return Resolution(available=available, submit=submit)

        domain = current_domain()

        if request.media_type == FILM:
            from app.core.film import get_film_languages

            available = get_film_languages(int(request.external_id), domain)
            _check_audio(request, available)
            _widen_to_everyone(common, request, available)

            def submit():
                return job_manager.submit_film(
                    int(request.external_id), request.title, domain, **common
                )

            return Resolution(available=available, submit=submit)

        # TV episode: the token and the episode list are fetched fresh. The ones
        # captured when the request was made are short-lived and long expired.
        from app.core.tv import get_episode_languages, get_token

        token = get_token(int(request.external_id), domain)
        episodes = _season_episodes(request, domain, token)
        index = _episode_index(episodes, request.episode_number)
        available = get_episode_languages(
            int(request.external_id), episodes[index]["id"], domain, token
        )
        _check_audio(request, available)
        _widen_to_everyone(common, request, available)

        def submit():
            return job_manager.submit_episode(
                int(request.external_id), episodes, index, domain, token,
                request.title, request.season or 1, **common,
            )

        return Resolution(available=available, submit=submit)

    except ResolutionError:
        raise
    except Exception as exc:
        logger.warning("Resolution failed for request %s: %s", request.id, exc)
        raise LinkDead(str(exc)) from exc
