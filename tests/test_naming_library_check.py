"""Paths under templates, and the library check that has to survive changing them.

Two obligations pull against each other here.

The default templates must produce exactly the paths the panel produced before
templates existed, byte for byte — otherwise every file already in every library
becomes invisible the moment this ships.

And once someone *does* change a template, files written under the old one must
still count as present. Otherwise the request system re-downloads them under the
new name and leaves a duplicate of something that was already there.
"""

import os

import pytest

from app.auth.permissions import ALL_PERMISSIONS
from app.core import naming, paths
from app.requests import models, resolver
from tests.conftest import do_setup, make_user, session_for


# ── The anchor: defaults reproduce the old layout exactly ─────────────────────
#
# These strings are the layout as it shipped. They are written out literally
# rather than computed, so that a change to the engine has to come here and
# argue with a human instead of quietly agreeing with itself.
#
# container="mp4" is passed explicitly rather than left to the setting. The
# extension is a setting now, and what these anchor is the folder and the stem
# — the parts the templates own. Which container the default picks is asserted
# once, on its own, by test_the_default_container_decides_the_extension.

@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"title": "Blade Runner", "year": "1982"},
         "Blade Runner (1982)/Blade Runner (1982).mp4"),
        ({"title": "Blade Runner", "year": None}, "Blade Runner/Blade Runner.mp4"),
        # clean_title strips commas and turns + into a space, for films.
        ({"title": "Fast, Furious + More", "year": "2001"},
         "Fast Furious   More (2001)/Fast Furious   More (2001).mp4"),
    ],
)
def test_film_paths_are_unchanged(kwargs, expected):
    assert paths.film_path("/lib", **kwargs, container="mp4") == os.path.join(
        "/lib", *expected.split("/"))


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"tv_name": "Dark", "season": 1, "episode_number": "7", "year": "2017"},
         "Dark (2017)/Season 01/Dark S01E07.mp4"),
        ({"tv_name": "Dark", "season": 12, "episode_number": "10", "year": None},
         "Dark/Season 12/Dark S12E10.mp4"),
        # fmt_ep pads the integer part only.
        ({"tv_name": "Dark", "season": 2, "episode_number": "7.5", "year": None},
         "Dark/Season 02/Dark S02E07.5.mp4"),
        # A series keeps its comma; a film would not. Preserved deliberately.
        ({"tv_name": "Law, Order", "season": 1, "episode_number": "1", "year": None},
         "Law, Order/Season 01/Law, Order S01E01.mp4"),
    ],
)
def test_episode_paths_are_unchanged(kwargs, expected):
    assert paths.episode_path("/lib", **kwargs, container="mp4") == os.path.join(
        "/lib", *expected.split("/"))


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"anime_name": "Naruto", "episode_number": "12", "year": "2002"},
         "Naruto (2002)/Season 01/Naruto S01E12.mp4"),
        ({"anime_name": "Akira", "episode_number": "1", "anime_type": "movie",
          "year": "1988"}, "Akira (1988)/Akira.mp4"),
    ],
)
def test_anime_paths_are_unchanged(kwargs, expected):
    assert paths.anime_path("/lib", **kwargs, container="mp4") == os.path.join(
        "/lib", *expected.split("/"))


def test_the_default_container_decides_the_extension():
    """The one thing the anchors above deliberately stop asserting."""
    assert paths.film_path("/lib", "Blade Runner", "1982").endswith(".mkv")
    assert paths.episode_path("/lib", "Dark", 1, "7", "2017").endswith(".mkv")
    assert paths.anime_path("/lib", "Naruto", "12", year="2002").endswith(".mkv")


def test_the_legacy_templates_produce_the_same_paths_as_the_defaults():
    """The library-check fallback is worthless if these two ever disagree."""
    for path_fn, args in (
        (paths.film_path, ("/lib", "Blade Runner", "1982")),
        (paths.anime_path, ("/lib", "Naruto", "12")),
    ):
        assert path_fn(*args) == path_fn(*args, templates=naming.LEGACY_TEMPLATES)

    assert paths.episode_path("/lib", "Dark", 1, "7", "2017") == paths.episode_path(
        "/lib", "Dark", 1, "7", "2017", templates=naming.LEGACY_TEMPLATES
    )


# ── Custom templates ──────────────────────────────────────────────────────────

