"""Which folders in the library hold which title.

The library check finds a title by rebuilding its folder name from title and
year. That breaks the moment either changes: the year was corrected once
already (#21), leaving one series spread over ``I Simpson/``, ``I Simpson
(2025)/`` and ``I Simpson (2026)/`` (#24), and a title renamed at the source or
a template edited twice would do the same. So every finished download files
its folder here under the title's id at its source, and the check asks this
registry after it asks the name.

An association's ``origin`` says who decided it, and that decides who may
change it:

- **download**: the folder a finished download was written to. Certain.
- **matched**: an older folder of the same series, recognised by name — same
  title, a year no earlier than the premiere, see ``resolver.series_years`` —
  at the moment a new episode landed. Recorded then and not when an episode
  list is merely opened: a download is the one moment someone has said "this
  is that series". Never for a film, where another year is as likely a remake.
- **manual**: set by a person. It also decides where the title's next files
  go, and with ``season_offset`` / ``episode_offset`` where inside the folder —
  which is how an anime split into parts at the source becomes one series.
- **rejected**: a pair a person has corrected away. Kept, not deleted, so the
  panel cannot guess it back: the next download would otherwise match the
  same folder by name and file it again.

The panel never overrides a person: an automatic filing leaves manual and
rejected rows exactly as they are.

Nothing here renames, moves or deletes a file. A registered folder that is
missing is looked for under another name by the size of a file known to be in
it (``reconcile``), and forgotten only after it has been missing for a month
from a library that was readable the whole time — it may be a sleeping
network mount.
"""

import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app import db
from app.requests.models import now_iso

logger = logging.getLogger(__name__)

FILM = "film"
EPISODE = "episode"
ANIME = "anime"
MEDIA_TYPES = (FILM, EPISODE, ANIME)

DOWNLOAD = "download"
MATCHED = "matched"
MANUAL = "manual"
REJECTED = "rejected"

# A job knows its kind, not its source: every film and series comes from
# StreamingCommunity, every anime from AnimeUnity.
SOURCE_BY_KIND = {FILM: "streamingcommunity", EPISODE: "streamingcommunity",
                  ANIME: "animeunity"}

# How long a folder may be missing, from a library that could be read, before
# its association is forgotten.
MISSING_GRACE = timedelta(days=30)

# "S01E07", "S10E04", "s02e07.5", as the default templates write them. Not
# anchored to a title, which is the point: a series renamed at the source still
# numbers its episodes the same way.
_EPISODE_RE = re.compile(r"(?i)(?<![a-z0-9])S(\d{1,3})E(\d{1,4}(?:\.\d+)?)(?!\.?\d)")

# Person-made rows first: they are the ones a person meant.
_ORDER = "CASE origin WHEN 'manual' THEN 0 WHEN 'download' THEN 1 ELSE 2 END, updated_at DESC, id"


@dataclass(frozen=True)
class Association:
    id: int
    source: str
    media_type: str
    external_id: str
    title: str | None
    folder: str
    origin: str
    season_offset: int
    episode_offset: int
    anchor_size: int | None
    missing_since: str | None
    created_at: str
    updated_at: str

    def to_public(self, root: str | None = None) -> dict:
        data = {k: getattr(self, k) for k in self.__dataclass_fields__ if k != "anchor_size"}
        if root is not None:
            data["exists"] = os.path.isdir(os.path.join(root, self.folder))
        return data


def _row(row) -> Association:
    return Association(**{k: row[k] for k in Association.__dataclass_fields__})


# ── Reading ───────────────────────────────────────────────────────────────────

def get(association_id: int) -> Association | None:
    row = db.query_one("SELECT * FROM library_folder WHERE id = ?", (association_id,))
    return _row(row) if row else None


def list_all() -> list[Association]:
    rows = db.query(f"SELECT * FROM library_folder ORDER BY title COLLATE NOCASE, {_ORDER}")
    return [_row(r) for r in rows]


def for_title(source: str, media_type: str, external_id,
              include_rejected: bool = False) -> list[Association]:
    if not (source and external_id):
        return []
    sql = ("SELECT * FROM library_folder "
           "WHERE source = ? AND media_type = ? AND external_id = ?")
    if not include_rejected:
        sql += " AND origin != 'rejected'"
    rows = db.query(f"{sql} ORDER BY {_ORDER}", (source, media_type, str(external_id)))
    return [_row(r) for r in rows]


def folders(source: str, media_type: str, external_id) -> list[str]:
    """Folder names associated with this title, person-made first."""
    return [a.folder for a in for_title(source, media_type, external_id)]


