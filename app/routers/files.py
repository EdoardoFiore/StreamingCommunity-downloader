import asyncio
import json
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import VIDEOS_DIR, DATA_FILE

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/files", tags=["files"])


def _safe_path(rel_path: str) -> Path:
    """Resolve path and ensure it's inside VIDEOS_DIR (prevent traversal)."""
    base = VIDEOS_DIR.resolve()
    target = (base / rel_path).resolve()
    if not target.is_relative_to(base):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return target


def _read_data() -> dict:
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _build_tree(directory: Path, base: Path, excluded: set) -> list[dict]:
    entries = []
    try:
        for item in sorted(directory.iterdir()):
            if item.name in excluded:
                continue
            if item.is_dir():
                children = _build_tree(item, base, excluded)
                if children:
                    entries.append({
                        "name": item.name,
                        "type": "directory",
                        "path": str(item.relative_to(base)),
                        "children": children,
                    })
            elif item.is_file():
                stat = item.stat()
                entries.append({
                    "name": item.name,
                    "type": "file",
                    "path": str(item.relative_to(base)),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                })
    except PermissionError:
        pass
    return entries


def _build_library_tree(directory: Path, depth: int = 0, max_depth: int = 3) -> list[dict]:
    """Recursively list subdirectories only (no files) for library navigation."""
    if depth >= max_depth:
        return []
    entries = []
    try:
        for item in sorted(directory.iterdir()):
            if item.is_dir() and not item.name.startswith('.'):
                entries.append({
                    "name": item.name,
                    "abs_path": str(item.resolve()),
                    "children": _build_library_tree(item, depth + 1, max_depth),
                })
    except PermissionError:
        pass
    return entries


_DEFAULT_EXCLUDED = {"images", "snippets", "lost+found"}


@router.get("")
async def list_files():
    if not VIDEOS_DIR.exists():
        return []
    excluded = _DEFAULT_EXCLUDED | set(_read_data().get("excluded_folders", []))
    return await asyncio.to_thread(_build_tree, VIDEOS_DIR, VIDEOS_DIR, excluded)


@router.get("/library-tree")
async def list_library_tree():
    data = _read_data()
    result = []
    for lib in data.get("libraries", []):
        lib_path = Path(lib["path"])
        exists = lib_path.exists()
        result.append({
            "name": lib["name"],
            "abs_path": str(lib_path.resolve()),
            "exists": exists,
            "children": (await asyncio.to_thread(_build_library_tree, lib_path)) if exists else [],
        })
    return result


@router.get("/stream/{file_path:path}")
def stream_file(file_path: str):
    target = _safe_path(file_path)
    return FileResponse(
        path=str(target),
        media_type="video/mp4",
        headers={"Content-Disposition": "inline"},
    )


@router.get("/download/{file_path:path}")
def download_file(file_path: str):
    target = _safe_path(file_path)
    return FileResponse(
        path=str(target),
        media_type="video/mp4",
        filename=target.name,
        headers={"Content-Disposition": f'attachment; filename="{target.name}"'},
    )


class MoveRequest(BaseModel):
    path: str
    dest_dir_path: str | None = None  # destination dir relative to VIDEOS_DIR (empty = root)
    dest_abs_path: str | None = None  # destination absolute path in a library
    library_name: str | None = None   # legacy


class BatchMoveRequest(BaseModel):
    paths: list[str]
    dest_dir_path: str


class BatchDeleteRequest(BaseModel):
    paths: list[str]


class RenameRequest(BaseModel):
    path: str
    new_name: str


