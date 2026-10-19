"""Terminal notification failures must not change a conversion's outcome."""

from __future__ import annotations

import threading

import pytest

from modules.app.controller import ConversionCallbacks, ConversionJob, JobStatus
from modules.app.events import JobState
from modules.app.services.conversion_service import ConversionRuntimeOptions
from modules.pipeline.models import ConversionResult
from tests.test_conversion_service import _make_service


@pytest.mark.parametrize("cancel_before_attach", [False, True])
def test_cancelled_job_survives_broken_terminal_notification(tmp_path, cancel_before_attach):
    source = tmp_path / "book.txt"
    source.write_text("A short cancellation example.", encoding="utf-8")
    init_entered = threading.Event()
    release_init = threading.Event()
    convert_entered = threading.Event()
    cancelled = threading.Event()

    class CancellablePipeline:
        def __init__(self, **kwargs):
            init_entered.set()
            if cancel_before_attach:
                assert release_init.wait(5)

        def cancel(self):
            cancelled.set()

        def convert(self, **kwargs):
            convert_entered.set()
            assert cancelled.wait(5)
            return ConversionResult(success=False, title="Book", author="Author", error="Cancelled")

    states = []
    errors = []

    def on_event(event):
        state = getattr(event, "state", None)
        if state is not None:
            states.append(state)
        if state == JobState.CANCELLED:
            raise RuntimeError("notification transport unavailable")

    service = _make_service(
        tmp_path, CancellablePipeline, job_cls=ConversionJob, status_enum=JobStatus,
    )
    job = service.start_conversion(
        source_path=source,
        runtime_options=ConversionRuntimeOptions(voice="af_bella"),
        callbacks=ConversionCallbacks(on_event=on_event, on_error=errors.append),
    )
    try:
        assert (init_entered if cancel_before_attach else convert_entered).wait(5)
        assert service.cancel_conversion(expected_job_id=job.id)
    finally:
        release_init.set()
        cancelled.set()
        assert job.wait(timeout=5)

    assert job.status == JobStatus.CANCELLED
    assert job.error == ("conversion cancelled" if cancel_before_attach else "Cancelled")
    assert states == [JobState.RUNNING, JobState.CANCELLED]
    assert errors == []
    assert job.pipeline is None
    assert convert_entered.is_set() is not cancel_before_attach
    service._preflight_job_slot_available()
