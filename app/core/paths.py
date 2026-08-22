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

from app.core import naming
from app.core.headers import sanitize_filename


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