def rejected_folders(source: str, media_type: str, external_id) -> set[str]:
    """Folders a person has said are not this title's. The name-based check
    skips them too, or it would keep finding what was corrected away."""
    if not (source and external_id):
        return set()
    rows = db.query(
        "SELECT folder FROM library_folder WHERE source = ? AND media_type = ? "
        "AND external_id = ? AND origin = 'rejected'",
        (source, media_type, str(external_id)),
    )
    return {r["folder"] for r in rows}


# ── Automatic filing ──────────────────────────────────────────────────────────

def record(source: str, media_type: str, external_id, folder: str, origin: str,
           title: str | None = None, anchor_size: int | None = None) -> None:
    """File ``folder`` under this title, unless a person has already decided.

    A download upgrades a matched row, never the other way round, and neither
    touches a manual or a rejected one beyond keeping its title and anchor
    fresh — a file landing in a folder is still news about that folder.
    """
    if not (source and external_id and folder):
        return
    now = now_iso()
    db.execute(
        "INSERT INTO library_folder(source, media_type, external_id, title, folder, origin, "
        "anchor_size, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source, media_type, external_id, folder) DO UPDATE SET "
        "title = COALESCE(excluded.title, title), "
        "origin = CASE WHEN origin = 'matched' AND excluded.origin = 'download' "
        "         THEN 'download' ELSE origin END, "
        "anchor_size = COALESCE(excluded.anchor_size, anchor_size), "
        "missing_since = NULL, updated_at = excluded.updated_at "
        "WHERE origin != 'rejected'",
        (source, media_type, str(external_id), title, folder, origin, anchor_size, now, now),
    )


# ── A person's decisions ──────────────────────────────────────────────────────

class InvalidFolder(ValueError):
    pass


def library_root(media_type: str) -> str:
    from app.requests import resolver

    return resolver.library_dir(media_type)


def validate_folder(media_type: str, folder: str) -> str:
    """A folder name that exists directly under this media type's library.

    One path component and nothing else: this is a name picked from a listing,
    and anything that could step outside the library — a separator, ``..`` — is
    refused even though the panel only ever reads through it.
    """
    name = (folder or "").strip()
    if (not name or name in (".", "..") or name.startswith(".")
            or "/" in name or "\\" in name or "\x00" in name):
        raise InvalidFolder("Nome di cartella non valido")
    if not os.path.isdir(os.path.join(library_root(media_type), name)):
        raise InvalidFolder(f"La cartella «{name}» non esiste nella libreria")
    return name


def library_folders(media_type: str) -> list[str]:
    """The folders directly under this media type's library, for a picker."""
    try:
        with os.scandir(library_root(media_type)) as entries:
            names = [e.name for e in entries if not e.name.startswith(".") and e.is_dir()]
    except OSError:
        return []
    return sorted(names, key=str.casefold)


def associate(source: str, media_type: str, external_id, folder: str,
              title: str | None = None, season_offset: int = 0,
              episode_offset: int = 0) -> Association:
    """A person says this folder holds this title. Overrides whatever was there,
    a rejection included: the latest decision wins."""
    folder = validate_folder(media_type, folder)
    anchor = _anchor_of(os.path.join(library_root(media_type), folder))
    now = now_iso()
    db.execute(
        "INSERT INTO library_folder(source, media_type, external_id, title, folder, origin, "
        "season_offset, episode_offset, anchor_size, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'manual', ?, ?, ?, ?, ?) "
        "ON CONFLICT(source, media_type, external_id, folder) DO UPDATE SET "
        "title = COALESCE(excluded.title, title), origin = 'manual', "
        "season_offset = excluded.season_offset, episode_offset = excluded.episode_offset, "
        "anchor_size = COALESCE(excluded.anchor_size, anchor_size), "
        "missing_since = NULL, updated_at = excluded.updated_at",
        (source, media_type, str(external_id), title, folder, int(season_offset),
         int(episode_offset), anchor, now, now),
    )
    row = db.query_one(
        "SELECT * FROM library_folder WHERE source = ? AND media_type = ? "
        "AND external_id = ? AND folder = ?",
        (source, media_type, str(external_id), folder),
    )
    return _row(row)


