"""Conversion job routes."""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, create_model

from modules.app import ConversionCallbacks, ConversionRuntimeOptions
from modules.contracts.conversion_runtime_options import (
    CONVERSION_RUNTIME_OPTION_SPECS,
    build_runtime_option_pydantic_fields,
    conversion_runtime_option_field_names,
)
from modules.app.services.repair_runtime import repair_runtime_fields_set
from modules.contracts.conversion_runtime import merge_runtime_options
from modules.contracts.limits import MAX_BOOK_TITLE_LENGTH
from modules.web.deps import ControllerDep, EventBrokerDep, TempDirDep, call_with_http_errors
from modules.web.serializers import job_payload, job_status_value
from modules.web.source_handles import resolve_source_reference

router = APIRouter()


class _ConversionRuntimeOptionsRequestMixin:
    def to_app_options(self) -> ConversionRuntimeOptions:
        return ConversionRuntimeOptions(
            **{
                name: getattr(self, name)
                for name in conversion_runtime_option_field_names()
            }
        )


ConversionRuntimeOptionsRequest = create_model(
    "ConversionRuntimeOptionsRequest",
    __base__=(BaseModel, _ConversionRuntimeOptionsRequestMixin),
    **build_runtime_option_pydantic_fields(CONVERSION_RUNTIME_OPTION_SPECS),
)


class _RuntimeOptionsRequest(ConversionRuntimeOptionsRequest):
    runtime_options: Optional[ConversionRuntimeOptionsRequest] = None

    def to_runtime_options(self) -> ConversionRuntimeOptions:
        if self.runtime_options is None:
            return self.to_app_options()

        return merge_runtime_options(
            self.to_app_options(),
            self.runtime_options.to_app_options(),
            nested_fields_set=set(self.runtime_options.model_fields_set),
        )


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
            runtime_fields_set=repair_runtime_fields_set(payload),
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
