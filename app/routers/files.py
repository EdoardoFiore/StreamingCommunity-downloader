import os
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import VIDEOS_DIR

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


def _build_tree(directory: Path, base: Path) -> list[dict]:
    entries = []
    try:
        for item in sorted(directory.iterdir()):
            if item.is_dir():
                children = _build_tree(item, base)
                if children:
                    entries.append({
                        "name": item.name,
                        "type": "directory",
                        "path": str(item.relative_to(base)),
                        "children": children,
                    })
            elif item.is_file() and item.suffix == ".mp4":
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


@router.get("")
def list_files():
    if not VIDEOS_DIR.exists():
        return []
    return _build_tree(VIDEOS_DIR, VIDEOS_DIR)


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
