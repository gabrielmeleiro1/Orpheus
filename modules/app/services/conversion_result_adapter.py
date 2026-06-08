"""Typed adapter between pipeline and storage conversion results."""

from __future__ import annotations

from typing import Optional

from modules.pipeline.models import ConversionResult as PipelineConversionResult
from modules.pipeline.result_validation import (
    ConversionArtifactValidation,
    format_failure_notes,
    validate_conversion_success_artifacts,
)
from modules.storage.models import ConversionResult as ControllerConversionResult


def success_artifact_error(
    validation: ConversionArtifactValidation,
) -> Optional[str]:
    """Return the controller error for a successful pipeline result with bad artifacts."""
    notes = list(validation.chapter_notes or [])
    if validation.packaged_output_note is not None:
        return (
            "Conversion reported success with invalid artifacts. "
            f"{validation.packaged_output_note}"
        )
    if notes:
        summary = format_failure_notes(notes)
        return f"Conversion reported success with invalid artifacts. {summary}"
    return None


def pipeline_to_controller_result(
    pipeline_result: PipelineConversionResult,
    *,
    emit_chapter_mp3_artifacts: bool = False,
    require_packaged_output: bool = False,
) -> ControllerConversionResult:
    """Map a pipeline result to the service/storage result contract."""
    validation = validate_conversion_success_artifacts(
        pipeline_result,
        emit_chapter_mp3_artifacts=emit_chapter_mp3_artifacts,
        require_packaged_output=require_packaged_output,
    )
    validation_error = (
        success_artifact_error(validation)
        if pipeline_result.success
        else None
    )

    return ControllerConversionResult(
        success=pipeline_result.success and validation_error is None,
        output_path=str(pipeline_result.output_path)
        if pipeline_result.output_path
        else None,
        total_duration_ms=pipeline_result.total_duration_ms,
        error_message=validation_error or pipeline_result.error,
        chapters_completed=validation.chapters_completed,
        chapters_total=len(pipeline_result.chapters),
    )
