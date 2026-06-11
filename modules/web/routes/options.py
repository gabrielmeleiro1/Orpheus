"""Model and capability routes."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from modules.web.deps import ControllerDep
from modules.tts.cleaner_api import fetch_openai_model_ids
from modules.tts.cleaner_backends.metadata import list_cleaner_backend_options
from modules.tts.cleaner_config import DEFAULT_API_BASE_URL, list_cuda_cleaner_models

router = APIRouter()


class OpenAIModelListRequest(BaseModel):
    api_key: str = Field(..., min_length=1)


class ParallelWarmupRequest(BaseModel):
    voice: str = "am_adam"
    speed: float = 1.0
    tts_engine: Optional[str] = None
    tts_quantization: Optional[str] = None
    cleaner_model: Optional[str] = None
    cleaner_backend: Optional[str] = None
    cleaner_cuda_device: Optional[str] = None
    cleaner_cuda_quantization: Optional[str] = None
    cleaner_api_model: Optional[str] = None
    cleaner_api_key: Optional[str] = None
    max_parallel_chapters: int = Field(default=4, ge=1)


@router.get("/api/options/models")
def get_model_options(controller: ControllerDep) -> dict:
    return {
        "tts_engines": controller.get_available_tts_engines(),
        "cleaner_models": controller.get_available_cleaner_models(),
        "api_cleaner_models": controller.get_available_api_cleaner_models(),
        "cuda_cleaner_models": list_cuda_cleaner_models(),
        "cleaner_backend_options": list_cleaner_backend_options(),
    }


@router.get("/api/options/voices")
def get_voice_options(controller: ControllerDep, engine: Optional[str] = None) -> dict:
    selected_engine = (
        engine or getattr(getattr(controller, "config", None), "tts_engine", "kokoro")
    ).lower()
    return {
        "engine": selected_engine,
        "voices": controller.get_available_voices(engine_name=selected_engine),
    }


@router.post("/api/options/openai/models")
def list_openai_models(payload: OpenAIModelListRequest) -> dict:
    models = fetch_openai_model_ids(api_key=payload.api_key, base_url=DEFAULT_API_BASE_URL)
    return {
        "provider": "openai",
        "base_url": DEFAULT_API_BASE_URL,
        "models": models,
    }


@router.post("/api/options/parallel/warmup")
def warmup_parallel_workers(
    payload: ParallelWarmupRequest,
    controller: ControllerDep,
) -> dict:
    return controller.warmup_parallel_worker_recommendation(
        voice=payload.voice,
        speed=payload.speed,
        tts_engine=payload.tts_engine,
        tts_quantization=payload.tts_quantization,
        cleaner_model=payload.cleaner_model,
        cleaner_backend=payload.cleaner_backend,
        cleaner_cuda_device=payload.cleaner_cuda_device,
        cleaner_cuda_quantization=payload.cleaner_cuda_quantization,
        cleaner_api_model=payload.cleaner_api_model,
        cleaner_api_key=payload.cleaner_api_key,
        max_parallel_chapters=payload.max_parallel_chapters,
    )


@router.get("/api/system/capabilities")
def get_system_capabilities(controller: ControllerDep) -> dict:
    return controller.get_system_capabilities()