def edit(association_id: int, external_id=None, folder: str | None = None,
         season_offset: int | None = None, episode_offset: int | None = None) -> Association:
    """Correct an association. The correction is the whole gesture.

    Pointing it at another title or another folder rejects the pair it was, so
    the panel cannot file it again, and records the new pair as a person's.
    Changing only the offsets keeps the pair and makes it a person's too.
    """
    current = get(association_id)
    if current is None or current.origin == REJECTED:
        raise LookupError("Associazione non trovata")
    new_id = str(external_id).strip() if external_id not in (None, "") else current.external_id
    new_folder = folder if folder not in (None, "") else current.folder
    offsets = dict(
        season_offset=current.season_offset if season_offset is None else int(season_offset),
        episode_offset=current.episode_offset if episode_offset is None else int(episode_offset),
    )
    if (new_id, new_folder) == (current.external_id, current.folder):
        db.execute(
            "UPDATE library_folder SET origin = 'manual', season_offset = ?, "
            "episode_offset = ?, updated_at = ? WHERE id = ?",
            (offsets["season_offset"], offsets["episode_offset"], now_iso(), association_id),
        )
        return get(association_id)

    validate_folder(current.media_type, new_folder)
    reject(association_id)
    return associate(
        current.source, current.media_type, new_id, new_folder,
        # A title belongs to its id; a different id's name is not known here,
        # and the next download fills it in.
        title=current.title if new_id == current.external_id else None,
        **offsets,
    )


def reject(association_id: int) -> None:
    db.execute(
        "UPDATE library_folder SET origin = 'rejected', updated_at = ? WHERE id = ?",
        (now_iso(), association_id),
    )


def restore(association_id: int) -> bool:
    """Undo a rejection: the pair goes back to being the panel's to find."""
    cursor = db.execute(
        "DELETE FROM library_folder WHERE id = ? AND origin = 'rejected'", (association_id,)
    )
    return cursor.rowcount > 0


# ── Numbers ───────────────────────────────────────────────────────────────────

def episode_key(season, episode) -> tuple[int, str] | None:
    """``(10, "4")`` for season 10 episode 04, or None when it is not a number.

    Both sides of a comparison go through here, so "04" on disk and "4" in a
    request agree, and so do "7.50" and "7.5".
    """
    try:
        whole, _, fraction = str(episode).strip().partition(".")
        number = str(int(whole))
        fraction = fraction.rstrip("0")
        if fraction and not fraction.isdigit():
            return None
        return int(season), number + ("." + fraction if fraction else "")
    except (TypeError, ValueError):
        return None


def shift_episode(episode, offset: int) -> str | None:
    """``"1"`` + 12 → ``"13"``; ``"7.5"`` + 2 → ``"9.5"``. None if not a number."""
    key = episode_key(1, episode)
    if key is None:
        return None
    whole, _, fraction = key[1].partition(".")
    shifted = int(whole) + int(offset)
    if shifted < 0:
        return None
    return str(shifted) + ("." + fraction if fraction else "")


def placed_numbers(association: Association, media_type: str, season, episode):
    """Where a title's own (season, episode) lands inside this folder.

    An anime has no seasons at its source — everything is season 1 — so its
    season is 1 plus the offset. None when the numbers cannot be placed.
    """
    native = 1 if media_type == ANIME else int(season or 1)
    placed_season = native + association.season_offset
    placed_episode = shift_episode(episode, association.episode_offset)
    if placed_season < 0 or placed_episode is None:
        return None
    return placed_season, placed_episode


# ── Finding a file in an associated folder ────────────────────────────────────

def _video_files(folder: str, depth: int = 2):
    """Video files in ``folder`` and, for a series, its season folders."""
    from app.core import container

    extensions = tuple(container.candidate_extensions())
    try:
        with os.scandir(folder) as entries:
            entries = sorted(entries, key=lambda e: e.name)
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith("."):
            # The remux's staging file, or anything else half-written.
            continue
        try:
            if entry.is_dir():
                if depth > 1:
                    yield from _video_files(entry.path, depth - 1)
            elif entry.name.lower().endswith(extensions):
                yield entry.path
        except OSError:
            continue


def _anchor_of(folder: str) -> int | None:
    """The size of some video in ``folder``: its fingerprint if it is renamed."""
    for path in _video_files(folder):
        try:
            return os.path.getsize(path)
        except OSError:
            continue
    return None


def episode_files(dirs: list[str]) -> dict[tuple[int, str], str]:
    """Every numbered episode in these folders, by ``episode_key``.

    Read by the episode number in the file name, not by the name the template
    would give it today: an associated folder is known to be this title, so the
    only question left is which episodes it has — and the title in the name is
    exactly the part that may have changed. The first folder wins a tie.
    """
    found: dict[tuple[int, str], str] = {}
    for folder in dirs:
        for path in _video_files(folder):
            match = _EPISODE_RE.search(os.path.basename(path))
            if not match:
                continue
            key = episode_key(match.group(1), match.group(2))
            if key is not None:
                found.setdefault(key, path)
    return found


