"""Maintenance and diagnostics routes."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from modules.web.deps import ControllerDep, call_with_http_errors

router = APIRouter()


class CleanupStorageRequest(BaseModel):
    confirm: str = Field(..., min_length=1)


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
