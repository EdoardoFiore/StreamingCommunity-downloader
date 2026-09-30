"""The library registry: which folder holds which title, by the title's id.

The name-based check finds a title by rebuilding its folder name, so a title
renamed at the source or a year corrected since makes a folder full of episodes
invisible. The registry files each finished download under the title's id, and
the check asks it second — see app/library.py.
"""

import os
from types import SimpleNamespace

import pytest

from app import jobs, library
from app.requests import models, resolver
from app.routers.tv import _mark_in_library


@pytest.fixture
def root(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(resolver, "library_dir", lambda media_type: str(lib))
    return lib


def _write(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()
    return str(path)


def _job(output_path, kind="episode", external_id="1234", title="I Simpson",
         year="1989", status="done"):
    return SimpleNamespace(job_id="j", type=kind, status=status, output_path=output_path,
                           external_id=external_id, media_label=title, year=year)


def _request(**overrides):
    fields = dict(
        id=1, content_key="k", source="streamingcommunity", media_type="episode",
        external_id="1234", slug="s", title="I Simpson", year="1989", season=10,
        episode_number="4", anime_type=None, poster=None,
        audio_languages=["ita"], subtitle_languages=[],
        status="pending", requested_by=1, job_id=None, output_path=None,
        problem=None, created_at="", updated_at="",
        available_snapshot=None, denial_reason=None, decided_by=None, decided_at=None,
    )
    fields.update(overrides)
    return models.Request(**{k: v for k, v in fields.items()
                             if k in models.Request.__dataclass_fields__})


# ── Filing ────────────────────────────────────────────────────────────────────

def test_a_finished_download_files_its_folder(root):
    path = _write(root / "I Simpson (1989)" / "Season 10" / "I Simpson S10E01.mkv")

    library.on_job_finished(_job(path))

    assert library.folders("streamingcommunity", "episode", "1234") == ["I Simpson (1989)"]


def test_the_series_older_folders_are_filed_with_it(root):
    """The issue's library: the first new episode claims the old folders too."""
    _write(root / "I Simpson" / "Season 10" / "I Simpson S10E01.mkv")
    _write(root / "I Simpson (2025)" / "Season 10" / "I Simpson S10E04.mkv")
    path = _write(root / "I Simpson (1989)" / "Season 10" / "I Simpson S10E06.mkv")

    library.on_job_finished(_job(path))

    assert set(library.folders("streamingcommunity", "episode", "1234")) == {
        "I Simpson (1989)", "I Simpson", "I Simpson (2025)",
    }
    # The one the panel wrote comes first.
    assert library.folders("streamingcommunity", "episode", "1234")[0] == "I Simpson (1989)"


def test_a_remakes_original_is_not_claimed(root):
    _write(root / "Shogun (1980)" / "Season 01" / "Shogun S01E01.mkv")
    path = _write(root / "Shogun (2024)" / "Season 01" / "Shogun S01E01.mkv")

    library.on_job_finished(_job(path, title="Shogun", year="2024"))

    assert library.folders("streamingcommunity", "episode", "1234") == ["Shogun (2024)"]


def test_a_film_claims_only_its_own_folder(root):
    """A film's other-year folder is as likely a remake as the same film."""
    _write(root / "Dune" / "Dune.mkv")
    path = _write(root / "Dune (2021)" / "Dune (2021).mkv")

    library.on_job_finished(_job(path, kind="film", title="Dune", year="2021"))

    assert library.folders("streamingcommunity", "film", "1234") == ["Dune (2021)"]


def test_an_anime_film_claims_only_its_own_folder(root):
    _write(root / "Akira" / "Akira.mkv")
    path = _write(root / "Akira (1988)" / "Akira.mkv")

    library.on_job_finished(_job(path, kind="anime", title="Akira", year="1988"))

    assert library.folders("animeunity", "anime", "1234") == ["Akira (1988)"]


@pytest.mark.parametrize("status", ["error", "cancelled"])
def test_only_a_finished_download_is_filed(root, status):
    path = _write(root / "I Simpson (1989)" / "Season 10" / "I Simpson S10E01.mkv")
    library.on_job_finished(_job(path, status=status))
    assert library.folders("streamingcommunity", "episode", "1234") == []


def test_a_file_outside_the_library_is_not_filed(root, tmp_path):
    path = _write(tmp_path / "elsewhere" / "I Simpson" / "Season 10" / "x.mkv")
    library.on_job_finished(_job(path))
    assert library.folders("streamingcommunity", "episode", "1234") == []


def test_a_job_with_no_id_is_not_filed(root):
    path = _write(root / "I Simpson (1989)" / "Season 10" / "I Simpson S10E01.mkv")
    library.on_job_finished(_job(path, external_id=None))
    assert library.folders("streamingcommunity", "episode", "1234") == []


def test_filing_twice_is_one_row(root):
    path = _write(root / "I Simpson (1989)" / "Season 10" / "I Simpson S10E01.mkv")
    library.on_job_finished(_job(path))
    library.on_job_finished(_job(path))
    assert library.folders("streamingcommunity", "episode", "1234") == ["I Simpson (1989)"]


# ── Finding ───────────────────────────────────────────────────────────────────

def test_a_series_renamed_at_the_source_is_still_found(root):
    """What the name-based check cannot do: the title in the folder and the
    file name is no longer the one the source uses."""
    old = _write(root / "I Simpson (1989)" / "Season 10" / "I Simpson S10E04.mkv")
    library.record("streamingcommunity", "episode", "1234", "I Simpson (1989)", "download")

    assert resolver.existing_file(_request(title="The Simpsons")) == old


def test_the_episode_number_decides_not_its_padding(root):
    old = _write(root / "Serie" / "Season 02" / "Serie S02E07.5.mkv")
    library.record("streamingcommunity", "episode", "1234", "Serie", "download")

    assert resolver.existing_file(_request(title="Altro", season=2, episode_number="7.5")) == old
    assert resolver.existing_file(_request(title="Altro", season=2, episode_number="7")) is None


def test_another_titles_folder_is_not_searched(root):
    _write(root / "I Simpson (1989)" / "Season 10" / "I Simpson S10E04.mkv")
    library.record("streamingcommunity", "episode", "999", "I Simpson (1989)", "download")

    assert resolver.existing_file(_request(title="The Simpsons")) is None


def test_a_renamed_film_is_found_by_its_folder(root):
    old = _write(root / "Il Padrino (1972)" / "Il Padrino (1972).mkv")
    library.record("streamingcommunity", "film", "1234", "Il Padrino (1972)", "download")

    request = _request(media_type="film", title="The Godfather", season=None,
                       episode_number=None, year="1972")
    assert resolver.existing_file(request) == old


def test_a_half_written_file_does_not_count(root):
    """The remux stages a dot-prefixed sibling in the library folder."""
    _write(root / "Il Padrino (1972)" / ".Il Padrino (1972).mkv")
    library.record("streamingcommunity", "film", "1234", "Il Padrino (1972)", "download")

    request = _request(media_type="film", title="The Godfather", season=None,
                       episode_number=None, year="1972")
    assert resolver.existing_file(request) is None


def test_an_anime_renamed_at_the_source_is_still_found(root):
    old = _write(root / "Naruto (2002)" / "Season 01" / "Naruto S01E12.mkv")
    library.record("animeunity", "anime", "55", "Naruto (2002)", "download")

    request = _request(source="animeunity", media_type="anime", external_id="55",
                       title="Naruto ITA", year="2002", season=None,
                       episode_number="12", anime_type="tv")
    assert resolver.existing_file(request) == old


def test_a_missing_registered_folder_is_skipped(root):
    library.record("streamingcommunity", "episode", "1234", "Sparita", "download")
    assert resolver.existing_file(_request(title="The Simpsons")) is None


def test_the_episode_list_uses_the_registry(root):
    for n in (1, 2):
        _write(root / "I Simpson (1989)" / "Season 10" / f"I Simpson S10E{n:02d}.mkv")
    library.record("streamingcommunity", "episode", "1234", "I Simpson (1989)", "download")
    episodes = [{"n": str(n)} for n in (1, 2, 3)]

    _mark_in_library(episodes, "The Simpsons", 10, "1989", tv_id=1234)

    assert [e["in_library"] for e in episodes] == [True, True, False]


def test_a_broken_registry_does_not_break_the_check(root, monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(library, "folders", explode)

    assert resolver.existing_file(_request()) is None


@pytest.mark.parametrize("season, episode, expected", [
    (1, "07", (1, "7")),
    ("10", "4", (10, "4")),
    (2, "7.50", (2, "7.5")),
    (1, "7.0", (1, "7")),
    (1, "abc", None),
    (1, None, None),
])
def test_episode_keys_agree_across_padding(season, episode, expected):
    assert library.episode_key(season, episode) == expected


# ── The job carries the id ────────────────────────────────────────────────────

@pytest.mark.parametrize("params, expected", [
    ({"id": 12}, "12"),
    ({"tv_id": 34}, "34"),
    ({"anime_id": "56"}, "56"),
    ({}, None),
])
def test_a_scheduled_entry_yields_its_id(params, expected):
    assert jobs._schedule_external_id(params) == expected


# ── A person's decisions ──────────────────────────────────────────────────────

def _assoc(root, folder, media_type="episode", external_id="1234", source="streamingcommunity",
           **kwargs):
    (root / folder).mkdir(exist_ok=True)
    return library.associate(source, media_type, external_id, folder, **kwargs)


def test_an_automatic_filing_never_overrides_a_person(root):
    manual = _assoc(root, "Mia Cartella", season_offset=2)
    library.record("streamingcommunity", "episode", "1234", "Mia Cartella", "download",
                   title="Altro")

    after = library.get(manual.id)
    assert after.origin == "manual" and after.season_offset == 2


def test_a_rejected_pair_is_not_filed_again(root):
    """The reason a removal is a rejection rather than a delete."""
    _write(root / "I Simpson (2025)" / "Season 10" / "I Simpson S10E04.mkv")
    path = _write(root / "I Simpson (1989)" / "Season 10" / "I Simpson S10E06.mkv")
    library.on_job_finished(_job(path))
    wrong = next(a for a in library.for_title("streamingcommunity", "episode", "1234")
                 if a.folder == "I Simpson (2025)")

    library.reject(wrong.id)
    library.on_job_finished(_job(path))

    assert library.folders("streamingcommunity", "episode", "1234") == ["I Simpson (1989)"]
    assert library.get(wrong.id).origin == "rejected"


def test_a_rejected_folder_is_not_found_by_name_either(root):
    _write(root / "I Simpson (2025)" / "Season 10" / "I Simpson S10E04.mkv")
    request = _request(title="I Simpson", year="1989")
    assert resolver.existing_file(request) is not None

    _assoc(root, "I Simpson (2025)")
    library.reject(library.for_title("streamingcommunity", "episode", "1234")[0].id)

    assert resolver.existing_file(request) is None


def test_correcting_the_folder_rejects_the_old_pair(root):
    wrong = _assoc(root, "Shogun (2024)")
    (root / "Shogun (1980)").mkdir()

    fixed = library.edit(wrong.id, folder="Shogun (1980)")

    assert library.get(wrong.id).origin == "rejected"
    assert (fixed.folder, fixed.origin) == ("Shogun (1980)", "manual")


def test_correcting_the_id_hands_the_folder_to_another_title(root):
    wrong = _assoc(root, "Shogun (2024)", title="Shogun")

    fixed = library.edit(wrong.id, external_id="999")

    assert library.get(wrong.id).origin == "rejected"
    assert (fixed.external_id, fixed.folder, fixed.origin) == ("999", "Shogun (2024)", "manual")
    # Whose name the old one was is not the new id's.
    assert fixed.title is None


def test_changing_only_the_offsets_keeps_the_pair(root):
    _write(root / "Serie" / "Season 01" / "Serie S01E01.mkv")
    library.record("streamingcommunity", "episode", "1234", "Serie", "download")
    row = library.for_title("streamingcommunity", "episode", "1234")[0]

    edited = library.edit(row.id, season_offset=1)

    assert edited.id == row.id and edited.origin == "manual" and edited.season_offset == 1


def test_a_restored_rejection_is_the_panels_again(root):
    row = _assoc(root, "X")
    library.reject(row.id)

    assert library.restore(row.id) is True
    assert library.get(row.id) is None
    assert library.restore(row.id) is False


def test_associating_again_overrides_a_rejection(root):
    row = _assoc(root, "X")
    library.reject(row.id)

    again = _assoc(root, "X")

    assert again.id == row.id and again.origin == "manual"


@pytest.mark.parametrize("folder", ["", ".", "..", "../etc", "a/b", "a\\b", ".hidden", "missing"])
def test_only_an_existing_folder_name_is_accepted(root, folder):
    with pytest.raises(library.InvalidFolder):
        library.associate("streamingcommunity", "episode", "1", folder)


# ── Where the next file goes ──────────────────────────────────────────────────

def test_a_manual_folder_decides_where_an_episode_goes(root):
    _assoc(root, "I Simpson (1989)")
    request = _request(title="The Simpsons", year="1989", season=10, episode_number="7")

    assert resolver.destination_path(request) == str(
        root / "I Simpson (1989)" / "Season 10" / "I Simpson S10E07.mkv")


def test_an_automatic_filing_does_not_decide(root):
    library.record("streamingcommunity", "episode", "1234", "I Simpson (2025)", "matched")
    (root / "I Simpson (2025)").mkdir()
    request = _request(title="I Simpson", year="1989", season=10, episode_number="7")

    assert "I Simpson (1989)" in resolver.destination_path(request)


def test_anime_parts_land_in_one_series(root):
    """Attack on Titan: a separate title per part at the source, one series here."""
    _assoc(root, "L'attacco dei giganti (2013)", media_type="anime", external_id="30",
           source="animeunity", season_offset=2, episode_offset=12)
    request = _request(source="animeunity", media_type="anime", external_id="30",
                       title="Shingeki no Kyojin 3 Part 2", year="2019", season=None,
                       episode_number="1", anime_type="tv")

    expected = root / "L'attacco dei giganti (2013)" / "Season 03" / "L'attacco dei giganti S03E13.mkv"
    assert resolver.destination_path(request) == str(expected)

    _write(expected)
    assert resolver.existing_file(request) == str(expected)


def test_an_anime_part_in_season_one_keeps_the_anime_templates(root):
    _assoc(root, "Naruto", media_type="anime", external_id="5", source="animeunity")
    request = _request(source="animeunity", media_type="anime", external_id="5",
                       title="Naruto ITA", year="2002", season=None, episode_number="3",
                       anime_type="tv")

    assert resolver.destination_path(request) == str(
        root / "Naruto" / "Season 01" / "Naruto S01E03.mkv")


def test_a_manual_film_folder_is_where_the_film_goes(root):
    _assoc(root, "Il Padrino (1972)", media_type="film")
    request = _request(media_type="film", title="The Godfather", season=None,
                       episode_number=None, year="1972")

    assert resolver.destination_path(request) == str(
        root / "Il Padrino (1972)" / "Il Padrino (1972).mkv")


def test_a_manual_folder_that_vanished_falls_back_to_the_name(root):
    _assoc(root, "Sparita")
    (root / "Sparita").rmdir()
    request = _request(title="I Simpson", year="1989")

    assert "I Simpson (1989)" in resolver.destination_path(request)


def test_the_download_itself_uses_the_same_placement(root):
    """The check and the download must not disagree about where a file is."""
    _assoc(root, "L'attacco dei giganti", media_type="anime", external_id="30",
           source="animeunity", season_offset=1)

    placed = library.destination("anime", "30", root=str(root), episode="4")

    assert placed == str(root / "L'attacco dei giganti" / "Season 02" / "L'attacco dei giganti S02E04.mkv")


@pytest.mark.parametrize("episode, offset, expected", [
    ("1", 12, "13"), ("7.5", 2, "9.5"), ("3", -2, "1"), ("1", -5, None), ("x", 1, None),
])
def test_episode_numbers_shift(episode, offset, expected):
    assert library.shift_episode(episode, offset) == expected


# ── Folders renamed or removed outside the panel ──────────────────────────────

def _payload(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    return str(path)


def test_a_renamed_folder_is_followed_by_its_files_size(root):
    path = _payload(root / "I Simpson (1989)" / "Season 10" / "I Simpson S10E01.mkv", 1234)
    library.on_job_finished(_job(path))
    os.rename(root / "I Simpson (1989)", root / "The Simpsons")

    summary = library.reconcile()

    assert summary["renamed"] == 1
    assert library.folders("streamingcommunity", "episode", "1234") == ["The Simpsons"]


def test_two_folders_with_the_same_size_are_not_guessed_between(root):
    path = _payload(root / "A" / "Season 01" / "A S01E01.mkv", 999)
    library.on_job_finished(_job(path, title="A", year=None))
    os.rename(root / "A", root / "B")
    _payload(root / "C" / "copia.mkv", 999)

    library.reconcile()

    row = library.for_title("streamingcommunity", "episode", "1234")[0]
    assert row.folder == "A" and row.missing_since is not None


def test_a_removed_folder_is_forgotten_after_a_month(root, monkeypatch):
    from datetime import timedelta

    path = _payload(root / "Via" / "Season 01" / "Via S01E01.mkv", 10)
    library.on_job_finished(_job(path, title="Via", year=None))
    import shutil
    shutil.rmtree(root / "Via")
    (root / "Altro").mkdir()  # the library is readable and not empty

    library.reconcile()
    assert library.folders("streamingcommunity", "episode", "1234") == ["Via"]

    later = library._now() + timedelta(days=31)
    monkeypatch.setattr(library, "_now", lambda: later)
    assert library.reconcile()["forgotten"] == 1
    assert library.folders("streamingcommunity", "episode", "1234") == []


def test_an_empty_or_unreadable_library_concludes_nothing(root):
    """An unmounted volume looks exactly like a deleted collection."""
    path = _payload(root / "Via" / "Season 01" / "Via S01E01.mkv", 10)
    library.on_job_finished(_job(path, title="Via", year=None))
    import shutil
    shutil.rmtree(root / "Via")

    library.reconcile()

    assert library.for_title("streamingcommunity", "episode", "1234")[0].missing_since is None


def test_a_folder_that_came_back_is_no_longer_missing(root):
    path = _payload(root / "Via" / "Season 01" / "Via S01E01.mkv", 10)
    library.on_job_finished(_job(path, title="Via", year=None))
    os.rename(root / "Via", root / "Temp")
    _payload(root / "Temp" / "extra.mkv", 10)  # ambiguous: no rename
    library.reconcile()
    os.rename(root / "Temp", root / "Via")

    library.reconcile()

    assert library.for_title("streamingcommunity", "episode", "1234")[0].missing_since is None


# ── The API ───────────────────────────────────────────────────────────────────

@pytest.fixture
def admin(client, admin_credentials, root):
    from app.auth.permissions import ALL_PERMISSIONS
    from tests.conftest import do_setup, make_user, session_for

    do_setup(client, admin_credentials)
    user = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    client.cookies.clear()
    return {"X-CSRF-Token": session_for(client, user.id)}


def test_the_api_associates_lists_edits_and_removes(client, admin, root):
    (root / "Uno").mkdir()
    (root / "Due").mkdir()

    created = client.post("/api/library/associations", headers=admin, json={
        "source": "streamingcommunity", "media_type": "episode", "external_id": "77",
        "folder": "Uno", "title": "Serie",
    })
    assert created.status_code == 201, created.text
    assert created.json()["exists"] is True

    folders = client.get("/api/library/folders", params={"media_type": "episode"}).json()
    assert folders["folders"] == ["Due", "Uno"]

    edited = client.patch(f"/api/library/associations/{created.json()['id']}",
                          headers=admin, json={"folder": "Due"})
    assert edited.status_code == 200 and edited.json()["folder"] == "Due"

    listed = client.get("/api/library/associations").json()["associations"]
    assert {(a["folder"], a["origin"]) for a in listed} == {("Uno", "rejected"), ("Due", "manual")}

    removed = client.delete(f"/api/library/associations/{edited.json()['id']}", headers=admin)
    assert removed.status_code == 200
    title = client.get("/api/library/associations/title", params={
        "source": "streamingcommunity", "media_type": "episode", "external_id": "77"}).json()
    assert title["associations"] == []


@pytest.mark.parametrize("body", [
    {"folder": "../fuori"},
    {"folder": "inesistente"},
    {"source": "animeunity"},
    {"external_id": "1; DROP"},
    {"season_offset": 1000},
])
def test_the_api_refuses_what_it_cannot_trust(client, admin, root, body):
    (root / "Uno").mkdir()
    payload = {"source": "streamingcommunity", "media_type": "episode", "external_id": "77",
               "folder": "Uno", **body}
    response = client.post("/api/library/associations", headers=admin, json=payload)
    assert response.status_code in (400, 422), response.text
