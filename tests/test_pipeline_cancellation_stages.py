"""Cancellation observed inside a stage is reported as a cancellation, not a failure."""

from __future__ import annotations

import logging

import pytest

from modules.pipeline.models import PipelineStage

from tests._factories_group2_conversion import SOURCE


@pytest.mark.parametrize("mode", ["audiobook", "document_audio"])
def test_cancel_during_packaging_reports_cancelled_and_leaves_no_partial_output(
    conversion, mode, caplog,
):
    source, _engine, make_pipeline = conversion
    pipeline = make_pipeline(conversion_mode=mode)

    def cancel_when_packaging(stage, *args):
        if stage == PipelineStage.PACKAGING:
            pipeline.cancel()

    pipeline.progress_callback = cancel_when_packaging
    with caplog.at_level(logging.ERROR, logger="modules.pipeline.orchestrator"):
        result = pipeline.convert(source, title="Tiny Book")

    assert not result.success
    assert result.error == "Cancelled"
    assert pipeline.stage == PipelineStage.CANCELLED
    assert result.output_path is None
    # A user cancel is not logged as a pipeline failure with a traceback.
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    output_dir = pipeline.config.output_dir
    assert not list(output_dir.rglob("*.m4b")) and not list(output_dir.rglob("*.m4a"))
    assert not list(output_dir.rglob("*.tmp.*"))
    assert source.read_bytes() == SOURCE.read_bytes()