CUSTOM = {**naming.DEFAULT_TEMPLATES,
          "episode_file": "{title} {season}x{episode2}",
          "season_folder": "Stagione {season}"}


def test_a_custom_template_changes_the_path():
    result = paths.episode_path("/lib", "Dark", 1, "7", "2017", templates=CUSTOM,
                                container="mp4")
    assert result == os.path.join("/lib", "Dark (2017)", "Stagione 1", "Dark 1x07.mp4")


def test_a_hostile_template_cannot_escape_the_library():
    """Validation refuses these on save; render() has to hold anyway."""
    hostile = {**naming.DEFAULT_TEMPLATES, "film_file": "../../etc/passwd"}
    result = paths.film_path("/lib", "X", None, templates=hostile)

    assert result.startswith("/lib" + os.sep)
    assert os.path.normpath(result).startswith("/lib" + os.sep)


def test_a_hostile_title_cannot_escape_the_library():
    result = paths.film_path("/lib", "../../etc/passwd", None)
    assert os.path.normpath(result).startswith("/lib" + os.sep)


# ── The library check ─────────────────────────────────────────────────────────

@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(resolver, "library_dir", lambda media_type: str(tmp_path))
    return tmp_path


def _request(**overrides):
    fields = dict(
        id=1, content_key="k", source="streamingcommunity", media_type="episode",
        external_id="1", slug="dark", title="Dark", year="2017", season=1,
        episode_number="7", anime_type=None, poster=None,
        audio_languages=["ita"], subtitle_languages=[],
        status="pending", requested_by=1, job_id=None, output_path=None,
        problem=None, created_at="", updated_at="",
        available_snapshot=None, denial_reason=None, decided_by=None, decided_at=None,
    )
    fields.update(overrides)
    return models.Request(**{k: v for k, v in fields.items()
                             if k in models.Request.__dataclass_fields__})


