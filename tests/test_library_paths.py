r"""A Windows library path on a Linux deployment, and what it cost.

Reported as "AnimeUnity does not download at all". Every segment of every
episode was fetched correctly and the job then died at the last step with

    Error opening output file N:\Jellyfin\Anime/Le Bizzarre .../... .mp4
    Error opening output files: Protocol not found

The anime library was configured with the *host* side of the Docker volume
mapping. Nothing on the way down objects to that: a backslash is an ordinary
character in a Linux filename, so ``os.makedirs`` cheerfully created a single
directory literally called ``N:\Jellyfin\Anime`` next to the app and the
download ran to completion into it. Only FFmpeg complained, and it complained
about a protocol called ``N`` — an error that points nowhere near the mistake.

Two defences, because they catch different deployments: the path is refused
when it is saved, and refused again before a download that inherited an
already-saved one fetches anything. Plus ``file:``, so a colon in a path
FFmpeg *should* accept is never read as a protocol name to begin with.
"""

import json
import os

import pytest

from app.core import paths
from app.core.ffmpeg_path import ffmpeg_file_arg
from app.core.paths import looks_like_windows_path, validate_library_path, windows_path_problem
from tests.conftest import ALL, do_setup, make_user, session_for


@pytest.fixture
def posix_host(monkeypatch):
    """Pretend to be the Linux container the report came from."""
    monkeypatch.setattr(paths, "_host_is_windows", lambda: False)
    monkeypatch.setattr(paths, "_in_container", lambda: True)


@pytest.fixture
def bare_linux_host(monkeypatch):
    """A manual install on Linux: still wrong, but for a different reason."""
    monkeypatch.setattr(paths, "_host_is_windows", lambda: False)
    monkeypatch.setattr(paths, "_in_container", lambda: False)


@pytest.fixture
def admin(client, admin_credentials):
    do_setup(client, admin_credentials)
    user = make_user("boss", "jf-boss-id", ALL)
    client.cookies.clear()
    return user, session_for(client, user.id)


# ── recognising one ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    r"N:\Jellyfin\Anime",
    r"C:\Users\me\Videos",
    "C:/Media",
    r"\\nas\media\anime",
])
def test_a_host_path_is_recognised(path):
    assert looks_like_windows_path(path)


@pytest.mark.parametrize("path", [
    "/media/anime",
    "/mnt/jellyfin/Anime",
    "videos",
    "",
    "  ",
])
def test_a_container_path_is_left_alone(path):
    assert not looks_like_windows_path(path)


def test_the_message_names_the_volume_mapping(posix_host):
    """The whole point of catching it: saying what to type instead.

    "Protocol not found" is true and useless. The offending path appears on
    both sides of the example so the reader can see which half the panel wants.
    """
    problem = windows_path_problem(r"N:\Jellyfin\Anime")

    assert problem is not None
    assert r"N:\Jellyfin\Anime" in problem
    assert "docker-compose" in problem
    assert "/media/anime" in problem


def test_bare_metal_linux_is_not_told_about_volumes(bare_linux_host):
    """There is no mapping to point at, so the advice must not invent one."""
    problem = windows_path_problem(r"N:\Jellyfin\Anime")

    assert problem is not None
    assert "docker" not in problem.lower()
    assert "/srv/media/anime" in problem


def test_a_windows_host_is_not_told_off(monkeypatch):
    """The same path is correct when the panel really does run on Windows."""
    monkeypatch.setattr(paths, "_host_is_windows", lambda: True)

    assert windows_path_problem(r"N:\Jellyfin\Anime") is None
    assert validate_library_path(r"N:\Jellyfin\Anime") == r"N:\Jellyfin\Anime"


def test_an_empty_path_is_refused_everywhere():
    with pytest.raises(ValueError, match="vuoto"):
        validate_library_path("   ")


def test_a_valid_path_comes_back_trimmed(posix_host):
    assert validate_library_path("  /media/anime  ") == "/media/anime"


# ── which deployment this is ────────────────────────────────────────

def test_the_real_host_has_the_last_word():
    """No patching at all, on whatever machine the suite is running.

    The check must never get in the way of a manual Windows install, where a
    drive path is simply the correct answer; and it must fire on Linux, where
    it is not. Running on both platforms, this asserts the same rule from
    opposite sides.
    """
    problem = windows_path_problem(r"N:\Jellyfin\Anime")

    assert (problem is None) == (os.name == "nt")


