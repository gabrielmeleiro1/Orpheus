"""Maintenance and diagnostics routes."""

from __future__ import annotations

from pathlib import Path
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from modules.web.deps import ControllerDep, call_with_http_errors

router = APIRouter()


class CleanupStorageRequest(BaseModel):
    confirm: str = Field(..., min_length=1)


class OpenDeletionFolderRequest(BaseModel):
    path: str


@router.post("/api/maintenance/delete-staging/open-folder")
def open_deletion_folder(payload: OpenDeletionFolderRequest, controller: ControllerDep) -> dict:
    from modules.app.services.library_service import _default_folder_opener

    root = Path(controller.config.temp_dir).resolve() / "delete_staging"
    if not re.fullmatch(r"[0-9a-f]{32}", payload.path):
        raise HTTPException(status_code=404, detail="Pending deletion folder not found")
    path = root / payload.path
    if root.is_symlink() or root.is_junction() or path.is_symlink() or path.is_junction() or not path.is_dir():
        raise HTTPException(status_code=404, detail="Pending deletion folder not found")
    try:
        _default_folder_opener(path)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail="Pending deletion folder could not be opened") from exc
    return {"folder_path": payload.path}


@router.get("/api/maintenance/diagnostics")
def get_maintenance_diagnostics(controller: ControllerDep) -> dict:
    return call_with_http_errors(controller.get_storage_diagnostics)


@router.post("/api/maintenance/backup")
def post_maintenance_backup(controller: ControllerDep) -> dict:
    # runtime_status=409: an active conversion makes the backup guard raise a
    # RuntimeError, which should surface as a conflict, not a 500 (matches the
    # cleanup endpoint).
    backup_path = call_with_http_errors(controller.backup_database, runtime_status=409)
    return {"backup_path": str(backup_path)}


@router.post("/api/maintenance/cleanup")
def post_maintenance_cleanup(payload: CleanupStorageRequest, controller: ControllerDep) -> dict:
    return call_with_http_errors(
        lambda: controller.cleanup_storage(confirm=payload.confirm),
        runtime_status=409,
    )