@dataclass
class Held:
    """One associated folder that exists, with its episodes read on demand."""
    association: Association
    path: str
    _files: dict | None = field(default=None, repr=False)

    @property
    def files(self) -> dict:
        if self._files is None:
            self._files = episode_files([self.path])
        return self._files


def held(source: str, media_type: str, external_id, root: str) -> list[Held]:
    """This title's associated folders that exist right now, person-made first."""
    found = []
    for association in for_title(source, media_type, external_id):
        path = os.path.join(root, association.folder)
        if os.path.isdir(path):
            found.append(Held(association, path))
    return found


def locate(folders_held: list[Held], media_type: str, *, season=None, episode=None,
           year=None, single_file: bool = False) -> str | None:
    """The file these folders already hold for this title, if any.

    Where the layout says it should be first — the same builder the download
    uses, so a custom template without an ``SxxEyy`` is still found — then by
    the episode number in any file name.
    """
    from app.core import container, paths

    extensions = container.candidate_extensions()

    def present(path: str) -> str | None:
        stem = os.path.splitext(path)[0]
        return next((stem + ext for ext in extensions if os.path.exists(stem + ext)), None)

    anime = media_type == ANIME
    for h in folders_held:
        if single_file:
            found = present(paths.placed_single_path(h.path, year, anime=anime))
            found = found or next(iter(_video_files(h.path, depth=1)), None)
            if found:
                return found
            continue
        placed = placed_numbers(h.association, media_type, season, episode)
        if placed is None:
            continue
        found = present(paths.placed_episode_path(h.path, placed[0], placed[1], anime=anime))
        found = found or h.files.get(episode_key(*placed))
        if found:
            return found
    return None


def find(source: str, media_type: str, external_id, root: str, *,
         season=None, episode=None, year=None, single_file: bool = False) -> str | None:
    return locate(held(source, media_type, external_id, root), media_type,
                  season=season, episode=episode, year=year, single_file=single_file)


# ── Where the next file goes ──────────────────────────────────────────────────

def destination(kind: str, external_id, *, root: str, season=None, episode=None,
                year=None, single_file: bool = False, container: str | None = None) -> str | None:
    """The path a person's association sends this download to, or None.

    Only a manual association decides here: the panel's own filings describe
    where things already are, and following them would pin every new episode
    to whichever stray folder was matched first. None means "decide by name":
    the series' folder already on disk for an episode
    (``resolver.series_episode_path``), the canonical path otherwise.
    Never raises — this runs at the top of a download, and a registry that
    cannot be read must not be the reason one fails.
    """
    from app.core import paths

    try:
        source = SOURCE_BY_KIND.get(kind)
        chosen = next((h for h in held(source, kind, external_id, root)
                       if h.association.origin == MANUAL), None)
        if chosen is None:
            return None
        anime = kind == ANIME
        if single_file:
            return paths.placed_single_path(chosen.path, year, anime=anime, container=container)
        placed = placed_numbers(chosen.association, kind, season, episode)
        if placed is None:
            return None
        return paths.placed_episode_path(chosen.path, placed[0], placed[1], anime=anime,
                                         container=container)
    except Exception:
        logger.exception("Cannot read the association for %s %s; using the default path",
                         kind, external_id)
        return None


# ── Filing a finished download ────────────────────────────────────────────────

def _layout(root: str, output_path: str) -> list[str] | None:
    """``output_path`` under ``root`` as its components, or None when outside."""
    try:
        relative = os.path.relpath(output_path, root)
    except ValueError:
        # Another drive, on Windows.
        return None
    parts = relative.split(os.sep)
    if parts[0] in ("", ".", "..") or len(parts) < 2:
        # Outside the library, or a file straight in the root: no folder of
        # its own to file.
        return None
    return parts


def file_download(job) -> None:
    """Register where a finished download went, and its series' older folders."""
    from app.core import naming, paths
    from app.requests import resolver

    kind = job.type
    source = SOURCE_BY_KIND.get(kind)
    if not source or not job.external_id or not job.output_path:
        return
    root = resolver.library_dir(kind)
    parts = _layout(root, job.output_path)
    if parts is None:
        return
    folder = parts[0]
    try:
        size = os.path.getsize(job.output_path)
    except OSError:
        size = None
    record(source, kind, job.external_id, folder, DOWNLOAD,
           title=job.media_label, anchor_size=size)

    # Title/file is a film or an anime film; title/season/file is a series.
    # Read off the path because the job does not carry the anime's type, and
    # the path is what was actually written.
    if len(parts) < 3 or not job.media_label:
        return
    folder_for = paths.anime_folder if kind == ANIME else paths.series_folder
    rejected = rejected_folders(source, kind, job.external_id)
    years = resolver.series_years(root, job.media_label, job.year, folder_for, exclude=rejected)
    for year in years[1:]:
        for templates in (None, naming.LEGACY_TEMPLATES):
            name = folder_for(job.media_label, year, templates)
            path = os.path.join(root, name)
            if name != folder and name not in rejected and os.path.isdir(path):
                record(source, kind, job.external_id, name, MATCHED,
                       title=job.media_label, anchor_size=_anchor_of(path))


