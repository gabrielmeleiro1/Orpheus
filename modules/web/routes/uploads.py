"""Source upload routes."""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from modules.app.services.library_service import MAX_UPLOAD_BYTES, UploadTooLargeError
from modules.web.deps import ControllerDep

router = APIRouter()


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
