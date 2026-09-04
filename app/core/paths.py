"""Destination paths for downloaded media.

Extracted from film.py, tv.py and animeunity.py, which each built the same
layout inline. The request system has to know where a download *would* land in
order to check whether it is already in the library, and a second copy of that
logic would silently drift from the downloader's.

The names themselves come from templates now (see app/core/naming.py), but the
three signatures here are unchanged: they are the shared contract with
``resolver.destination_path()``, and that agreement is the whole reason this
module exists. ``templates=`` is the one addition, and it is what lets the
library check ask "where would this have landed under the *old* layout?" without
a second implementation of any of it.
"""

import os
import re

from app.core import naming
from app.core.headers import sanitize_filename

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

# Named rather than inlined so the tests can point them at a temp file: the
# only other way to exercise the detection is to run the suite in a container.
_DOCKERENV_FILE = "/.dockerenv"
_CGROUP_FILE = "/proc/1/cgroup"
_CONTAINER_MARKS = ("docker", "containerd", "kubepods", "libpod")

# Where the shipped compose mounts the media volume, and what VIDEOS_DIR is set
# to there. Named in the advice because "a path inside the container" is not
# something anyone can guess: the answer is one specific directory, and telling
# people to work it out from the compose file is how the reported deployment
# ended up with the host path in the first place.
CONTAINER_VIDEOS_DIR = "/app/videos"


def _host_is_windows() -> bool:
    """Whether this machine genuinely uses Windows paths.

    A function rather than ``os.name == "nt"`` inline, so the tests have a seam:
    they must be able to describe a Linux deployment while themselves running
    on Windows, and patching ``os.name`` globally makes ``pathlib`` hand out
    PosixPath objects the platform cannot instantiate.
    """
    return os.name == "nt"


def _in_container() -> bool:
    """Whether this looks like a container, so the advice can name the volume.

    Only consulted once the path is already known to be wrong, so the cost of
    reading two files does not land on any download that is fine. ``/.dockerenv``
    is what Docker itself drops in; the cgroup scan catches Podman, containerd
    and Kubernetes, where that file does not exist.
    """
    if os.path.exists(_DOCKERENV_FILE):
        return True
    try:
        with open(_CGROUP_FILE, encoding="utf-8", errors="replace") as handle:
            # Read once into a name. Calling handle.read() inside the genexp
            # would leave the file at EOF after the first mark, so every mark
            # but "docker" would be tested against an empty string.
            cgroups = handle.read()
    except OSError:
        # No /proc at all, which is every non-Linux host.
        return False
    return any(mark in cgroups for mark in _CONTAINER_MARKS)


def looks_like_windows_path(path: str) -> bool:
    r"""A drive letter or a UNC share: ``N:\Jellyfin`` or ``\\nas\media``."""
    text = (path or "").strip()
    return bool(_WINDOWS_DRIVE_RE.match(text)) or text.startswith("\\\\")


def windows_path_problem(path: str) -> str | None:
    r"""Why *path* cannot work on this host, or None when it can.

    A Windows path configured on a POSIX host is the one library
    misconfiguration that fails late and reports the wrong thing.
    ``os.makedirs`` accepts it, because a backslash is an ordinary character in
    a Linux filename, and quietly creates a single directory literally called
    ``N:\Jellyfin\Anime`` next to the app. Nothing complains until FFmpeg is
    handed the name at the very end of the download and answers "Protocol not
    found" — after every segment has already been fetched.

    Nothing is said about a Windows path on a Windows host: a manual install
    there is the case this must not get in the way of, and ``N:\Jellyfin\Anime``
    is simply correct.

    The advice differs by deployment, because the fix does. In a container the
    host path belongs on the left of the volume mapping and the panel needs the
    right-hand side; on bare-metal Linux there is no mapping to point at and the
    answer is just a local absolute path.
    """
    text = (path or "").strip()
    if _host_is_windows() or not looks_like_windows_path(text):
        return None
    if _in_container():
        return (
            f"«{text}» è un percorso Windows, ma il pannello gira in un container Linux. "
            f"Indica il percorso interno al container: «{CONTAINER_VIDEOS_DIR}/<cartella "
            f"nel volume>», per esempio «{CONTAINER_VIDEOS_DIR}/Anime». Il percorso "
            "dell'host va nel docker-compose, non qui: è la parte a sinistra dei due punti "
            f"nel volume montato su «{CONTAINER_VIDEOS_DIR}»."
        )
    return (
        f"«{text}» è un percorso Windows, ma il pannello gira su Linux. "
        "Indica un percorso assoluto del filesystem locale, per esempio "
        "«/srv/media/anime»."
    )


