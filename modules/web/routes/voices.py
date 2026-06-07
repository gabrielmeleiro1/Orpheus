"""Voice preview and suggestion routes."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from modules.web.deps import ControllerDep, TempDirDep, call_with_http_errors
from modules.web.serializers import safe_unlink
from modules.web.source_handles import resolve_source_reference

router = APIRouter()


class VoicePreviewRequest(BaseModel):
    voice: str = Field(..., min_length=1)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    text: Optional[str] = Field(default=None, max_length=320)
    tts_engine: Optional[str] = None
    tts_quantization: Optional[str] = None
    tts_provider: Optional[str] = None
    tts_device: Optional[str] = None
    tts_options: Optional[dict[str, object]] = None


class VoiceSuggestionRequest(BaseModel):
    source_path: str = Field(..., min_length=1)
    tts_engine: Optional[str] = None


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

    return FileResponse(
        path=preview_path,
        media_type="audio/wav",
        filename=preview_path.name,
        background=BackgroundTask(safe_unlink, preview_path),
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
        )
    )
