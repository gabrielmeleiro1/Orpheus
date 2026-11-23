"""Conversion job routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from modules.app import ConversionCallbacks
from modules.app.services.repair_runtime import repair_runtime_fields_set
from modules.storage.sqlite_repo import LibraryReplacedError
from modules.contracts.public_errors import PublicError
from modules.web.events import normalize_event
from modules.web.deps import public_http_error, ControllerDep, EventBrokerDep, TempDirDep, call_with_http_errors
from modules.web.schemas.conversions import (
    RepairConversionRequest,
    StartConversionRequest,
)
from modules.web.serializers import job_payload, job_status_value
from modules.web.source_handles import resolve_source_reference

router = APIRouter()


def _started_job_payload(job, *, default_book_id: int | None = None) -> dict:
    # The worker thread is already running, so read one consistent snapshot.
    payload = job_payload(job)
    return {
        "job_id": payload["id"],
        "client_request_id": payload["client_request_id"],
        "status": payload["status"],
        "book_id": payload["book_id"] if payload["book_id"] is not None else default_book_id,
        "error": payload["error"],
    }


@router.post("/api/conversions")
def start_conversion(
    payload: StartConversionRequest,
    controller: ControllerDep,
    event_broker: EventBrokerDep,
    temp_dir: TempDirDep,
) -> dict:
    try:
        service = getattr(controller, "_conversion_service", None)
        source_path = resolve_source_reference(
            payload.source_path, temp_dir,
            resolve_recovered_upload=getattr(service, "resolve_recovered_upload", None),
        )
    except ValueError as exc:
        if payload.source_path.startswith("upload:"):
            raise HTTPException(status_code=400, detail="Source file is no longer available — add it again") from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    def publish_event(event):
        event_payload = normalize_event(event)
        if event_payload.get("job_id") is not None:
            event_payload["client_request_id"] = payload.client_request_id
        event_broker.publish(event_payload)

    job = call_with_http_errors(
        lambda: controller.start_conversion(
            source_path=source_path,
            runtime_options=payload.to_runtime_options(),
            callbacks=ConversionCallbacks(on_event=publish_event),
            conversion_mode=payload.conversion_mode,
            title=payload.title,
            author=payload.author,
            **({"client_request_id": payload.client_request_id} if payload.client_request_id is not None else {}),
        ),
        runtime_status=409,
    )
    return _started_job_payload(job)


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
    def start_repair():
        try:
            return controller.repair_conversion(
                book_id=book_id,
                runtime_options=payload.to_runtime_options(),
                runtime_fields_set=repair_runtime_fields_set(payload),
                callbacks=ConversionCallbacks(on_event=event_broker.publish),
                **({"expected_epoch": payload.expected_epoch} if payload.expected_epoch is not None else {}),
            )
        except LibraryReplacedError as error:
            raise public_http_error(PublicError(error.code, str(error), 409)) from error

    job = call_with_http_errors(start_repair, runtime_status=409)
    return _started_job_payload(job, default_book_id=book_id)


@router.post("/api/conversions/{job_id}/cancel")
def cancel_conversion(job_id: str, controller: ControllerDep) -> dict:
    job = controller.get_active_job()
    if job is None or str(job.id) != job_id:
        raise HTTPException(status_code=404, detail="Active job not found")

    cancelled = controller.cancel_conversion(expected_job_id=job_id)
    return {"job_id": job_id, "cancelled": bool(cancelled)}
