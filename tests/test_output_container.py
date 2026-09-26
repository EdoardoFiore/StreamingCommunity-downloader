"""The output container is a setting, and everything downstream reads it once.

The container used to be an accident: every destination was built as .mp4 and
the file turned into .mkv the moment there was a second audio track or a
subtitle to embed. Issue #19 asked for MP4 because some devices cannot play
Matroska at all, so the choice had to become explicit.

What these pin is the part that is easy to get wrong later: the FFmpeg command
differs between the two containers in exactly three places, the library check
must keep costing two stats, and the subtitle mode decides whether there is a
remux at all.
"""

import os

import pytest

from app import config
from app.core import container, format as fmt, paths
from app.core.m3u8 import M3U8_Downloader
from app.auth.permissions import ALL_PERMISSIONS
from tests.conftest import do_setup, make_user, session_for


def _configure(**settings):
    config.save_settings({**config.get_settings(), **settings})


# ── The setting itself ────────────────────────────────────────────────────────

def test_the_default_is_mkv():
    assert container.configured() == "mkv"
    assert container.subtitle_mode() == container.EMBED
    assert container.extension() == ".mkv"


def test_the_setting_is_honoured():
    _configure(output_container="mp4", subtitle_mode="external")
    assert container.configured() == "mp4"
    assert container.subtitle_mode() == container.EXTERNAL
    assert container.extension() == ".mp4"


@pytest.mark.parametrize("stored", ["avi", "", None, 7, {"a": 1}])
def test_an_unusable_stored_value_falls_back_instead_of_raising(stored):
    """Read inside a running download: a hand-edited data.json must not stop it."""
    _configure(output_container=stored, subtitle_mode=stored)
    assert container.configured() == container.DEFAULT
    assert container.subtitle_mode() == container.DEFAULT_SUBTITLE_MODE


def test_the_configured_container_is_probed_first():
    assert container.candidate_extensions() == (".mkv", ".mp4")
    _configure(output_container="mp4")
    assert container.candidate_extensions() == (".mp4", ".mkv")


# ── Paths ─────────────────────────────────────────────────────────────────────

def test_only_the_extension_changes_between_containers():
    """Folders and stems belong to the templates; the container owns the suffix."""
    for build, args in (
        (paths.film_path, ("/lib", "Blade Runner", "1982")),
        (paths.anime_path, ("/lib", "Naruto", "12")),
    ):
        mkv = build(*args, container="mkv")
        mp4 = build(*args, container="mp4")
        assert os.path.splitext(mkv)[0] == os.path.splitext(mp4)[0]
        assert (os.path.splitext(mkv)[1], os.path.splitext(mp4)[1]) == (".mkv", ".mp4")


def test_the_paths_follow_the_setting_with_no_argument():
    _configure(output_container="mp4")
    assert paths.film_path("/lib", "Blade Runner", "1982").endswith(".mp4")
    assert paths.episode_path("/lib", "Dark", 1, "7", "2017").endswith(".mp4")
    assert paths.anime_path("/lib", "Naruto", "12").endswith(".mp4")


# ── The join ──────────────────────────────────────────────────────────────────

def test_movflags_is_mp4_only():
    """It is mov/mp4-private. Handed to the matroska muxer FFmpeg refuses the
    whole command — at the very end of the download, after every byte."""
    assert "movflags" not in container.join_options("/lib/Film.mkv")
    assert container.join_options("/lib/Film.mp4")["movflags"] == "+faststart"


def test_the_join_is_otherwise_identical():
    mkv = container.join_options("/lib/Film.mkv")
    mp4 = container.join_options("/lib/Film.mp4")
    assert mkv == {k: v for k, v in mp4.items() if k != "movflags"}
    assert mkv["c:v"] == "copy"


def test_the_temp_audio_files_are_mp4_whatever_the_setting_says():
    """They are genuinely mp4, so the options come from the name being written,
    not from the configured container."""
    _configure(output_container="mkv")
    assert "movflags" in container.join_options("tmp/job/_audio_0.mp4")


# ── The remux command ─────────────────────────────────────────────────────────

AUDIO = [{"path": "a0.mp4", "language": "ita"}]
SUBS = [{"path": "s0.vtt", "language": "forced-eng"}]


def _cmd(ext):
    return fmt._remux_cmd(f"/lib/F{ext}", f"/lib/.F.remuxing{ext}", AUDIO, SUBS)


def test_matroska_copies_subtitles_and_mp4_converts_them():
    mkv, mp4 = _cmd(".mkv"), _cmd(".mp4")
    assert mkv[mkv.index("-c:s") + 1] == "copy"
    assert mp4[mp4.index("-c:s") + 1] == "mov_text"


def test_only_mp4_gets_faststart_on_the_remux():
    """Without it the remuxed mp4 puts moov at the end and /api/files/stream
    stops being progressive — the join already sets it for the mp4 it writes."""
    assert "-movflags" not in _cmd(".mkv")
    mp4 = _cmd(".mp4")
    assert mp4[mp4.index("-movflags") + 1] == "+faststart"


