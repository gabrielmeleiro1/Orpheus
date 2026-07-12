"""Source upload routes."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from modules.contracts.limits import MAX_UPLOAD_BYTES
from modules.contracts.upload_errors import UploadTooLargeError
from modules.web.deps import ControllerDep

router = APIRouter()

# Multipart framing (boundary lines and part headers) rides along with the
# file bytes in Content-Length; allow modest overhead before rejecting.
CONTENT_LENGTH_SLACK_BYTES = 64 * 1024

_UPLOAD_BODY_PATHS = frozenset({"/api/uploads/source"})


def add_upload_size_limit_middleware(app: FastAPI) -> None:
    """Reject clearly oversized uploads from the Content-Length header before
    the request body is spooled to disk. The streamed per-chunk limit in the
    handler remains the authoritative backstop."""

    @app.middleware("http")
    async def reject_oversized_upload_bodies(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method.upper() == "POST" and request.url.path in _UPLOAD_BODY_PATHS:
            declared = request.headers.get("content-length")
            if declared is not None:
                try:
                    declared_bytes = int(declared)
                except ValueError:
                    declared_bytes = None
                if (
                    declared_bytes is not None
                    and declared_bytes > MAX_UPLOAD_BYTES + CONTENT_LENGTH_SLACK_BYTES
                ):
                    return JSONResponse(
                        status_code=413,
                        content={
                            "detail": f"Upload body exceeds {MAX_UPLOAD_BYTES} bytes"
                        },
                    )
        return await call_next(request)


class TextUploadRequest(BaseModel):
    text: str = Field(..., min_length=1)
    title: str | None = None


@router.post("/api/uploads/source")
def upload_source(controller: ControllerDep, file: UploadFile = File(...)) -> dict:
    try:
        return controller.upload_source(
            filename=file.filename or "",
            file_obj=file.file,
            max_bytes=MAX_UPLOAD_BYTES,
        )
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        file.file.close()


@router.post("/api/uploads/text")
def upload_text_source(payload: TextUploadRequest, controller: ControllerDep) -> dict:
    try:
        return controller.upload_text_source(
            text=payload.text,
            title=payload.title,
            max_bytes=MAX_UPLOAD_BYTES,
        )
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
