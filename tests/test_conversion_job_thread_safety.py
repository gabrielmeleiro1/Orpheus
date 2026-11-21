"""Concurrency tests for ConversionJob's internal synchronization.

These use threads + barriers to force overlap deterministically. There are no
sleeps used as synchronization; correctness is asserted via invariants that must
hold under every interleaving.
"""

from __future__ import annotations

import threading

from modules.app.controller import ConversionJob, JobStatus
from modules.storage.models import ConversionResult


class _RecordingPipeline:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.cancel_count = 0

    def cancel(self) -> None:
        with self._lock:
            self.cancel_count += 1


def test_cancel_during_pipeline_attach_never_loses_cancellation() -> None:
    # Whether cancel() lands before or after the worker attaches the pipeline,
    # the pipeline must be cancelled exactly once and cancel_requested must stick.
    for i in range(300):
        job = ConversionJob(id=f"job-{i}", book_id=None, status=JobStatus.RUNNING)
        pipeline = _RecordingPipeline()
        barrier = threading.Barrier(2)

        def canceller() -> None:
            barrier.wait()
            job.cancel()

        def worker() -> None:
            barrier.wait()
            if job.attach_pipeline(pipeline):
                pipeline.cancel()

        threads = [threading.Thread(target=canceller), threading.Thread(target=worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()

        assert job.cancel_requested is True
        assert pipeline.cancel_count == 1


def test_read_state_snapshot_never_observes_torn_completion() -> None:
    # complete() sets status and book_id atomically. A concurrent reader must
    # never see COMPLETED with a stale book_id, nor RUNNING with a book_id set.
    for i in range(300):
        expected_book_id = i + 1
        job = ConversionJob(id=f"job-{i}", book_id=None, status=JobStatus.RUNNING)
        barrier = threading.Barrier(2)
        torn: list[tuple] = []

        def writer() -> None:
            barrier.wait()
            job.complete(expected_book_id)

        def reader() -> None:
            barrier.wait()
            for _ in range(2000):
                _id, book_id, status, _error = job.read_state()
                if status is JobStatus.COMPLETED:
                    if book_id != expected_book_id:
                        torn.append((status, book_id))
                    break
                if status is JobStatus.RUNNING and book_id is not None:
                    torn.append((status, book_id))
                    break

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()

        assert torn == []


def test_fail_preserves_existing_successful_result_atomically() -> None:
    job = ConversionJob(id="job", book_id=None, status=JobStatus.RUNNING)
    failure_result = ConversionResult(
        success=False,
        output_path=None,
        total_duration_ms=0,
        error_message="boom",
        chapters_completed=0,
        chapters_total=0,
    )
    job.fail("boom", failure_result)

    _id, _book_id, status, error = job.read_state()
    assert status is JobStatus.FAILED
    assert error == "boom"
    assert job.result is failure_result


def test_stale_cancel_does_not_cancel_successor(tmp_path):
    from unittest.mock import Mock

    from modules.web.routes.conversions import cancel_conversion
    from tests.test_conversion_service import _make_service

    pool = Mock()
    service = _make_service(tmp_path, None, job_cls=ConversionJob, status_enum=JobStatus,
                            shared_worker_pool_manager=pool)
    old = ConversionJob(id="old", book_id=None, status=JobStatus.RUNNING)
    successor = ConversionJob(id="new", book_id=None, status=JobStatus.RUNNING)
    checked = threading.Event()
    resume = threading.Event()

    class Controller:
        def get_active_job(self):
            checked.set()
            assert resume.wait(2)
            return old

        def cancel_conversion(self, *args, **kwargs):
            return service.cancel_conversion(*args, **kwargs)

    service._active_job = old
    responses = []
    request = threading.Thread(target=lambda: responses.append(cancel_conversion("old", Controller())))
    request.start()
    assert checked.wait(2)
    old.complete(1)
    service._active_job = successor
    resume.set()
    request.join(2)

    assert not request.is_alive()
    assert responses == [{"job_id": "old", "cancelled": False}]
    assert not successor.cancel_requested
    pool.shutdown.assert_not_called()


def test_successor_waits_for_captured_job_cancellation(tmp_path):
    import pytest
    from unittest.mock import Mock

    from modules.errors import ConversionCleanupPendingError
    from tests.test_conversion_service import _make_service

    entered = threading.Event()
    resume = threading.Event()
    pool = Mock()
    service = _make_service(tmp_path, None, job_cls=ConversionJob, status_enum=JobStatus,
                            shared_worker_pool_manager=pool)
    job = ConversionJob(id="old", book_id=None, status=JobStatus.RUNNING)
    service._active_job = job

    def cancel():
        entered.set()
        assert resume.wait(2)

    job.pipeline = Mock(cancel=cancel)
    request = threading.Thread(target=service.cancel_conversion)
    request.start()
    assert entered.wait(2)
    job.complete(1)
    try:
        assert service.get_active_job() is job
        with pytest.raises(ConversionCleanupPendingError):
            service._preflight_job_slot_available()
    finally:
        resume.set()
        request.join(2)
    service._preflight_job_slot_available()
    pool.shutdown.assert_called_once()


def test_start_notification_failure_fails_job_and_releases_slot(tmp_path):
    import pytest
    from unittest.mock import Mock

    from modules.app.controller import ConversionCallbacks
    from modules.app.events import JobState
    from tests.test_conversion_service import _make_service

    service = _make_service(tmp_path, None, job_cls=ConversionJob, status_enum=JobStatus)
    job = ConversionJob(id="launch", book_id=None, status=JobStatus.IDLE)
    service._active_job = job
    events = []
    errors = []
    target = Mock()

    def on_event(event):
        events.append(event.state)
        raise RuntimeError("notification unavailable")

    callbacks = ConversionCallbacks(on_event=on_event, on_error=errors.append)
    with pytest.raises(RuntimeError, match="notification unavailable"):
        service._start_pipeline_job(job=job, target=target, callbacks=callbacks,
                                    started_message="started")
    assert job.status == JobStatus.FAILED
    assert not job.result.success
    assert errors == ["notification unavailable"]
    assert events == [JobState.RUNNING, JobState.FAILED]
    target.assert_not_called()
    service._preflight_job_slot_available()


def test_thread_launch_failure_notifies_failure_once_and_releases_slot(tmp_path, monkeypatch):
    import pytest
    from unittest.mock import Mock

    from modules.app.controller import ConversionCallbacks
    from modules.app.events import JobState
    from tests.test_conversion_service import _make_service

    service = _make_service(tmp_path, None, job_cls=ConversionJob, status_enum=JobStatus)
    job = ConversionJob(id="launch", book_id=None, status=JobStatus.IDLE)
    service._active_job = job
    events = []
    errors = []
    target = Mock()

    def on_error(message):
        errors.append(message)
        raise ValueError("error notification unavailable")

    monkeypatch.setattr(threading.Thread, "start", Mock(side_effect=RuntimeError("cannot start thread")))
    callbacks = ConversionCallbacks(on_event=lambda event: events.append(event.state), on_error=on_error)
    with pytest.raises(RuntimeError, match="cannot start thread"):
        service._start_pipeline_job(job=job, target=target, callbacks=callbacks,
                                    started_message="started")
    assert job.status == JobStatus.FAILED
    assert errors == ["cannot start thread"]
    assert events == [JobState.RUNNING, JobState.FAILED]
    target.assert_not_called()
    service._preflight_job_slot_available()


def test_service_cancel_during_pipeline_attach_delivers_cancellation_once(tmp_path, monkeypatch):
    from tests.test_conversion_service import _make_service

    service = _make_service(tmp_path, None, job_cls=ConversionJob, status_enum=JobStatus)
    job = ConversionJob(id="attaching", book_id=None, status=JobStatus.RUNNING)
    service._active_job = job
    pipeline = _RecordingPipeline()
    request_cancel = job.request_cancel

    def attach_before_delivery():
        cancellation = request_cancel()
        if job.attach_pipeline(pipeline):
            pipeline.cancel()
        return cancellation

    monkeypatch.setattr(job, "request_cancel", attach_before_delivery)
    assert service.cancel_conversion(expected_job_id=job.id)
    assert pipeline.cancel_count == 1
