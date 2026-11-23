"""Library routes."""

from __future__ import annotations

import mimetypes
import os
from secrets import token_hex

import anyio
from contextlib import contextmanager, nullcontext
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.responses import FileResponse as BaseFileResponse
from starlette.responses import Response

from modules.storage.sqlite_repo import LibraryReplacedError
from modules.contracts.limits import MAX_BOOK_AUTHOR_LENGTH, MAX_BOOK_TAGS_LENGTH, MAX_BOOK_TITLE_LENGTH
from modules.contracts.public_errors import PublicError
from modules.web.deps import public_http_error, ControllerDep, call_with_http_errors
from modules.web.serializers import book_summary_to_dict, book_to_dict

router = APIRouter()

# Library reads retried when a restore replaces the library mid-read.
_LIBRARY_READ_ATTEMPTS = 3

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


class FileResponse(BaseFileResponse):
    """Serve the descriptor opened while library identity was protected."""

    def __init__(self, *, file, **kwargs):
        super().__init__(stat_result=os.fstat(file.fileno()), **kwargs)
        self._opened_file = file
        self._file = anyio.wrap_file(file)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._opened_file.close()

    async def _send_bytes(self, send, start, end):
        await self._file.seek(start)
        while start < end:
            chunk = await self._file.read(min(self.chunk_size, end - start))
            if not chunk:
                break
            start += len(chunk)
            await send({"type": "http.response.body", "body": chunk, "more_body": True})

    async def _handle_simple(self, send, send_header_only, send_pathsend):
        del send_pathsend  # Always streams the pinned descriptor.
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        if not send_header_only:
            await self._send_bytes(send, 0, self.stat_result.st_size)
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _handle_single_range(self, send, start, end, file_size, send_header_only):
        self.headers["content-range"] = f"bytes {start}-{end - 1}/{file_size}"
        self.headers["content-length"] = str(end - start)
        await send({"type": "http.response.start", "status": 206, "headers": self.raw_headers})
        if not send_header_only:
            await self._send_bytes(send, start, end)
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _handle_multiple_ranges(self, send, ranges, file_size, send_header_only):
        boundary = token_hex(13)
        length, header = self.generate_multipart(ranges, boundary, file_size, self.headers["content-type"])
        self.headers["content-type"] = f"multipart/byteranges; boundary={boundary}"
        self.headers["content-length"] = str(length)
        await send({"type": "http.response.start", "status": 206, "headers": self.raw_headers})
        if not send_header_only:
            for start, end in ranges:
                await send({"type": "http.response.body", "body": header(start, end), "more_body": True})
                await self._send_bytes(send, start, end)
                # Match the framing counted by the inherited multipart generator.
                await send({"type": "http.response.body", "body": b"\r\n", "more_body": True})
            await send({"type": "http.response.body", "body": f"--{boundary}--".encode("latin-1"), "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


class PlaybackPositionRequest(BaseModel):
    # Reject NaN/±Infinity: they otherwise 500 the handler or persist a
    # non-finite position that later serializes as invalid JSON.
    model_config = ConfigDict(allow_inf_nan=False)

    position_sec: float = Field(..., ge=0)
    expected_epoch: str | None = None


class LibraryBookUpdateRequest(BaseModel):
    """Library-only metadata: edits are not written back into audio file tags."""

    expected_epoch: str | None = None
    title: Optional[str] = Field(default=None, max_length=MAX_BOOK_TITLE_LENGTH)
    author: Optional[str] = Field(default=None, max_length=MAX_BOOK_AUTHOR_LENGTH)
    tags: Optional[str] = Field(default=None, max_length=MAX_BOOK_TAGS_LENGTH)
    favorite: Optional[bool] = None

    @field_validator("title")
    @classmethod
    def title_required(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("Title is required")
        return value.strip()


def _playback_media_type(output_path) -> str:
    mapped_type = _AUDIO_MEDIA_TYPES.get(output_path.suffix.lower())
    if mapped_type:
        return mapped_type

    media_type, _ = mimetypes.guess_type(str(output_path))
    return media_type or "application/octet-stream"


@contextmanager
def library_epoch_errors():
    try:
        yield
    except LibraryReplacedError as error:
        raise public_http_error(PublicError(error.code, str(error), 409)) from error


def library_snapshot(controller, expected_epoch: str | None = None):
    snapshot = getattr(controller, "library_snapshot", None)
    if callable(snapshot):
        return snapshot(expected_epoch)
    if expected_epoch is not None:
        raise LibraryReplacedError()
    return nullcontext({})


@router.get("/api/library")
def list_library(
    controller: ControllerDep,
    search: Optional[str] = None,
    conversion_mode: Optional[str] = None,
    favorite: Optional[bool] = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    def read_books():
        return controller.get_library_books(
            search=search,
            limit=limit,
            offset=offset,
            conversion_mode=conversion_mode,
            favorite=favorite,
        )

    def current_identity() -> dict:
        with library_snapshot(controller) as identity:
            return dict(identity)

    # The service reads the rows under the repository lock and checks their
    # files after releasing it. Restore is the only identity change and every
    # restore gets a new epoch, so an identity unchanged across the read
    # describes the rows that were read.
    for _attempt in range(_LIBRARY_READ_ATTEMPTS):
        identity = current_identity()
        books = read_books()
        if current_identity() == identity:
            break
    else:
        with library_snapshot(controller) as identity:
            identity = dict(identity)
            books = read_books()
    return {
        **identity,
        "items": [{**book_summary_to_dict(book), **identity} for book in books],
        "count": len(books),
        "limit": limit,
        "offset": offset,
    }


@router.get("/api/library/count")
def count_library_books(controller: ControllerDep) -> dict:
    return {"count": controller.get_library_count()}


@router.get("/api/library/{book_id}")
def get_library_book(book_id: int, controller: ControllerDep) -> dict:
    with library_snapshot(controller) as identity:
        book = controller.get_book(book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="Book not found")

        payload = {**book_to_dict(book), **identity}
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
    with library_epoch_errors(), library_snapshot(controller) as identity:
        updated = controller.update_book_metadata(
            book_id,
            title=payload.title,
            author=(payload.author or "") if "author" in payload.model_fields_set else None,
            tags=(payload.tags or "") if "tags" in payload.model_fields_set else None,
            favorite=payload.favorite,
            **({"expected_epoch": payload.expected_epoch} if payload.expected_epoch is not None else {}),
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Book not found")
        updated = book_to_dict(updated)
        updated.update(identity)
        updated["playback_url"] = f"/api/library/{book_id}/audio" if updated.get("playback_path") else None
        return updated


@router.put("/api/library/{book_id}/playback-position")
def update_library_book_playback_position(
    book_id: int,
    payload: PlaybackPositionRequest,
    controller: ControllerDep,
) -> dict:
    with library_epoch_errors(), library_snapshot(controller) as identity:
        updated_position = controller.update_book_playback_position(
            book_id, payload.position_sec,
            **({"expected_epoch": payload.expected_epoch} if payload.expected_epoch is not None else {}),
        )
        if updated_position is None:
            raise HTTPException(status_code=404, detail="Book not found")

        return {
            "book_id": book_id,
            "position_sec": updated_position,
            **identity,
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
def delete_library_book(book_id: int, controller: ControllerDep, expected_epoch: str | None = None) -> dict:
    with library_epoch_errors():
        delete = getattr(controller, "delete_book_with_identity", controller.delete_book)
        deleted = call_with_http_errors(
            lambda: delete(book_id, **({"expected_epoch": expected_epoch} if expected_epoch is not None else {})),
            runtime_status=409,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Book not found")

        return {
            "book_id": book_id,
            "deleted": True,
            **(deleted if isinstance(deleted, dict) else {}),
        }


@router.get("/api/library/{book_id}/cover")
def get_library_cover(book_id: int, controller: ControllerDep) -> Response:
    try:
        cover = controller.get_book_cover(book_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Book not found") from error
    if cover is None:
        # Text sources have no cover; that is an answer, not an error, so the UI
        # draws its generated cover without logging a failed request.
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    return Response(
        content=cover.data,
        media_type=cover.media_type,
        # A restored library can reuse book IDs, and sources can be replaced.
        # The desktop already caches covers by its library/source generation.
        headers={"Cache-Control": "no-store"},
    )


@router.api_route("/api/library/{book_id}/audio", methods=["GET", "HEAD"])
def get_library_audio(
    book_id: int,
    request: Request,
    controller: ControllerDep,
    chapter_number: int | None = Query(default=None, ge=1),
) -> FileResponse:
    expected_epoch = getattr(request.state, "audio_library_epoch", None)
    try:
        with library_snapshot(controller, expected_epoch):
            output_path = (
                controller.get_book_playback_file(book_id)
                if chapter_number is None
                else controller.get_book_playback_file(book_id, chapter_number=chapter_number)
            )
            if output_path is None:
                raise HTTPException(status_code=404, detail="Playable audio not found")
            try:
                file = output_path.open("rb")
            except FileNotFoundError as error:
                raise HTTPException(status_code=404, detail="Playable audio not found") from error
            try:
                return FileResponse(
                    file=file,
                    path=output_path,
                    media_type=_playback_media_type(output_path),
                    filename=output_path.name,
                    content_disposition_type="inline",
                )
            except BaseException:
                file.close()
                raise
    except LibraryReplacedError as error:
        raise HTTPException(status_code=401, detail="Audio token belongs to a replaced library") from error
