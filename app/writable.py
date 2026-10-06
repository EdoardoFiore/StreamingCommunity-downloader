"""Startup check that the panel can write everywhere it has to (#28).

Run as a non-root ``user:`` in Docker, the panel used to start cleanly and fail
only once a download began — ``[Errno 13] Permission denied: 'tmp'``, three
retries and three minutes later — and a config volume created by an earlier
root run fails the same way on the first settings save. Both are deployment
mistakes that no retry fixes, so they are reported once, at boot, with the uid
that would need the access and the paths it lacks.

Temp space and the config files are fatal: nothing works without them. A
library is only warned about, because it may be a network mount that comes up
after the container does, and the failure stays confined to downloads into it.
Running as root none of this can trip, which is what keeps the default
deployment unchanged.
"""

import logging
import os
import tempfile
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


class NotWritableError(RuntimeError):
    pass


def _identity() -> str:
    """``uid 1000:1000`` on POSIX; empty on Windows, which has neither call."""
    if hasattr(os, "getuid"):
        return f"uid {os.getuid()}:{os.getgid()}"
    return ""


def _nearest_existing(path: Path) -> Path:
    """The path itself, or its closest ancestor on disk: what a ``mkdir -p``
    would have to write into."""
    path = path.absolute()
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _dir_is_writable(path: Path) -> bool:
    """Create a file and remove it. ``os.access`` is not trusted for
    directories: on NFS with root squashing and on some bind mounts it
    answers from the mode bits, not from what the server will allow."""
    try:
        with tempfile.TemporaryFile(dir=path):
            pass
    except OSError:
        return False
    return True


def problem(path: Path, is_dir: bool) -> str | None:
    """Why ``path`` cannot be written, or None when it can.

    A missing path is judged by its nearest existing ancestor, since that is
    where it would be created. An existing file is checked with ``os.access``
    rather than opened for writing, so a check never touches the database or
    the config it is checking.
    """
    path = Path(path)
    if path.exists():
        if is_dir:
            return None if _dir_is_writable(path) else f"{path} (directory not writable)"
        if os.access(path, os.W_OK):
            return None
        return f"{path} (file not writable)"
    anchor = _nearest_existing(path)
    if anchor.is_dir() and _dir_is_writable(anchor):
        return None
    return f"{path} (cannot be created in {anchor})"


def check(tmp_dir: Path, config_files: Iterable[Path], library_dirs: Iterable[Path]) -> None:
    """Raise NotWritableError for missing temp or config access; warn for libraries.

    ``config_files`` are the database, data.json and schedule.json: each needs
    its directory writable (SQLite keeps its -wal and -shm files beside the
    database, the settings lock is data.json.lock) and, if it already exists,
    the file itself.
    """
    problems: list[str] = []

    tmp_dir = Path(tmp_dir)
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # reported below, with the path that refused
    if issue := problem(tmp_dir, is_dir=True):
        problems.append(issue)

    seen_dirs: set[Path] = set()
    for file in config_files:
        file = Path(file)
        directory = file.parent.absolute()
        if directory not in seen_dirs:
            seen_dirs.add(directory)
            if issue := problem(directory, is_dir=True):
                problems.append(issue)
        if file.exists() and (issue := problem(file, is_dir=False)):
            problems.append(issue)
        lock = Path(str(file) + ".lock")
        if lock.exists() and (issue := problem(lock, is_dir=False)):
            problems.append(issue)

    who = _identity()
    hint = (
        "If the container runs with `user:`, give that user ownership of the "
        "paths above (chown -R UID:GID <dir>), or point TMP_DIR / DB_FILE / "
        "DATA_FILE / SCHEDULE_FILE somewhere it can write."
    )

    for library in library_dirs:
        if issue := problem(Path(library), is_dir=True):
            logger.warning(
                "Library not writable%s: %s — downloads into it will fail. %s",
                f" by {who}" if who else "", issue, hint,
            )

    if problems:
        raise NotWritableError(
            f"The panel cannot write where it needs to{f' (running as {who})' if who else ''}:\n  - "
            + "\n  - ".join(problems)
            + f"\n{hint}"
        )
