"""Source inspection routes."""

from __future__ import annotations

from pydantic import BaseModel, Field
from fastapi import APIRouter

from modules.contracts.source_extensions import SUPPORTED_SOURCE_SUFFIXES
from modules.errors import SourceFileNotFoundError, UnsupportedSourceTypeError
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
    def preflight() -> dict:
        source = resolve_source_reference(payload.source_path, temp_dir)
        # Plain, coded answers for the two mistakes a user can make here.
        if source.suffix.lower() not in SUPPORTED_SOURCE_SUFFIXES:
            raise UnsupportedSourceTypeError()
        if not source.exists():
            raise SourceFileNotFoundError(source)
        result = controller.preflight_source(source)
        find_existing = getattr(controller, "find_existing_books", None)
        if callable(find_existing) and isinstance(result, dict):
            # Converting again stays allowed; these facts let the UI say so.
            existing = find_existing(source, result.get("title"))
            if isinstance(existing, list):
                result = {**result, "existing_books": existing}
        return result

    return call_with_http_errors(preflight)