def on_job_finished(job) -> None:
    """Job listener. A registry that fails must not fail anything else."""
    if getattr(job, "status", None) != "done":
        return
    try:
        file_download(job)
    except Exception:
        logger.exception("Cannot file download %s in the library registry",
                         getattr(job, "job_id", "?"))


_listener_registered = False
_listener_lock = threading.Lock()


def register_job_listener():
    """Register once. Idempotent, like the other listeners."""
    global _listener_registered
    with _listener_lock:
        if _listener_registered:
            return
        from app.jobs import job_manager

        job_manager.add_listener(on_job_finished)
        _listener_registered = True


# ── Folders renamed or removed outside the panel ──────────────────────────────

def _now() -> datetime:
    """Indirected so tests can move the clock."""
    return datetime.now(timezone.utc)


def _sizes_by_folder(root: str, names: set[str]) -> dict[int, set[str]]:
    """Every video's size in these folders, mapped to the folders holding it."""
    sizes: dict[int, set[str]] = {}
    for name in names:
        for path in _video_files(os.path.join(root, name)):
            try:
                sizes.setdefault(os.path.getsize(path), set()).add(name)
            except OSError:
                continue
    return sizes


def reconcile() -> dict:
    """Follow folders renamed outside the panel, and forget removed ones.

    A missing folder is looked for among the library's folders that nothing is
    associated with, by the size of a file known to be in it: a video's size in
    bytes is as good as unique, and survives renaming the folder and the file
    alike. Exactly one candidate or none — two folders holding a file of that
    size is a copy, and guessing between them is how a wrong answer is made.

    Nothing is concluded from a library that cannot be read or reads empty:
    that is an unmounted volume far more often than a deleted collection.
    """
    summary = {"renamed": 0, "missing": 0, "forgotten": 0}
    active = [a for a in list_all() if a.origin != REJECTED]
    by_root: dict[str, list[Association]] = {}
    for association in active:
        by_root.setdefault(library_root(association.media_type), []).append(association)

    for root, associations in by_root.items():
        try:
            with os.scandir(root) as entries:
                names = {e.name for e in entries if not e.name.startswith(".") and e.is_dir()}
        except OSError:
            continue
        if not names:
            continue
        missing = [a for a in associations if a.folder not in names]
        for association in associations:
            if association.folder in names and association.missing_since:
                db.execute("UPDATE library_folder SET missing_since = NULL WHERE id = ?",
                           (association.id,))
        if not missing:
            continue

        taken = {a.folder for a in active}
        sizes = None
        for association in missing:
            if association.anchor_size:
                if sizes is None:
                    sizes = _sizes_by_folder(root, names - taken)
                holders = sizes.get(association.anchor_size, set())
                if len(holders) == 1:
                    _move(association, next(iter(holders)))
                    summary["renamed"] += 1
                    continue
            if association.missing_since is None:
                db.execute("UPDATE library_folder SET missing_since = ? WHERE id = ?",
                           (now_iso(), association.id))
                summary["missing"] += 1
            elif _now() - datetime.fromisoformat(association.missing_since) > MISSING_GRACE:
                db.execute("DELETE FROM library_folder WHERE id = ?", (association.id,))
                summary["forgotten"] += 1
            else:
                summary["missing"] += 1

    if summary["renamed"] or summary["forgotten"]:
        logger.info("Library associations reconciled: %s", summary)
    return summary


def _move(association: Association, new_folder: str) -> None:
    logger.info("Folder «%s» found again as «%s» (%s %s)", association.folder, new_folder,
                association.media_type, association.external_id)
    try:
        db.execute(
            "UPDATE library_folder SET folder = ?, missing_since = NULL, updated_at = ? "
            "WHERE id = ?",
            (new_folder, now_iso(), association.id),
        )
    except sqlite3.IntegrityError:
        # The title already has that folder: the row that moved is redundant.
        db.execute("DELETE FROM library_folder WHERE id = ?", (association.id,))
