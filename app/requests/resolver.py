"""Resolving a request against the source, at the moment it is approved.

Days can pass between asking and approving. In that time the source domain can
change, the link can die and the available audio tracks can change. All of that
is re-checked here, against the *current* configuration — never against values
the client sent or values frozen at request time.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Callable

from app.config import DATA_FILE, VIDEOS_DIR
from app.core import animeunity, container, naming, paths, probe
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


def destination_path(request: models.Request, templates: dict | None = None) -> str:
    """Where this request's file would land, using the downloader's own layout.

    A folder a person has associated with the title wins, exactly as it does
    in the download itself (``library.destination``); otherwise an episode
    joins the series' folder already on disk (``series_home``). Not for an explicit
    ``templates``: that asks where the file would be under a given layout,
    which is a question about names, not about this title.
    """
    output_dir = library_dir(request.media_type)
    if templates is None:
        from app import library

        placed = library.destination(
            request.media_type, request.external_id, root=output_dir,
            season=request.season, episode=request.episode_number, year=request.year,
            single_file=_single_file(request),
        )
        if placed:
            return placed
        if request.media_type == EPISODE or (request.media_type == ANIME
                                             and not _single_file(request)):
            return series_episode_path(
                request.media_type, request.external_id, output_dir, request.title,
                request.season or 1, request.episode_number or "1", request.year,
                anime_type=request.anime_type,
            )
    if request.media_type == FILM:
        return paths.film_path(output_dir, request.title, request.year, templates)
    if request.media_type == EPISODE:
        return paths.episode_path(
            output_dir, request.title, request.season or 1,
            request.episode_number or "1", request.year, templates,
        )
    return paths.anime_path(
        output_dir, request.title, request.episode_number or "1",
        request.anime_type or "tv", request.year, templates,
    )


def candidate_paths(*bases: str) -> list[str]:
    """Every path a file could be sitting at, given where it would land.

    Two axes. The container, because it is a setting now: a file downloaded
    before it was changed carries the other extension, and it must not become
    invisible for that. And the naming template, because changing that must not
    make files already in the library invisible either: they would be
    re-downloaded to the new name, leaving a duplicate of something that was
    already there — so callers pass the current template's path *and* the
    legacy one, current first, since a library that has been renamed should win
    over a legacy file left behind.

    One stat per known container, the configured extension first, so the file
    the panel would write today is the one looked for first. With the default
    configuration both templates render the same path and the deduplication
    collapses this back to the two stats it has always been.

    Takes rendered paths rather than a Request so the episode list can ask the
    same question without one. The rule lives here once; see existing_file()
    and the library check in app/routers/tv.py.
    """
    extensions = container.candidate_extensions()
    candidates = []
    for base in dict.fromkeys(bases):
        stem = os.path.splitext(base)[0]
        candidates.extend(stem + ext for ext in extensions)
    return list(dict.fromkeys(candidates))


def first_existing(*bases: str) -> str | None:
    """The file already occupying any of these destinations, if any."""
    return next((p for p in candidate_paths(*bases) if os.path.exists(p)), None)


_YEAR_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def _norm_year(year) -> str | None:
    text = str(year).strip() if year not in (None, "", 0) else ""
    return text or None


def series_years(output_dir: str, title: str, year,
                 folder_for: Callable = paths.series_folder,
                 exclude: set[str] = frozenset()) -> list[str | None]:
    """Every year this series' folder already exists under, canonical first.

    A series' folder is named after its year, and that year has not always been
    the right one: before issue #21 it came from ``last_air_date``, so each
    download named the folder after whichever season was latest at the time,
    and before there was a year at all it had none. One series ends up spread
    over ``I Simpson/``, ``I Simpson (2025)/`` and ``I Simpson (2026)/`` (#24),
    and a check that looks only at the canonical folder calls every episode in
    the other two missing — and downloads them again.

    Candidates are found by rendering the folder template with each year the
    library root mentions and keeping the renders that exist, so a custom
    template is honoured without parsing folder names back. A year *before*
    the canonical one is refused: the wrong years were always a later season's,
    never an earlier one, while an earlier year is what a remake's original
    looks like — "Shōgun (1980)" must not satisfy "Shōgun (2024)". The folder
    with no year is always accepted: that is simply an older version's layout.

    Returns just the canonical year when the root cannot be listed, so the
    check degrades to what it was rather than failing. ``folder_for`` is
    ``paths.anime_folder`` for an anime, whose folder has its own template.
    ``exclude`` holds the folders a person has said are not this title's
    (``library.rejected_folders``); a year whose folder is one of them is not
    offered, or a correction would last only until the next check.
    """
    canonical = _norm_year(year)
    years: list[str | None] = [canonical]
    try:
        with os.scandir(output_dir) as entries:
            names = {e.name for e in entries if e.is_dir()}
    except OSError:
        return years

    floor = int(canonical) if canonical and canonical.isdigit() else None
    found = sorted({y for name in names for y in _YEAR_RE.findall(name)}, reverse=True)
    layouts = (naming.templates(), naming.LEGACY_TEMPLATES)
    for candidate in [None, *found]:
        if candidate == canonical:
            continue
        if candidate is not None and floor is not None and int(candidate) < floor:
            continue
        rendered = {folder_for(title, candidate, t) for t in layouts}
        if rendered & names and not rendered <= exclude:
            years.append(candidate)
    return years


def episode_candidates(output_dir: str, title: str, season, episode_number,
                       years: list[str | None], container: str | None = None) -> list[str]:
    """Where an episode could be sitting: each year's folder, each layout.

    Canonical year first, then current template before legacy, so the file the
    panel would write today wins over one left behind in an older folder.
    """
    bases = []
    for year in years:
        for templates in (None, naming.LEGACY_TEMPLATES):
            bases.append(paths.episode_path(output_dir, title, season, episode_number,
                                            year, templates, container=container))
    return bases


def series_home(output_dir: str, title: str, year, season, *, anime: bool = False,
                exclude: set[str] = frozenset()) -> tuple[str | None, dict | None]:
    """The ``(year, templates)`` whose folder a new episode of this series joins.

    The series' folder already on disk, not the canonical one, when the two
    differ (#24): a series kept in ``Ted Lasso (2026)/`` got its next episode in
    a brand new ``Ted Lasso (2020)/`` — the right year, but Jellyfin then shows
    two series, one of them holding a single episode. Among the folders
    ``series_years`` finds, the one holding the most episodes of this season
    wins, then the one holding the most episodes at all, then the canonical,
    which comes first. Counting is what keeps one stray file from capturing the
    series: it was the fear of exactly that which made the canonical folder
    the only destination, and a folder with one episode loses to the one with
    forty. A season split across folders follows its larger half.

    Returns the canonical ``(year, None)`` when there is nothing to choose
    between. Never raises: a library that cannot be read is no reason for a
    download that has already fetched its bytes to fail.
    """
    from app import library

    folder_for = paths.anime_folder if anime else paths.series_folder
    canonical = (year or None, None)
    try:
        years = series_years(output_dir, title, year, folder_for, exclude=exclude)
        if len(years) < 2:
            return canonical
        try:
            key_season = int(season)
        except (TypeError, ValueError):
            key_season = None
        # A folder holding no episode never wins: an empty one is not where
        # the series lives, only where something once was or will be.
        best, best_score = canonical, (0, 0)
        for candidate in years:
            for templates in (None, naming.LEGACY_TEMPLATES):
                name = folder_for(title, candidate, templates)
                folder = os.path.join(output_dir, name)
                if name in exclude or not os.path.isdir(folder):
                    continue
                keys = library.episode_files([folder]).keys()
                score = (sum(1 for s, _ in keys if s == key_season), len(keys))
                # Strictly greater: on a tie the earlier candidate stays, and
                # the canonical year is listed first.
                if score > best_score:
                    best, best_score = (candidate, templates), score
                break
        return best
    except Exception:
        logger.exception("Cannot choose the folder of %s; using the canonical one", title)
        return canonical


def series_episode_path(media_type: str, external_id, output_dir: str, title: str,
                        season, episode_number, year, anime_type: str | None = None,
                        container: str | None = None) -> str:
    """Where a new episode lands when no person has decided: see ``series_home``.

    Built with the same ``paths`` builders the check probes, under the year and
    layout chosen, so the download and the check cannot disagree about it.
    """
    from app import library

    anime = media_type == ANIME
    try:
        exclude = library.rejected_folders(library.SOURCE_BY_KIND[media_type],
                                           media_type, external_id)
    except Exception:
        logger.exception("Cannot read the rejected folders of %s", external_id)
        exclude = set()
    home_year, templates = series_home(output_dir, title, year, 1 if anime else season,
                                       anime=anime, exclude=exclude)
    if anime:
        return paths.anime_path(output_dir, title, episode_number, anime_type or "tv",
                                home_year, templates, container=container)
    return paths.episode_path(output_dir, title, season, episode_number, home_year,
                              templates, container=container)


def _rejected(request: models.Request) -> set[str]:
    from app import library

    try:
        return library.rejected_folders(request.source, request.media_type, request.external_id)
    except Exception:
        logger.exception("Cannot read the rejected folders of %s", request.external_id)
        return set()


def _destinations(request: models.Request) -> list[str]:
    if request.media_type == EPISODE:
        output_dir = library_dir(EPISODE)
        return episode_candidates(
            output_dir, request.title, request.season or 1, request.episode_number or "1",
            series_years(output_dir, request.title, request.year, exclude=_rejected(request)),
        )
    if request.media_type == ANIME and paths.is_anime_series(request.anime_type):
        output_dir = library_dir(ANIME)
        years = series_years(output_dir, request.title, request.year, paths.anime_folder,
                             exclude=_rejected(request))
        return [
            paths.anime_path(output_dir, request.title, request.episode_number or "1",
                             request.anime_type or "tv", year, templates)
            for year in years for templates in (None, naming.LEGACY_TEMPLATES)
        ]
    return [
        destination_path(request),
        destination_path(request, templates=naming.LEGACY_TEMPLATES),
    ]


def _single_file(request: models.Request) -> bool:
    """A film or an anime film: one file, straight in its folder."""
    return request.media_type == FILM or (
        request.media_type == ANIME and not paths.is_anime_series(request.anime_type)
    )


def _registered_file(request: models.Request) -> str | None:
    """The file the registry's folders hold for this request. Never raises:
    the name-based check has already answered, and this only adds to it."""
    from app import library

    single_file = _single_file(request)
    try:
        return library.find(
            request.source, request.media_type, request.external_id,
            library_dir(request.media_type),
            season=request.season, episode=request.episode_number, year=request.year,
            single_file=single_file,
        )
    except Exception:
        logger.exception("Library registry lookup failed for %s %s",
                         request.media_type, request.external_id)
        return None


def existing_file(request: models.Request) -> str | None:
    """The file already occupying this request's destination, if any.

    Where the name says it should be first, then the folders the registry has
    filed under this title (``app.library``), which find it even when the
    title or the year it was saved under has changed since. For an episode the
    first step includes the same series' folders under other years — see
    ``series_years`` — which is what covers a library older than the registry.

    A new download goes to ``destination_path``: the folder a person associated
    with the title, else — for an episode — the series' folder already on disk
    that holds most of it, so the library does not grow another folder.
    """
    return first_existing(*_destinations(request)) or _registered_file(request)


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


def _tmdb_id_for(request, media_type: str) -> int | None:
    """The title's TMDB id, for the stream fallback. Best effort.

    Approval is already doing network work, so one more lookup is affordable
    here in a way it is not on the direct download path. It must never be the
    reason an approval fails: without an id there is simply no fallback, which
    is the behaviour there has always been.
    """
    from app.core import metadata

    try:
        return metadata.title_metadata(
            media_type, request.external_id, request.slug or "", ""
        ).get("tmdb_id")
    except Exception:
        logger.info("No tmdb_id for request %s; the fallback will be unavailable", request.id)
        return None


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

    # What the file already holds counts too. A re-download replaces it, so a
    # track that is present but no longer named by any live request — the one
    # request that asked for it was denied afterwards — would disappear without
    # anyone being told. Untagged audio is skipped: it names no language, and
    # asking the source for "und" would find nothing.
    existing = existing_file(request)
    if existing is not None:
        present = probe.media_languages(existing)
        if present is not None:
            audio = sorted(set(audio) | (present["audio"] - {"und"}))
            subtitles = sorted(set(subtitles) | (present["subtitles"] - {"und"}))

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
            tmdb_id = _tmdb_id_for(request, "movie")

            def submit():
                return job_manager.submit_film(
                    int(request.external_id), request.title, domain,
                    tmdb_id=tmdb_id, **common
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

        tmdb_id = _tmdb_id_for(request, "tv")

        def submit():
            return job_manager.submit_episode(
                int(request.external_id), episodes, index, domain, token,
                request.title, request.season or 1, tmdb_id=tmdb_id, **common,
            )

        return Resolution(available=available, submit=submit)

    except ResolutionError:
        raise
    except Exception as exc:
        logger.warning("Resolution failed for request %s: %s", request.id, exc)
        raise LinkDead(str(exc)) from exc
