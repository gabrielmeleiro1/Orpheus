"""Source inspection routes."""

from __future__ import annotations

from pydantic import BaseModel, Field
from fastapi import APIRouter

from modules.web.deps import ControllerDep, TempDirDep, call_with_http_errors
from modules.web.source_handles import resolve_source_reference

router = APIRouter()


class SourcePreflightRequest(BaseModel):
    source_path: str = Field(..., min_length=1)


@router.post("/api/sources/preflight")
def post_source_preflight(
    payload: SourcePreflightRequest,
    temp_dir: TempDirDep,
    controller: ControllerDep,
) -> dict:
    return call_with_http_errors(
        lambda: controller.preflight_source(
            resolve_source_reference(payload.source_path, temp_dir)
        ),
    )
