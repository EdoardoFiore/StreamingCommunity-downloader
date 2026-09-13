"""What an episode row carries, and whether its file is already on disk.

get_info_season used to keep {id, n, name} out of a payload that had already
been fetched, and the detail view's episode list needs the rest of it.
"""

import os

import pytest

from app.core import naming, paths, tv
from app.requests import resolver
from app.routers.tv import _mark_in_library


# ── The widened projection ─────────────────────────────────────────────────────

def test_an_episode_carries_what_the_list_shows():
    out = tv._episode({
        "id": 12, "number": "3", "name": "Il ritorno",
        "plot": "Qualcosa accade.", "duration": 47,
        "images": [{"type": "cover", "filename": "abc.jpg"}],
    })

    assert out == {
        "id": 12, "n": "3", "name": "Il ritorno",
        "plot": "Qualcosa accade.", "duration": 47,
        "still": "/api/image/abc.jpg",
    }


def test_a_missing_field_does_not_take_the_season_down():
    """These are a third party's field names. One rename must cost a synopsis,
    not the whole episode list."""
    out = tv._episode({"id": 9, "number": "1"})

    assert out["id"] == 9 and out["n"] == "1"
    assert out["name"] is None and out["plot"] is None
    assert out["duration"] is None and out["still"] is None


def test_an_empty_plot_is_reported_as_absent_not_as_an_empty_string():
    assert tv._episode({"id": 1, "number": "1", "plot": ""})["plot"] is None


def test_the_still_goes_through_the_image_proxy():
    """Never a CDN URL in the page: the host is resolved server-side."""
    out = tv._episode({"id": 1, "number": "1",
                       "images": [{"type": "background", "filename": "x.jpg"}]})
    assert out["still"] == "/api/image/x.jpg"


# ── The library check ──────────────────────────────────────────────────────────

@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(resolver, "library_dir", lambda media_type: str(tmp_path))
    return tmp_path


def _write(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(b"x")


def test_an_episode_already_on_disk_is_flagged(library):
    _write(paths.episode_path(str(library), "Serie", 1, "2", "2020"))
    episodes = [{"id": 1, "n": "1"}, {"id": 2, "n": "2"}]

    _mark_in_library(episodes, "Serie", 1, "2020")

    assert [e["in_library"] for e in episodes] == [False, True]


def test_the_mkv_sibling_counts(library):
    """The downloader remuxes to .mkv as soon as there is a second audio track."""
    base = paths.episode_path(str(library), "Serie", 1, "1", "2020")
    _write(os.path.splitext(base)[0] + ".mkv")
    episodes = [{"id": 1, "n": "1"}]

    _mark_in_library(episodes, "Serie", 1, "2020")

    assert episodes[0]["in_library"] is True


def test_a_file_written_under_the_legacy_template_still_counts(library, monkeypatch):
    """Changing a naming template must not hide files already in the library."""
    _write(paths.episode_path(str(library), "Serie", 1, "1", "2020",
                              naming.LEGACY_TEMPLATES))
    monkeypatch.setattr(naming, "templates", lambda: {
        **naming.DEFAULT_TEMPLATES, "episode_file": "{title} - {episode2}",
    })
    episodes = [{"id": 1, "n": "1"}]

    _mark_in_library(episodes, "Serie", 1, "2020")

    assert episodes[0]["in_library"] is True


def test_a_crafted_title_cannot_walk_out_of_the_library(library, tmp_path):
    """The title comes from the caller and reaches the filesystem. It only ever
    stats, but it must not stat outside the library either."""
    outside = tmp_path.parent / "outside.mp4"
    outside.write_bytes(b"x")
    episodes = [{"id": 1, "n": "1"}]

    _mark_in_library(episodes, "../" * 6 + "outside", 1, "")

    assert episodes[0]["in_library"] is False


def test_the_check_never_breaks_the_episode_list(library, monkeypatch):
    """A library on an unreachable mount costs the flag, not the page."""
    monkeypatch.setattr(paths, "episode_path",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("mount gone")))
    episodes = [{"id": 1, "n": "1"}]

    _mark_in_library(episodes, "Serie", 1, "2020")

    assert episodes[0]["in_library"] is False
