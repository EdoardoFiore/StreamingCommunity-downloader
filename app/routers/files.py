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


_DEFAULT_EXCLUDED = {"images", "snippets", "subtitle", "lost+found"}


@router.get("")
def list_files():
    if not VIDEOS_DIR.exists():
        return []
    excluded = _DEFAULT_EXCLUDED | set(_read_data().get("excluded_folders", []))
    return _build_tree(VIDEOS_DIR, VIDEOS_DIR, excluded)


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
    library_name: str


@router.post("/move")
def move_to_library(body: MoveRequest):
    data = _read_data()
    lib_map = {lib["name"]: lib["path"] for lib in data.get("libraries", [])}

    if body.library_name not in lib_map:
        raise HTTPException(status_code=400, detail=f"Libreria '{body.library_name}' non configurata")

    source = _safe_path(body.path)
    dest_dir = Path(lib_map[body.library_name])

    if not dest_dir.exists():
        raise HTTPException(status_code=400, detail=f"Il percorso della libreria non esiste: {dest_dir}")

    dest = dest_dir / source.name
    if dest.exists():
        raise HTTPException(status_code=409, detail=f"'{source.name}' esiste già nella libreria")

    try:
        shutil.move(str(source), str(dest))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Spostamento fallito: {e}")

    return {"moved_to": str(dest)}


@router.delete("/delete/{file_path:path}", status_code=204)
def delete_path(file_path: str):
    base = VIDEOS_DIR.resolve()
    target = (base / file_path).resolve()
    if not target.is_relative_to(base):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
