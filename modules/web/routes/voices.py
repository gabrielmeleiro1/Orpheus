"""Voice preview and suggestion routes."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import FileResponse
from starlette.types import Receive, Scope, Send

from modules.contracts.limits import MAX_SPEED, MIN_SPEED
from modules.web.deps import ControllerDep, TempDirDep, call_with_http_errors
from modules.web.serializers import safe_unlink
from modules.web.source_handles import resolve_source_reference

router = APIRouter()


class _EphemeralFileResponse(FileResponse):
    """Serve a generated file and remove it however the response ends.

    A background task runs only after a complete send, so a client that
    disconnects mid-preview would otherwise leave the WAV in the temp folder.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            safe_unlink(Path(self.path))


class VoicePreviewRequest(BaseModel):
    # Reject NaN/±Infinity so a non-finite speed 422s instead of 500ing.
    model_config = ConfigDict(allow_inf_nan=False)

    voice: str = Field(..., min_length=1)
    speed: float = Field(default=1.0, ge=MIN_SPEED, le=MAX_SPEED)
    text: Optional[str] = Field(default=None, max_length=320)
    tts_engine: Optional[str] = None
    tts_quantization: Optional[str] = None
    tts_provider: Optional[str] = None
    tts_device: Optional[str] = None
    tts_options: Optional[dict[str, object]] = None


class VoiceSuggestionRequest(BaseModel):
    source_path: str = Field(..., min_length=1)
    tts_engine: Optional[str] = None
    cleaner_backend: Optional[str] = None
    cleaner_api_model: Optional[str] = None
    cleaner_api_key: Optional[str] = Field(default=None, repr=False)


@router.post("/api/voices/preview")
def post_voice_preview(payload: VoicePreviewRequest, controller: ControllerDep) -> FileResponse:
    preview_kwargs = {
        "voice": payload.voice,
        "speed": payload.speed,
        "text": payload.text,
        "engine_name": payload.tts_engine,
        "tts_quantization": payload.tts_quantization,
        "tts_options": payload.tts_options,
    }
    if payload.tts_provider is not None:
        preview_kwargs["tts_provider"] = payload.tts_provider
    if payload.tts_device is not None:
        preview_kwargs["tts_device"] = payload.tts_device

    preview_path = call_with_http_errors(
        lambda: controller.generate_voice_preview(**preview_kwargs)
    )

    return _EphemeralFileResponse(
        path=preview_path,
        media_type="audio/wav",
        filename=preview_path.name,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/voices/suggest")
def post_voice_suggestion(
    payload: VoiceSuggestionRequest,
    controller: ControllerDep,
    temp_dir: TempDirDep,
) -> dict:
    return call_with_http_errors(
        lambda: controller.suggest_voice_for_book(
            source_path=resolve_source_reference(payload.source_path, temp_dir),
            engine_name=payload.tts_engine,
            **payload.model_dump(include={"cleaner_backend", "cleaner_api_model", "cleaner_api_key"}, exclude_none=True),
        )
    )
