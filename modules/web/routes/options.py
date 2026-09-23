"""Model and capability routes."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from modules.web.deps import ControllerDep, call_with_http_errors

router = APIRouter()


class OpenAIModelListRequest(BaseModel):
    api_key: str = Field(..., min_length=1)


class ParallelWarmupRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    voice: str = "am_adam"
    speed: float = Field(default=1.0, ge=0.5, le=3.0)
    tts_engine: str | None = None
    tts_quantization: str | None = None
    cleaner_model: str | None = None
    cleaner_backend: str | None = None
    cleaner_cuda_device: str | None = None
    cleaner_cuda_quantization: str | None = None
    cleaner_api_model: str | None = None
    cleaner_api_key: str | None = None
    max_parallel_chapters: int = Field(default=4, ge=1)


@router.get("/api/options/models")
def get_model_options(controller: ControllerDep, request: Request) -> dict:
    result = {
        "tts_engines": controller.get_available_tts_engines(),
        "cleaner_models": controller.get_available_cleaner_models(),
        "api_cleaner_models": controller.get_available_api_cleaner_models(),
        "cuda_cleaner_models": controller.get_cuda_cleaner_models(),
        "cleaner_backend_options": controller.get_cleaner_backend_options(),
    }
    # Once setup is configured, conversion offers downloaded local models only.
    # Installation and removal belong to Settings, not an implicit model load.
    from modules.web.routes.setup import _get_status_cache, _check_model_group_assets
    from scripts import setup_runtime
    from scripts.download_models import MODEL_GROUPS

    state, _ = setup_runtime._read_setup_state(setup_runtime.default_state_path())
    if state is not None:
        groups = [group for group in MODEL_GROUPS if group.startswith("cleaner-")]
        assets = _check_model_group_assets(groups, receipt_verifier=_get_status_cache(request).receipt_verifier)
        installed = {MODEL_GROUPS[group][0] for group in groups if assets[group]["ready"]}
        for key in ("cleaner_models", "cuda_cleaner_models"):
            result[key] = [model for model in result[key] if model in installed]
        options = []
        for option in result["cleaner_backend_options"]:
            option = dict(option)
            if option.get("is_local") and option.get("requires_model", True):
                option["models"] = [model for model in option.get("models", []) if model in installed]
                if not option["models"]:
                    continue
            options.append(option)
        result["cleaner_backend_options"] = options
    return result


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
def list_openai_models(payload: OpenAIModelListRequest, controller: ControllerDep) -> dict:
    # A rejected/expired key is an upstream failure, not an internal one; surface
    # the provider's message as 502 rather than a bare, opaque 500.
    return call_with_http_errors(
        lambda: controller.list_openai_models(api_key=payload.api_key),
        runtime_status=502,
    )


@router.post("/api/options/parallel/warmup")
def warmup_parallel_workers(
    payload: ParallelWarmupRequest,
    controller: ControllerDep,
) -> dict:
    return call_with_http_errors(
        lambda: controller.warmup_parallel_worker_recommendation(
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
    )


@router.get("/api/system/capabilities")
def get_system_capabilities(controller: ControllerDep) -> dict:
    return controller.get_system_capabilities()
