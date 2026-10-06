"""The boot check for paths the panel must write (#28).

Running the container as a non-root ``user:`` used to start fine and then fail
every download with ``Permission denied: 'tmp'``. Now the panel says so at
boot, naming the paths — and as root, the default, nothing changes.
"""

import asyncio
import json
import logging
import os
import sys

import pytest

from app import writable


def _refuse(monkeypatch, *refused):
    """Make these directories unwritable without touching mode bits, which
    Windows ignores on directories and root ignores everywhere."""
    refused = {p.absolute() for p in refused}
    real = writable._dir_is_writable
    monkeypatch.setattr(
        writable, "_dir_is_writable",
        lambda path: path.absolute() not in refused and real(path),
    )


def test_everything_writable_passes_and_creates_the_temp_dir(tmp_path):
    tmp_dir = tmp_path / "tmp"
    config = tmp_path / "config"
    config.mkdir()
    (config / "panel.db").write_bytes(b"")

    writable.check(
        tmp_dir=tmp_dir,
        config_files=[config / "panel.db", config / "data.json", config / "schedule.json"],
        library_dirs=[tmp_path / "library"],
    )

    assert tmp_dir.is_dir()


def test_a_config_dir_not_created_yet_is_fine_when_its_parent_is_writable(tmp_path):
    """First run on a fresh volume: the database creates its own folder."""
    writable.check(
        tmp_dir=tmp_path / "tmp",
        config_files=[tmp_path / "config" / "panel.db"],
        library_dirs=[],
    )


def test_an_unwritable_temp_dir_stops_the_boot(tmp_path, monkeypatch):
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    _refuse(monkeypatch, tmp_dir)

    with pytest.raises(writable.NotWritableError) as raised:
        writable.check(tmp_dir=tmp_dir, config_files=[], library_dirs=[])

    message = str(raised.value)
    assert str(tmp_dir) in message
    assert "chown" in message


def test_a_config_file_left_behind_by_root_stops_the_boot(tmp_path, monkeypatch):
    """The directory is the user's, the database inside it is not: SQLite
    would open it read-only and fail on the first write."""
    db_file = tmp_path / "panel.db"
    db_file.write_bytes(b"")
    lock = tmp_path / "data.json.lock"
    lock.write_bytes(b"")
    data = tmp_path / "data.json"
    data.write_text("{}")
    real_access = os.access
    monkeypatch.setattr(
        writable.os, "access",
        lambda path, mode: str(path) not in {str(db_file), str(lock)} and real_access(path, mode),
    )

    with pytest.raises(writable.NotWritableError) as raised:
        writable.check(tmp_dir=tmp_path / "tmp", config_files=[db_file, data], library_dirs=[])

    message = str(raised.value)
    assert f"{db_file} (" in message
    assert f"{lock} (" in message
    assert f"{data} (" not in message


def test_every_problem_is_listed_at_once(tmp_path, monkeypatch):
    """Fixing one path per restart is the loop this check exists to end."""
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    _refuse(monkeypatch, tmp_dir, config)

    with pytest.raises(writable.NotWritableError) as raised:
        writable.check(tmp_dir=tmp_dir, config_files=[config / "panel.db"], library_dirs=[])

    assert str(tmp_dir) in str(raised.value)
    assert str(config) in str(raised.value)


def test_an_unwritable_library_is_only_a_warning(tmp_path, monkeypatch, caplog):
    """It may be a network mount that is not up yet; the panel itself works."""
    library = tmp_path / "library"
    library.mkdir()
    _refuse(monkeypatch, library)

    with caplog.at_level(logging.WARNING, logger="app.writable"):
        writable.check(tmp_dir=tmp_path / "tmp", config_files=[], library_dirs=[library])

    assert any(str(library) in r.getMessage() for r in caplog.records)


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs POSIX mode bits, which root bypasses",
)
def test_real_mode_bits_are_detected(tmp_path):
    """The mocked tests above, against an actual read-only directory."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        with pytest.raises(writable.NotWritableError):
            writable.check(tmp_dir=locked / "tmp", config_files=[], library_dirs=[])
    finally:
        locked.chmod(0o755)


# ── Wiring into the app ────────────────────────────────────────────────────────

def test_the_boot_stops_before_migrations(monkeypatch):
    """Otherwise SQLite's "unable to open database file" comes first, naming
    neither the path nor the uid."""
    import app.main as main_module

    migrated = []
    monkeypatch.setattr(main_module.db, "run_migrations", lambda: migrated.append(1))

    def refuse():
        raise writable.NotWritableError("nope")

    monkeypatch.setattr(main_module, "check_writable_paths", refuse)

    async def _run():
        async with main_module.lifespan(main_module.app):
            pass

    with pytest.raises(writable.NotWritableError):
        asyncio.run(_run())
    assert migrated == []


def test_library_dirs_include_videos_dir_only_as_a_fallback(tmp_path, monkeypatch):
    import app.main as main_module
    from app import config

    data_file = config.DATA_FILE
    monkeypatch.setattr(config, "VIDEOS_DIR", tmp_path / "videos")

    data_file.write_text(json.dumps({"libraries": [
        {"type": "film", "path": str(tmp_path / "films")},
    ]}))
    assert main_module._library_dirs() == sorted([tmp_path / "films", tmp_path / "videos"])

    data_file.write_text(json.dumps({"libraries": [
        {"type": kind, "path": str(tmp_path / kind)} for kind in ("film", "tv", "anime")
    ]}))
    assert tmp_path / "videos" not in main_module._library_dirs()
