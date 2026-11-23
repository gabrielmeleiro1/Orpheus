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
    # Names from ``summary.kept[category=unowned_output_folder]``; off by default.
    remove_unowned_folders: list[str] = Field(default_factory=list, max_length=100)


class OpenDeletionFolderRequest(BaseModel):
    path: str


def _staged_files_folder(controller, entry: Path) -> Path:
    """Where a pending deletion's files wait; ``entry`` itself holds only its record.

    Each artifact is staged beside its original, under the managed root that
    holds it. The record is validated against those roots, and a folder is
    opened only when it is a real directory inside one of them.
    """
    from modules.storage.deletion import read_delete_transaction
    from modules.storage.maintenance import canonical_sources_dir

    try:
        roots = (
            Path(controller.config.output_dir).resolve(),
            Path(controller.config.temp_dir).resolve(),
            canonical_sources_dir(controller.config.db_path).resolve(),
        )
        _book_id, artifacts = read_delete_transaction(entry, allowed_roots=roots)
        for _original, staged in artifacts:
            folder = staged.parent
            if (
                folder.is_dir() and not folder.is_symlink() and not folder.is_junction()
                and any(folder.resolve().is_relative_to(root) for root in roots)
            ):
                return folder
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return entry


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
        _default_folder_opener(_staged_files_folder(controller, path))
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


@router.get("/api/maintenance/backups")
def get_maintenance_backups(controller: ControllerDep) -> dict:
    return {"items": controller.list_database_backups()}


@router.post("/api/maintenance/backups/{backup_id}/restore")
def post_maintenance_restore(backup_id: str, controller: ControllerDep) -> dict:
    try:
        result = call_with_http_errors(
            lambda: controller.restore_database_backup_report(backup_id), runtime_status=409,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Database backup not found") from error
    return {
        "restored_id": backup_id,
        "safety_copy_path": str(result["safety_copy"]),
        "summary": result["summary"],
    }


@router.post("/api/maintenance/cleanup")
def post_maintenance_cleanup(payload: CleanupStorageRequest, controller: ControllerDep) -> dict:
    extra = {"remove_unowned_folders": tuple(payload.remove_unowned_folders)} if payload.remove_unowned_folders else {}
    return call_with_http_errors(
        lambda: controller.cleanup_storage(confirm=payload.confirm, **extra),
        runtime_status=409,
    )
