"""Library routes."""

from __future__ import annotations

import mimetypes
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import FileResponse

from modules.web.deps import ControllerDep, call_with_http_errors
from modules.web.serializers import book_summary_to_dict, book_to_dict

router = APIRouter()

_AUDIO_MEDIA_TYPES = {
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".mp4": "audio/mp4",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".wave": "audio/wav",
}


class PlaybackPositionRequest(BaseModel):
    # Reject NaN/±Infinity: they otherwise 500 the handler or persist a
    # non-finite position that later serializes as invalid JSON.
    model_config = ConfigDict(allow_inf_nan=False)

    position_sec: float = Field(..., ge=0)


class LibraryBookUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1)
    author: Optional[str] = None
    tags: Optional[str] = None
    favorite: Optional[bool] = None


def _playback_media_type(output_path) -> str:
    mapped_type = _AUDIO_MEDIA_TYPES.get(output_path.suffix.lower())
    if mapped_type:
        return mapped_type

    media_type, _ = mimetypes.guess_type(str(output_path))
    return media_type or "application/octet-stream"


@router.get("/api/library")
def list_library(
    controller: ControllerDep,
    search: Optional[str] = None,
    conversion_mode: Optional[str] = None,
    favorite: Optional[bool] = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    books = controller.get_library_books(
        search=search,
        limit=limit,
        offset=offset,
        conversion_mode=conversion_mode,
        favorite=favorite,
    )
    return {
        "items": [book_summary_to_dict(book) for book in books],
        "count": len(books),
        "limit": limit,
        "offset": offset,
    }


@router.get("/api/library/count")
def count_library_books(controller: ControllerDep) -> dict:
    return {"count": controller.get_library_count()}


@router.get("/api/library/{book_id}")
def get_library_book(book_id: int, controller: ControllerDep) -> dict:
    book = controller.get_book(book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")

    payload = book_to_dict(book)
    payload["playback_url"] = f"/api/library/{book_id}/audio" if payload.get("playback_path") else None
    return payload


@router.get("/api/library/{book_id}/chapters/{chapter_number}/cleaning-preview")
def get_library_chapter_cleaning_preview(
    book_id: int,
    chapter_number: int,
    controller: ControllerDep,
) -> dict:
    payload = controller.get_chapter_cleaning_preview(book_id, chapter_number)
    if payload is None:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return payload


@router.patch("/api/library/{book_id}")
def update_library_book(
    book_id: int,
    payload: LibraryBookUpdateRequest,
    controller: ControllerDep,
) -> dict:
    updated = controller.update_book_metadata(
        book_id,
        title=payload.title,
        author=payload.author,
        tags=payload.tags,
        favorite=payload.favorite,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Book not found")
    updated["playback_url"] = f"/api/library/{book_id}/audio" if updated.get("playback_path") else None
    return updated


@router.put("/api/library/{book_id}/playback-position")
def update_library_book_playback_position(
    book_id: int,
    payload: PlaybackPositionRequest,
    controller: ControllerDep,
) -> dict:
    updated_position = controller.update_book_playback_position(book_id, payload.position_sec)
    if updated_position is None:
        raise HTTPException(status_code=404, detail="Book not found")

    return {
        "book_id": book_id,
        "position_sec": updated_position,
    }


@router.post("/api/library/{book_id}/open-folder")
def open_library_book_folder(book_id: int, controller: ControllerDep) -> dict:
    folder_path = call_with_http_errors(lambda: controller.open_book_folder(book_id))
    if folder_path is None:
        raise HTTPException(status_code=404, detail="Book folder not found")

    return {
        "book_id": book_id,
        "folder_path": str(folder_path),
    }


@router.delete("/api/library/{book_id}")
def delete_library_book(book_id: int, controller: ControllerDep) -> dict:
    deleted = call_with_http_errors(
        lambda: controller.delete_book(book_id),
        runtime_status=409,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Book not found")

    return {
        "book_id": book_id,
        "deleted": True,
    }


@router.api_route("/api/library/{book_id}/audio", methods=["GET", "HEAD"])
def get_library_audio(
    book_id: int,
    controller: ControllerDep,
) -> FileResponse:
    output_path = controller.get_book_playback_file(book_id)
    if output_path is None:
        raise HTTPException(status_code=404, detail="Playable audio not found")

    return FileResponse(
        path=output_path,
        media_type=_playback_media_type(output_path),
        filename=output_path.name,
        content_disposition_type="inline",
    )