def _write(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()
    return path


def test_a_file_written_under_the_old_template_still_counts(library, monkeypatch):
    """The headline requirement.

    Without this, changing a template makes every existing episode look missing,
    and the queue quietly re-downloads a library it already has.
    """
    legacy = _write(str(library / "Dark (2017)" / "Season 01" / "Dark S01E07.mkv"))

    from app import config
    config.save_settings({**config.get_settings(), "naming_templates": {
        "episode_file": "{title} {season}x{episode2}",
    }})

    assert resolver.existing_file(_request()) == legacy


def test_the_current_template_wins_over_a_legacy_leftover(library):
    from app import config
    config.save_settings({**config.get_settings(), "naming_templates": {
        "episode_file": "{title} {season}x{episode2}",
    }})

    _write(str(library / "Dark (2017)" / "Season 01" / "Dark S01E07.mkv"))
    current = _write(str(library / "Dark (2017)" / "Season 01" / "Dark 1x07.mkv"))

    assert resolver.existing_file(_request()) == current


def test_the_mkv_sibling_is_found(library):
    """A file in the other container still counts as present.

    The container is a setting, so anything downloaded before it was changed
    carries the other extension. Missing it would re-download a title that is
    already there and leave two copies side by side.
    """
    mkv = _write(str(library / "Dark (2017)" / "Season 01" / "Dark S01E07.mkv"))
    assert resolver.existing_file(_request()) == mkv


def test_the_other_container_is_found_too(library, monkeypatch):
    """The same, from the other side: configured mp4, an old mkv on disk."""
    from app import config
    config.save_settings({**config.get_settings(), "output_container": "mp4"})

    mp4 = _write(str(library / "Dark (2017)" / "Season 01" / "Dark S01E07.mp4"))
    assert resolver.existing_file(_request()) == mp4


def test_nothing_on_disk_is_nothing(library):
    assert resolver.existing_file(_request()) is None


# ── One series, several year folders (#24) ──────────────────────────────────
#
# Before #21 a series' folder was named after its latest season's year, and
# before that after no year at all, so one series can sit in three folders.

def _simpsons(library, folder, episode):
    return _write(str(library / folder / "Season 10" / f"I Simpson S10E{episode:02d}.mkv"))


def _simpsons_request(**overrides):
    return _request(**{"title": "I Simpson", "year": "1989", "season": 10, **overrides})


def test_an_episode_in_a_later_years_folder_counts(library):
    stray = _simpsons(library, "I Simpson (2025)", 4)
    assert resolver.existing_file(_simpsons_request(episode_number="4")) == stray


def test_an_episode_in_the_yearless_folder_counts(library):
    stray = _simpsons(library, "I Simpson", 1)
    assert resolver.existing_file(_simpsons_request(episode_number="1")) == stray


def test_the_canonical_folder_wins_over_a_stray_one(library):
    _simpsons(library, "I Simpson (2026)", 2)
    canonical = _simpsons(library, "I Simpson (1989)", 2)
    assert resolver.existing_file(_simpsons_request(episode_number="2")) == canonical


def test_a_new_episode_joins_the_folder_the_series_is_in(library):
    """#24: a new canonical folder next to the real one is a second series in
    Jellyfin, holding one episode."""
    _simpsons(library, "I Simpson (2025)", 1)
    assert "I Simpson (2025)" in resolver.destination_path(_simpsons_request(episode_number="5"))


def test_the_folder_holding_the_season_wins_over_a_stray_episode(library):
    """Ted Lasso: the whole series in (2026), one episode already sent to the
    canonical (2020). The next one goes where the season is, not where the
    stray landed."""
    for n in range(1, 9):
        _write(str(library / "Ted Lasso (2026)" / "Season 04" / f"Ted Lasso S04E{n:02d}.mkv"))
    _write(str(library / "Ted Lasso (2020)" / "Season 04" / "Ted Lasso S04E09.mkv"))
    request = _request(title="Ted Lasso", year="2020", season=4, episode_number="10")

    assert resolver.destination_path(request) == str(
        library / "Ted Lasso (2026)" / "Season 04" / "Ted Lasso S04E10.mkv")


def test_on_a_tie_the_canonical_folder_wins(library):
    _simpsons(library, "I Simpson (1989)", 1)
    _simpsons(library, "I Simpson (2025)", 2)
    assert "I Simpson (1989)" in resolver.destination_path(_simpsons_request(episode_number="5"))


def test_an_empty_year_folder_does_not_attract_downloads(library):
    (library / "I Simpson (2025)").mkdir()
    assert "I Simpson (1989)" in resolver.destination_path(_simpsons_request(episode_number="5"))


def test_a_rejected_folder_is_never_the_destination(client, library):
    from app import library as registry

    _simpsons(library, "I Simpson (2025)", 1)
    registry.record("streamingcommunity", "episode", "1", "I Simpson (2025)", "rejected")
    assert "I Simpson (1989)" in resolver.destination_path(_simpsons_request(episode_number="5"))


def test_the_download_lands_where_the_check_looks(library):
    """The downloader builds its path through the same function, so the next
    check finds the file it just wrote."""
    _simpsons(library, "I Simpson (2025)", 1)
    written = resolver.series_episode_path("episode", "1", str(library), "I Simpson", 10, "5", "1989")
    _write(written)
    assert resolver.existing_file(_simpsons_request(episode_number="5")) == written


def test_an_earlier_year_is_a_different_series(library):
    """A wrong year was always a later season's. An earlier one is a remake's
    original, and it must not satisfy the remake."""
    _write(str(library / "Shogun (1980)" / "Season 01" / "Shogun S01E01.mkv"))
    request = _request(title="Shogun", year="2024", season=1, episode_number="1")
    assert resolver.existing_file(request) is None


def test_a_longer_title_is_a_different_series(library):
    _write(str(library / "I Simpson Show (2025)" / "Season 10" / "I Simpson S10E01.mkv"))
    assert resolver.existing_file(_simpsons_request(episode_number="1")) is None


def test_with_no_year_known_every_year_folder_counts(library):
    stray = _simpsons(library, "I Simpson (2025)", 3)
    assert resolver.existing_file(_simpsons_request(year=None, episode_number="3")) == stray


def test_a_custom_folder_template_is_matched_by_rendering_it(library):
    from app import config
    config.save_settings({**config.get_settings(), "naming_templates": {
        "series_folder": "{title}[ - {year}]",
    }})
    stray = _simpsons(library, "I Simpson - 2025", 6)
    assert resolver.existing_file(_simpsons_request(episode_number="6")) == stray


def test_the_episode_list_sees_the_union_of_the_folders(library):
    """The issue's own example: seasons split across folders, all present."""
    from app.routers.tv import _mark_in_library

    for n in (1, 2, 3):
        _simpsons(library, "I Simpson", n)
    for n in (4, 5):
        _simpsons(library, "I Simpson (2025)", n)
    (library / "I Simpson (2026)").mkdir()
    episodes = [{"n": str(n)} for n in range(1, 7)]

    _mark_in_library(episodes, "I Simpson", 10, "1989")

    assert [e["in_library"] for e in episodes] == [True] * 5 + [False]


def test_an_unlistable_library_degrades_to_the_canonical_folder(library, monkeypatch):
    def refuse(path):
        raise PermissionError(path)
    monkeypatch.setattr(os, "scandir", refuse)

    assert resolver.series_years(str(library), "I Simpson", "1989") == ["1989"]


def test_the_default_configuration_costs_no_extra_stats(library, monkeypatch):
    """The dedup must collapse the cross product back to what it always was.

    Two axes, one stat each: the naming template (current and legacy, identical
    under the defaults) and the container (one per known container). A third
    container, or a third extension probed per container, has to come here and
    argue with a human before it multiplies the stats on every library check.
    """
    checked = []
    real_exists = os.path.exists
    monkeypatch.setattr(
        os.path, "exists",
        lambda p: checked.append(p) or real_exists(p),
    )

    resolver.existing_file(_request())

    assert len(checked) == 2
    assert len(set(checked)) == 2


# ── Settings API ──────────────────────────────────────────────────────────────


@pytest.fixture
def admin(client, admin_credentials):
    do_setup(client, admin_credentials)
    user = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    client.cookies.clear()
    return user, session_for(client, user.id)


def _csrf(admin):
    return {"X-CSRF-Token": admin[1]}


def test_the_settings_endpoint_always_returns_all_nine_slots(client, admin):
    """A half-filled form reads as "no rule", not as "the default rule"."""
    body = client.get("/api/domain/settings").json()
    assert set(body["naming_templates"]) == set(naming.DEFAULT_TEMPLATES)


def test_a_valid_template_is_saved(client, admin):
    res = client.put("/api/domain/settings", headers=_csrf(admin), json={
        "naming_templates": {**naming.DEFAULT_TEMPLATES,
                             "episode_file": "{title} {season}x{episode2}"},
    })
    assert res.status_code == 200
    assert naming.templates()["episode_file"] == "{title} {season}x{episode2}"


def test_an_invalid_template_is_a_422_naming_the_problem(client, admin):
    res = client.put("/api/domain/settings", headers=_csrf(admin), json={
        "naming_templates": {"film_file": "{title}/{year}"},
    })
    assert res.status_code == 422
    assert "percorso" in str(res.json()["detail"])


def test_saving_a_template_does_not_disturb_the_other_settings(client, admin):
    from app import config

    before = config.get_settings()["max_concurrent_downloads"]
    client.put("/api/domain/settings", headers=_csrf(admin),
               json={"naming_templates": {"film_file": "{title}"}})

    assert config.get_settings()["max_concurrent_downloads"] == before


def test_the_preview_uses_the_real_engine(client, admin):
    body = client.post("/api/domain/settings/naming-preview", headers=_csrf(admin), json={
        "templates": {"episode_file": "{title} {season}x{episode2}"},
    }).json()

    assert body["slots"]["episode_file"]["preview"] == "Titolo 1x07"
    assert body["slots"]["episode_file"]["error"] is None


def test_the_preview_reports_the_error_instead_of_a_result(client, admin):
    body = client.post("/api/domain/settings/naming-preview", headers=_csrf(admin), json={
        "templates": {"film_file": "{title} {quality}"},
    }).json()

    assert body["slots"]["film_file"]["preview"] is None
    assert "quality" in body["slots"]["film_file"]["error"]


def test_an_unknown_slot_in_a_preview_is_ignored(client, admin):
    body = client.post("/api/domain/settings/naming-preview", headers=_csrf(admin), json={
        "templates": {"nonsense": "{title}"},
    }).json()
    assert body["slots"] == {}


def test_an_anime_series_joins_its_folder_too(library):
    _write(str(library / "Naruto (2010)" / "Season 01" / "Naruto S01E01.mkv"))
    request = _request(source="animeunity", media_type="anime", external_id="5",
                       title="Naruto", year="2002", season=None, episode_number="2",
                       anime_type="tv")
    assert "Naruto (2010)" in resolver.destination_path(request)