def test_dockerenv_is_enough(monkeypatch):
    monkeypatch.setattr(paths, "_DOCKERENV_FILE", __file__)

    assert paths._in_container()


def test_a_cgroup_marker_is_enough(monkeypatch, tmp_path):
    """Podman and Kubernetes leave no /.dockerenv behind."""
    cgroup = tmp_path / "cgroup"
    cgroup.write_text("0::/kubepods/besteffort/pod4f2\n", encoding="utf-8")
    monkeypatch.setattr(paths, "_DOCKERENV_FILE", str(tmp_path / "absent"))
    monkeypatch.setattr(paths, "_CGROUP_FILE", str(cgroup))

    assert paths._in_container()


def test_an_ordinary_host_is_not_a_container(monkeypatch, tmp_path):
    """A bare-metal Linux box: no marker file, and /proc/1/cgroup names none.

    A missing file has to read as "not a container" rather than raising —
    Windows has no /proc at all.
    """
    cgroup = tmp_path / "cgroup"
    cgroup.write_text("0::/user.slice/user-1000.slice\n", encoding="utf-8")
    monkeypatch.setattr(paths, "_DOCKERENV_FILE", str(tmp_path / "absent"))
    monkeypatch.setattr(paths, "_CGROUP_FILE", str(cgroup))
    assert paths._in_container() is False

    monkeypatch.setattr(paths, "_CGROUP_FILE", str(tmp_path / "absent"))
    assert paths._in_container() is False


# ── refused when saved ────────────────────────────────────────────────────────

def test_saving_a_host_path_is_rejected(client, admin, posix_host):
    _, csrf = admin

    response = client.put(
        "/api/domain/libraries",
        json={
            "libraries": [{"type": "anime", "path": r"N:\Jellyfin\Anime"}],
            "excluded_folders": [],
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400
    assert "docker-compose" in response.json()["detail"]


def test_a_rejected_save_writes_nothing(client, admin, posix_host):
    """One bad library must not leave the good ones half-applied."""
    from app import config

    _, csrf = admin
    client.put(
        "/api/domain/libraries",
        json={
            "libraries": [
                {"type": "film", "path": "/media/film"},
                {"type": "anime", "path": r"N:\Jellyfin\Anime"},
            ],
            "excluded_folders": [],
        },
        headers={"X-CSRF-Token": csrf},
    )

    stored = json.loads(config.DATA_FILE.read_text(encoding="utf-8"))
    assert "libraries" not in stored


def test_container_paths_still_save(client, admin, posix_host):
    _, csrf = admin

    response = client.put(
        "/api/domain/libraries",
        json={
            "libraries": [{"type": "anime", "path": "/media/anime"}],
            "excluded_folders": [],
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200


# ── refused before the download runs ──────────────────────────────────────────

def test_a_download_to_a_host_path_fails_before_fetching_anything(posix_host, monkeypatch):
    """The deployment in the report already had the bad path saved.

    Validating on save does nothing for it, so the check runs again at the top
    of the download — before a single segment is requested, rather than after
    all 186 of them.
    """
    from app.core import m3u8

    def explode(*args, **kwargs):
        raise AssertionError("nothing may be fetched before the path is checked")

    monkeypatch.setattr(m3u8.requests.Session, "get", explode)

    with pytest.raises(RuntimeError) as excinfo:
        m3u8.download_m3u8(
            m3u8_index="https://cdn.example.test/playlist.m3u8",
            output_filename=r"N:\Jellyfin\Anime\Show (2021)\Show S01E01.mp4",
        )

    assert "docker-compose" in str(excinfo.value)


# ── and the colon itself ──────────────────────────────────────────────────────

def test_ffmpeg_is_told_the_path_is_a_path():
    """``N:`` is a drive letter to everyone except FFmpeg, to which it is a
    protocol. The prefix removes the ambiguity."""
    assert ffmpeg_file_arg(r"N:\Jellyfin\out.mp4") == r"file:N:\Jellyfin\out.mp4"
    assert ffmpeg_file_arg("/media/anime/out.mp4") == "file:/media/anime/out.mp4"


def test_the_prefix_is_not_applied_twice():
    once = ffmpeg_file_arg("/media/anime/out.mp4")
    assert ffmpeg_file_arg(once) == once


def test_the_extension_stays_at_the_end():
    """FFmpeg picks the muxer from the extension, so the prefix must not
    disturb it."""
    assert ffmpeg_file_arg("/media/x/out.mkv").endswith(".mkv")
