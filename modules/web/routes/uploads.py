"""Source upload routes."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from modules.contracts.limits import MAX_BOOK_TITLE_LENGTH, MAX_UPLOAD_BYTES
from modules.contracts.upload_errors import UploadTooLargeError
from modules.web.deps import ControllerDep

router = APIRouter()

# Multipart framing (boundary lines and part headers) rides along with the
# file bytes in Content-Length; allow modest overhead before rejecting.
CONTENT_LENGTH_SLACK_BYTES = 64 * 1024

_UPLOAD_BODY_PATHS = frozenset({"/api/uploads/source", "/api/uploads/text"})

# Bound JSON parsing independently from the authoritative UTF-8 byte check in
# LibraryService. The character bound intentionally matches the byte limit so
# ASCII text retains the exact historical limit; multibyte text remains gated
# by the authoritative encoded-byte check in the service.
MAX_TEXT_CHARS = MAX_UPLOAD_BYTES


class _UploadBodySizeLimitMiddleware:
    """Bound declared and chunked upload bodies before framework parsing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method", "").upper() != "POST"
            or scope.get("path") not in _UPLOAD_BODY_PATHS
        ):
            await self.app(scope, receive, send)
            return

        limit = MAX_UPLOAD_BYTES + CONTENT_LENGTH_SLACK_BYTES
        headers = dict(scope.get("headers", []))
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                pass

        received = 0
        too_large = False

        async def limited_receive() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            if not too_large:
                await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not too_large:
                raise
        if too_large:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": f"Upload body exceeds {MAX_UPLOAD_BYTES} bytes"},
        )
        await response(scope, receive, send)


def add_upload_size_limit_middleware(app: FastAPI) -> None:
    app.add_middleware(_UploadBodySizeLimitMiddleware)


class TextUploadRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_TEXT_CHARS)
    title: str | None = Field(default=None, max_length=MAX_BOOK_TITLE_LENGTH)


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