@router.post("/move")
async def move_to_library(body: MoveRequest):
    data = _read_data()
    source = _safe_path(body.path)

    if body.dest_dir_path is not None:
        # Move within VIDEOS_DIR
        base = VIDEOS_DIR.resolve()
        dest_dir = (base / body.dest_dir_path).resolve() if body.dest_dir_path else base
        if not dest_dir.is_relative_to(base) and dest_dir != base:
            raise HTTPException(status_code=400, detail="Destinazione non valida")
        if not dest_dir.exists():
            raise HTTPException(status_code=400, detail="Cartella di destinazione non esiste")
        if source.is_dir():
            src_resolved = source.resolve()
            if dest_dir == src_resolved or dest_dir.is_relative_to(src_resolved):
                raise HTTPException(status_code=400, detail="Non puoi spostare una cartella dentro se stessa")
        dest = dest_dir / source.name
    elif body.dest_abs_path:
        lib_paths = [Path(lib["path"]).resolve() for lib in data.get("libraries", [])]
        dest_dir = Path(body.dest_abs_path).resolve()
        if not dest_dir.exists():
            raise HTTPException(status_code=400, detail="Cartella di destinazione non esiste")
        if not any(dest_dir == lp or dest_dir.is_relative_to(lp) for lp in lib_paths):
            raise HTTPException(status_code=400, detail="Destinazione non in una libreria configurata")
        dest = dest_dir / source.name
    elif body.library_name:
        lib_map = {lib["name"]: lib["path"] for lib in data.get("libraries", [])}
        if body.library_name not in lib_map:
            raise HTTPException(status_code=400, detail=f"Libreria '{body.library_name}' non configurata")
        dest_dir_root = Path(lib_map[body.library_name])
        if not dest_dir_root.exists():
            raise HTTPException(status_code=400, detail=f"Il percorso della libreria non esiste: {dest_dir_root}")
        rel = source.relative_to(VIDEOS_DIR.resolve())
        dest = dest_dir_root / rel
    else:
        raise HTTPException(status_code=400, detail="Specificare dest_dir_path, dest_abs_path o library_name")

    if dest.exists():
        raise HTTPException(status_code=409, detail=f"'{source.name}' esiste già nella destinazione")

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.move, str(source), str(dest))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Spostamento fallito: {e}")

    return {"moved_to": str(dest)}


def _delete_sync(target: Path):
    """Synchronous delete helper (runs in thread)."""
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


@router.delete("/delete/{file_path:path}", status_code=204)
async def delete_path(file_path: str):
    base = VIDEOS_DIR.resolve()
    target = (base / file_path).resolve()
    if not target.is_relative_to(base):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")
    await asyncio.to_thread(_delete_sync, target)


def _batch_move_sync(paths: list[str], dest_dir_path: str) -> list[dict]:
    """Move multiple paths within VIDEOS_DIR (blocking, runs in thread)."""
    base = VIDEOS_DIR.resolve()
    dest_dir = (base / dest_dir_path).resolve() if dest_dir_path else base
    results = []
    for p in paths:
        try:
            source = (base / p).resolve()
            if not source.is_relative_to(base) or not source.exists():
                results.append({"path": p, "ok": False, "error": "File non trovato"})
                continue
            if not dest_dir.is_relative_to(base) and dest_dir != base:
                results.append({"path": p, "ok": False, "error": "Destinazione non valida"})
                continue
            if source.is_dir():
                if dest_dir == source.resolve() or dest_dir.is_relative_to(source.resolve()):
                    results.append({"path": p, "ok": False, "error": "Non puoi spostare una cartella dentro se stessa"})
                    continue
            dest = dest_dir / source.name
            if dest.exists():
                results.append({"path": p, "ok": False, "error": f"'{source.name}' esiste già nella destinazione"})
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(dest))
            results.append({"path": p, "ok": True, "moved_to": str(dest)})
        except Exception as e:
            results.append({"path": p, "ok": False, "error": str(e)})
    return results


@router.post("/move-batch")
async def batch_move(body: BatchMoveRequest):
    if not body.paths:
        raise HTTPException(status_code=400, detail="Nessun file selezionato")
    results = await asyncio.to_thread(_batch_move_sync, body.paths, body.dest_dir_path)
    return {"results": results}


def _batch_delete_sync(paths: list[str]) -> list[dict]:
    """Delete multiple paths within VIDEOS_DIR (blocking, runs in thread)."""
    base = VIDEOS_DIR.resolve()
    results = []
    for p in paths:
        try:
            target = (base / p).resolve()
            if not target.is_relative_to(base):
                results.append({"path": p, "ok": False, "error": "Path non valido"})
                continue
            if not target.exists():
                results.append({"path": p, "ok": False, "error": "Non trovato"})
                continue
            _delete_sync(target)
            results.append({"path": p, "ok": True})
        except Exception as e:
            results.append({"path": p, "ok": False, "error": str(e)})
    return results


@router.post("/rename")
async def rename_path(body: RenameRequest):
    if not body.new_name or '/' in body.new_name or '\\' in body.new_name or body.new_name in ('.', '..'):
        raise HTTPException(status_code=400, detail="Nome non valido")
    source = _safe_path(body.path)
    dest = source.parent / body.new_name
    if dest.exists():
        raise HTTPException(status_code=409, detail=f"'{body.new_name}' esiste già")
    try:
        await asyncio.to_thread(source.rename, dest)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Rinomina fallita: {e}")
    return {"renamed_to": str(dest.relative_to(VIDEOS_DIR.resolve()))}


@router.post("/delete-batch")
async def batch_delete(body: BatchDeleteRequest):
    if not body.paths:
        raise HTTPException(status_code=400, detail="Nessun file selezionato")
    results = await asyncio.to_thread(_batch_delete_sync, body.paths)
    return {"results": results}