def test_the_language_metadata_is_identical_in_both():
    def tags(cmd):
        return [a for a in cmd if a.startswith("-metadata") or a.startswith("-disposition")
                or a.startswith("language=") or a.startswith("title=") or a == "forced"]

    assert tags(_cmd(".mkv")) == tags(_cmd(".mp4"))
    assert "language=eng" in _cmd(".mp4")  # 'forced-eng' normalised, not literal


@pytest.mark.parametrize("ext", [".mkv", ".mp4"])
def test_every_output_option_precedes_the_output_filename(ext):
    """The reason the argv is built by hand. ffmpeg-python put these after the
    filename, where ffmpeg ignores them, and subtitle languages never applied."""
    cmd = _cmd(ext)
    last = len(cmd) - 1
    assert cmd[last].endswith(f".remuxing{ext}")
    for i, arg in enumerate(cmd):
        if arg.startswith(("-map", "-metadata", "-disposition", "-c:")):
            assert i < last


# ── The remux, run ────────────────────────────────────────────────────────────

@pytest.fixture
def library_dir(tmp_path):
    """A folder holding nothing but the video.

    Not tmp_path itself: the autouse _configured_domain fixture drops its
    data.json there, and these assert on the exact directory listing — the
    whole point being that the staging file is gone.
    """
    path = tmp_path / "library"
    path.mkdir()
    return path


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    """Stand in for the binary: no test in this suite runs a real FFmpeg."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        target = cmd[-1].removeprefix("file:")
        if run.returncode == 0:
            with open(target, "wb") as handle:
                handle.write(b"remuxed")
        else:
            with open(target, "wb") as handle:
                handle.write(b"half")  # a real failure leaves debris behind

        class Proc:
            returncode = run.returncode
            stderr = b"boom"
        return Proc()

    run.returncode = 0
    monkeypatch.setattr(fmt.subprocess, "run", run)
    return run


def test_the_remux_replaces_the_file_in_place(library_dir, fake_ffmpeg):
    """Input and output share an extension now: the join already wrote the
    configured container, so there is no rename to hide behind."""
    video = library_dir / "Film.mkv"
    video.write_bytes(b"joined")

    assert fmt.remux(str(video), audio_tracks=AUDIO) == str(video)
    assert video.read_bytes() == b"remuxed"
    assert os.listdir(library_dir) == ["Film.mkv"]


def test_a_failed_remux_leaves_no_debris_and_keeps_the_original(library_dir, fake_ffmpeg):
    fake_ffmpeg.returncode = 1
    video = library_dir / "Film.mkv"
    video.write_bytes(b"joined")

    with pytest.raises(RuntimeError, match="remux"):
        fmt.remux(str(video), audio_tracks=AUDIO)

    assert video.read_bytes() == b"joined"
    assert os.listdir(library_dir) == ["Film.mkv"]


def test_nothing_to_add_means_no_ffmpeg_at_all(library_dir, fake_ffmpeg):
    video = library_dir / "Film.mkv"
    video.write_bytes(b"joined")

    assert fmt.remux(str(video)) == str(video)
    assert video.read_bytes() == b"joined"


def test_the_staging_file_is_hidden_from_a_library_scan():
    """It sits in the library folder while FFmpeg writes. A Jellyfin scan that
    runs meanwhile must not index it as a second copy of the title."""
    staged = fmt.staging_path("/lib/Film (2020)/Film (2020).mkv")
    assert os.path.basename(staged).startswith(".")
    assert staged.endswith(".mkv")
    assert os.path.dirname(staged) == os.path.dirname("/lib/Film (2020)/Film (2020).mkv")


# ── Subtitle placement ────────────────────────────────────────────────────────

class _FakeSegments:
    """Writes the file the real one would, and fetches nothing."""

    def __init__(self, url, key, temp_dir=None, **kwargs):
        self.temp_dir = temp_dir

    def get_info(self):
        pass

    def download_ts(self):
        pass

    def join(self, output_filename):
        os.makedirs(os.path.dirname(output_filename), exist_ok=True)
        with open(output_filename, "wb") as handle:
            handle.write(b"video")


@pytest.fixture
def downloader(tmp_path, monkeypatch):
    """A downloader whose video is already joined and whose subtitle is in temp."""
    monkeypatch.setattr("app.core.m3u8.M3U8_Segments", _FakeSegments)
    remuxed = []
    monkeypatch.setattr(
        fmt, "remux",
        lambda video, audio_tracks=None, subtitle_tracks=None: (
            remuxed.append({"audio": audio_tracks or [], "subs": subtitle_tracks or []}),
            video,
        )[1],
    )

    library = tmp_path / "lib" / "Film (2020)"
    library.mkdir(parents=True)
    temp = tmp_path / "tmp"
    temp.mkdir()
    (temp / "Film (2020).it.vtt").write_text("WEBVTT", encoding="utf-8")

    def build():
        return M3U8_Downloader(
            "https://example.test/index.m3u8",
            output_filename=str(library / "Film (2020).mkv"),
            temp_dir=str(temp),
            subtitle_languages=["ita"],
        )

    return build, library, temp, remuxed


def test_embedded_subtitles_go_through_the_remux_and_leave_no_sidecar(downloader):
    build, library, temp, remuxed = downloader
    _configure(subtitle_mode="embed")

    build().start()

    assert [os.path.basename(s["path"]) for s in remuxed[0]["subs"]] == ["Film (2020).it.vtt"]
    assert not (library / "Film (2020).it.vtt").exists()


def test_external_subtitles_land_beside_the_video_and_skip_the_remux(downloader):
    """Nothing to mux means nothing to mux: the sidecar is the deliverable."""
    build, library, temp, remuxed = downloader
    _configure(subtitle_mode="external")

    build().start()

    assert remuxed == []
    assert (library / "Film (2020).it.vtt").read_text(encoding="utf-8") == "WEBVTT"
    assert not (temp / "Film (2020).it.vtt").exists()


def test_external_subtitles_survive_a_remux_forced_by_extra_audio(downloader):
    """A second audio track still needs the mux — the subtitles still stay out."""
    build, library, temp, remuxed = downloader
    _configure(subtitle_mode="external")

    downloader_obj = build()
    downloader_obj.audio_paths = [{"path": "a0.mp4", "language": "eng"}]
    downloader_obj.start()

    assert remuxed[0]["subs"] == []
    assert remuxed[0]["audio"] == [{"path": "a0.mp4", "language": "eng"}]
    assert (library / "Film (2020).it.vtt").exists()


# ── The superseded copy ───────────────────────────────────────────────────────

def test_the_copy_in_the_other_container_is_removed(downloader):
    """Re-downloading after a container switch must not leave two files.

    Jellyfin reads Film.mkv and Film.mp4 in one folder as two versions of one
    film, and the second is exactly the one the switch was meant to replace.
    """
    build, library, temp, _ = downloader
    stale = library / "Film (2020).mp4"
    stale.write_bytes(b"the old container")

    result = build().start()

    assert result == str(library / "Film (2020).mkv")
    assert not stale.exists()


def test_an_unrelated_title_is_never_touched(downloader):
    """Only the exact sibling of the file just written."""
    build, library, temp, _ = downloader
    bystander = library / "Another Film (1999).mp4"
    bystander.write_bytes(b"not mine")

    build().start()

    assert bystander.exists()


# ── Settings API ──────────────────────────────────────────────────────────────

@pytest.fixture
def admin(client, admin_credentials):
    do_setup(client, admin_credentials)
    user = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    client.cookies.clear()
    return user, session_for(client, user.id)


def _csrf(admin):
    return {"X-CSRF-Token": admin[1]}


def test_the_api_offers_exactly_what_the_table_holds():
    """The Literal on SettingsUpdate is written out by hand; this is the tripwire
    that stops it drifting from app.core.container."""
    from typing import get_args
    from app.routers.domain import SettingsUpdate

    allowed = get_args(get_args(SettingsUpdate.model_fields["output_container"].annotation)[0])
    assert set(allowed) == set(container.CONTAINERS)

    modes = get_args(get_args(SettingsUpdate.model_fields["subtitle_mode"].annotation)[0])
    assert set(modes) == set(container.SUBTITLE_MODES)


def test_the_container_round_trips(client, admin):
    res = client.put("/api/domain/settings", headers=_csrf(admin),
                     json={"output_container": "mp4", "subtitle_mode": "external"})
    assert res.status_code == 200
    body = client.get("/api/domain/settings").json()
    assert (body["output_container"], body["subtitle_mode"]) == ("mp4", "external")


@pytest.mark.parametrize("payload", [
    {"output_container": "avi"},
    {"output_container": "webm"},
    {"subtitle_mode": "somewhere"},
])
def test_an_unknown_value_is_refused(client, admin, payload):
    assert client.put("/api/domain/settings", headers=_csrf(admin),
                      json=payload).status_code == 422


def test_saving_the_format_leaves_the_other_settings_alone(client, admin):
    before = config.get_settings()["max_concurrent_downloads"]
    client.put("/api/domain/settings", headers=_csrf(admin),
               json={"output_container": "mp4"})
    assert config.get_settings()["max_concurrent_downloads"] == before


def test_the_jellyfin_refresh_switch_actually_persists(client, admin):
    """It was in the defaults, read by downloads_hooks and PUT by the settings
    page, but missing from SettingsUpdate — so pydantic dropped it and the
    switch reported success while saving nothing."""
    res = client.put("/api/domain/settings", headers=_csrf(admin),
                     json={"jellyfin_refresh_on_download": True})
    assert res.status_code == 200
    assert config.get_settings()["jellyfin_refresh_on_download"] is True
