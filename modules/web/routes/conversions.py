"""Conversion job routes."""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from modules.app import ConversionCallbacks, ConversionRuntimeOptions
from modules.pipeline.book_workspace import MAX_BOOK_TITLE_LENGTH
from modules.web.deps import ControllerDep, EventBrokerDep, TempDirDep, call_with_http_errors
from modules.web.serializers import job_payload, job_status_value
from modules.web.source_handles import resolve_source_reference

router = APIRouter()


def _model_fields_set(model: BaseModel) -> set[str]:
    fields_set = getattr(model, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(model, "__fields_set__", set())
    return set(fields_set)


class ConversionRuntimeOptionsRequest(BaseModel):
    voice: str = "am_adam"
    speed: float = 1.0
    tts_engine: Optional[str] = None
    tts_quantization: Optional[str] = None
    tts_provider: Optional[str] = None
    tts_device: Optional[str] = None
    cleaner_model: Optional[str] = None
    cleaner_backend: Optional[str] = None
    cleaner_cuda_device: Optional[str] = None
    cleaner_cuda_quantization: Optional[str] = None
    cleaner_api_model: Optional[str] = None
    cleaner_api_key: Optional[str] = None
    tts_options: Optional[dict[str, object]] = None
    parallel_worker_mode: Optional[str] = None
    enable_parallel: Optional[bool] = None
    max_parallel_chapters: Optional[int] = Field(default=None, ge=1)

    def to_app_options(self) -> ConversionRuntimeOptions:
        return ConversionRuntimeOptions(
            voice=self.voice,
            speed=self.speed,
            tts_engine=self.tts_engine,
            tts_quantization=self.tts_quantization,
            tts_provider=self.tts_provider,
            tts_device=self.tts_device,
            cleaner_model=self.cleaner_model,
            cleaner_backend=self.cleaner_backend,
            cleaner_cuda_device=self.cleaner_cuda_device,
            cleaner_cuda_quantization=self.cleaner_cuda_quantization,
            cleaner_api_model=self.cleaner_api_model,
            cleaner_api_key=self.cleaner_api_key,
            tts_options=self.tts_options,
            parallel_worker_mode=self.parallel_worker_mode,
            enable_parallel=self.enable_parallel,
            max_parallel_chapters=self.max_parallel_chapters,
        )


class _RuntimeOptionsRequest(ConversionRuntimeOptionsRequest):
    runtime_options: Optional[ConversionRuntimeOptionsRequest] = None

    def to_runtime_options(self) -> ConversionRuntimeOptions:
        if self.runtime_options is None:
            return self.to_app_options()

        flat_options = self.to_app_options()
        nested_options = self.runtime_options.to_app_options()
        nested_fields = _model_fields_set(self.runtime_options)
        option_values = {
            field: (
                getattr(nested_options, field)
                if field in nested_fields
                else getattr(flat_options, field)
            )
            for field in ConversionRuntimeOptions.__dataclass_fields__
        }
        return ConversionRuntimeOptions(**option_values)


class StartConversionRequest(_RuntimeOptionsRequest):
    source_path: str = Field(..., min_length=1)
    conversion_mode: Literal["audiobook", "document_audio"] = "audiobook"
    title: Optional[str] = Field(default=None, max_length=MAX_BOOK_TITLE_LENGTH)
    author: Optional[str] = None


class RepairConversionRequest(_RuntimeOptionsRequest):
    pass


@router.post("/api/conversions")
def start_conversion(
    payload: StartConversionRequest,
    controller: ControllerDep,
    event_broker: EventBrokerDep,
    temp_dir: TempDirDep,
) -> dict:
    job = call_with_http_errors(
        lambda: controller.start_conversion(
            source_path=resolve_source_reference(payload.source_path, temp_dir),
            runtime_options=payload.to_runtime_options(),
            callbacks=ConversionCallbacks(on_event=event_broker.publish),
            conversion_mode=payload.conversion_mode,
            title=payload.title,
            author=payload.author,
        ),
        runtime_status=409,
    )
    return {
        "job_id": job.id,
        "status": job_status_value(job),
        "book_id": getattr(job, "book_id", None),
        "error": getattr(job, "error", None),
    }


@router.get("/api/conversions/active")
def get_active_conversion(controller: ControllerDep) -> dict:
    job = controller.get_active_job()
    if job is None:
        return {"active": False, "job": None}

    is_active = job.is_active() if hasattr(job, "is_active") else job_status_value(job) == "running"
    return {"active": bool(is_active), "job": job_payload(job)}


@router.post("/api/conversions/repair/{book_id}")
def repair_conversion(
    book_id: int,
    payload: RepairConversionRequest,
    controller: ControllerDep,
    event_broker: EventBrokerDep,
) -> dict:
    job = call_with_http_errors(
        lambda: controller.repair_conversion(
            book_id=book_id,
            runtime_options=payload.to_runtime_options(),
            callbacks=ConversionCallbacks(on_event=event_broker.publish),
        ),
        runtime_status=409,
    )
    return {
        "job_id": job.id,
        "status": job_status_value(job),
        "book_id": getattr(job, "book_id", book_id),
        "error": getattr(job, "error", None),
    }


@router.post("/api/conversions/{job_id}/cancel")
def cancel_conversion(job_id: str, controller: ControllerDep) -> dict:
    job = controller.get_active_job()
    if job is None or str(job.id) != job_id:
        raise HTTPException(status_code=404, detail="Active job not found")

    cancelled = controller.cancel_conversion()
    return {"job_id": job_id, "cancelled": bool(cancelled)}