def validate_library_path(path: str) -> str:
    """Return the trimmed path, or raise ValueError explaining what is wrong."""
    text = (path or "").strip()
    if not text:
        raise ValueError("Il percorso della libreria non può essere vuoto.")
    problem = windows_path_problem(text)
    if problem:
        raise ValueError(problem)
    return text


def fmt_ep(n) -> str:
    """Format an episode number with a zero-padded integer part ('7' → '07')."""
    s = str(n)
    parts = s.split(".", 1)
    return parts[0].zfill(2) if len(parts) == 1 else parts[0].zfill(2) + "." + parts[1]


def clean_title(name: str) -> str:
    return sanitize_filename(str(name).replace("+", " ").replace(",", ""))


def _templates(overrides: dict | None) -> dict:
    return overrides if overrides is not None else naming.templates()


def _values(title: str, year=None, season=None, episode=None) -> dict:
    values = {"title": title, "year": "" if year in (None, "", 0) else str(year)}
    if season is not None:
        values["season"] = str(season)
        values["season2"] = f"{int(season):02d}"
    if episode is not None:
        values["episode"] = str(episode)
        values["episode2"] = fmt_ep(episode)
    return values


def film_path(output_dir: str, title: str, year=None, templates: dict | None = None) -> str:
    """videos/Title (YYYY)/Title (YYYY).mp4, or whatever the templates say."""
    tpl = _templates(templates)
    values = _values(clean_title(title), year)
    folder = naming.render("film_folder", tpl.get("film_folder"), values)
    stem = naming.render("film_file", tpl.get("film_file"), values)
    return os.path.join(output_dir, folder, stem + ".mp4")


def episode_path(output_dir: str, tv_name: str, season: int, episode_number,
                 year=None, templates: dict | None = None) -> str:
    """videos/Series (YYYY)/Season 01/Series S01E01.mp4, or whatever the templates say.

    The title is sanitised rather than cleaned, unlike films and anime: a series
    called "Law, Order" keeps its comma here and would lose it there. That
    asymmetry predates the templates and is preserved deliberately — changing it
    would rename every series folder already on disk.
    """
    tpl = _templates(templates)
    values = _values(sanitize_filename(tv_name), year, season, episode_number)
    folder = naming.render("series_folder", tpl.get("series_folder"), values)
    season_folder = naming.render("season_folder", tpl.get("season_folder"), values)
    stem = naming.render("episode_file", tpl.get("episode_file"), values)
    return os.path.join(output_dir, folder, season_folder, stem + ".mp4")


def is_anime_series(anime_type: str) -> bool:
    return (anime_type or "tv").lower() in ("tv", "serie", "series", "anime")


def anime_path(output_dir: str, anime_name: str, episode_number,
               anime_type: str = "tv", year=None, templates: dict | None = None) -> str:
    """Series: .../Name (YYYY)/Season 01/Name S01E01.mp4 — movies: .../Name (YYYY)/Name.mp4"""
    tpl = _templates(templates)
    # AnimeUnity has no season number: everything is season 1, which is what the
    # default templates hardcode and why {season} is still offered here.
    values = _values(clean_title(anime_name), year, 1, episode_number)
    folder = naming.render("anime_folder", tpl.get("anime_folder"), values)

    if is_anime_series(anime_type):
        season_folder = naming.render(
            "anime_season_folder", tpl.get("anime_season_folder"), values
        )
        stem = naming.render("anime_episode_file", tpl.get("anime_episode_file"), values)
        return os.path.join(output_dir, folder, season_folder, stem + ".mp4")

    stem = naming.render("anime_movie_file", tpl.get("anime_movie_file"), values)
    return os.path.join(output_dir, folder, stem + ".mp4")
