"""Destination paths for downloaded media.

Extracted from film.py, tv.py and animeunity.py, which each built the same
layout inline. The request system has to know where a download *would* land in
order to check whether it is already in the library, and a second copy of that
logic would silently drift from the downloader's.
"""

import os

from app.core.headers import sanitize_filename


def fmt_ep(n) -> str:
    """Format an episode number with a zero-padded integer part ('7' → '07')."""
    s = str(n)
    parts = s.split(".", 1)
    return parts[0].zfill(2) if len(parts) == 1 else parts[0].zfill(2) + "." + parts[1]


def clean_title(name: str) -> str:
    return sanitize_filename(str(name).replace("+", " ").replace(",", ""))


def _with_year(name: str, year) -> str:
    return f"{name} ({year})" if year else name


def film_path(output_dir: str, title: str, year=None) -> str:
    """videos/Title (YYYY)/Title (YYYY).mp4"""
    name = clean_title(title)
    folder = _with_year(name, year)
    return os.path.join(output_dir, folder, folder + ".mp4")


def episode_path(output_dir: str, tv_name: str, season: int, episode_number, year=None) -> str:
    """videos/Series (YYYY)/Season 01/Series S01E01.mp4"""
    name = sanitize_filename(tv_name)
    folder = _with_year(name, year)
    filename = f"{name} S{season:02d}E{fmt_ep(episode_number)}.mp4"
    return os.path.join(output_dir, folder, f"Season {season:02d}", filename)


def is_anime_series(anime_type: str) -> bool:
    return (anime_type or "tv").lower() in ("tv", "serie", "series", "anime")


def anime_path(output_dir: str, anime_name: str, episode_number,
               anime_type: str = "tv", year=None) -> str:
    """Series: .../Name (YYYY)/Season 01/Name S01E01.mp4 — movies: .../Name (YYYY)/Name.mp4"""
    name = clean_title(anime_name)
    folder = _with_year(name, year)
    if is_anime_series(anime_type):
        return os.path.join(
            output_dir, folder, "Season 01", f"{name} S01E{fmt_ep(episode_number)}.mp4"
        )
    return os.path.join(output_dir, folder, f"{name}.mp4")
