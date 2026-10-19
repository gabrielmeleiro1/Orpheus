"""A terminal job keeps resources reserved while exposing retryable admission."""

import threading
from contextlib import contextmanager

import pytest

from modules.app.controller import ConversionCallbacks, ConversionJob, JobStatus
from modules.app.events import JobState
from modules.app.services.job_coordinator import JobCoordinator
from modules.errors import ConversionCleanupPendingError
from modules.pipeline.models import ChapterResult, RepairResult
from tests.test_conversion_service import _make_service, _PipelineStub


@contextmanager
def _terminal_cancellation(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class Pool:
        cleanup_pending = False

        def shutdown(self, **kwargs):
            self.cleanup_pending = True
            entered.set()
            assert release.wait(5)
            return False

    pool = Pool()
    coordinator = JobCoordinator(
        job_cls=ConversionJob, status_enum=JobStatus, shared_worker_pool_manager=pool,
    )
    job = coordinator.reserve_conversion(tmp_path / "source.md", None, None)
    worker_release = threading.Event()
    worker = threading.Thread(target=lambda: worker_release.wait(5))
    job.begin_running(worker)
    worker.start()
    canceller = threading.Thread(target=lambda: coordinator.cancel_conversion(job.id))
    canceller.start()
    try:
        assert entered.wait(3)
        job.mark_cancelled("cancelled")
        worker_release.set()
        assert job.wait(3)
        yield coordinator, pool, release, canceller
    finally:
        release.set()
        worker_release.set()
        canceller.join(3)
        worker.join(3)


def test_storage_cleanup_waits_for_terminal_cancellation_pool_release(tmp_path):
    with _terminal_cancellation(tmp_path) as (coordinator, pool, release, canceller):
        with pytest.raises(ConversionCleanupPendingError):
            coordinator.begin_storage_cleanup()
        release.set()
        canceller.join(3)
        with pytest.raises(ConversionCleanupPendingError):
            coordinator.begin_storage_cleanup()
        with pytest.raises(ConversionCleanupPendingError):
            coordinator.reserve_conversion(tmp_path / "next.md", None, None)
        pool.cleanup_pending = False
        coordinator.begin_storage_cleanup()
        coordinator.end_storage_cleanup()


@pytest.mark.parametrize("cleanup", ["book", "upload"])
def test_book_and_upload_cleanup_wait_for_cancellation_quiescence(tmp_path, cleanup):
    with _terminal_cancellation(tmp_path) as (coordinator, pool, release, canceller):
        def attempt():
            if cleanup == "book":
                coordinator.begin_book_cleanup(99)
                coordinator.end_book_cleanup(99)
            else:
                with coordinator.upload_cleanup_guard(tmp_path / "unrelated.md"):
                    pass

        with pytest.raises(ConversionCleanupPendingError):
            attempt()
        release.set()
        canceller.join(3)
        with pytest.raises(ConversionCleanupPendingError):
            attempt()
        pool.cleanup_pending = False
        attempt()


def test_next_job_can_retry_after_terminal_cleanup_without_overlapping_resources(tmp_path, monkeypatch):
    source = tmp_path / "source.md"
    source.write_text("Book text")
    audio = tmp_path / "book.m4b"
    audio.write_bytes(b"audio")
    result = RepairResult(
        success=True, title="Book", author=None, output_path=audio,
        chapters=[ChapterResult(1, "One", duration_ms=1000)], total_duration_ms=1000,
    )

    class Pipeline(_PipelineStub):
        def __init__(self, **kwargs):
            super().__init__(result=result, **kwargs)

    service = _make_service(tmp_path, Pipeline, job_cls=ConversionJob, status_enum=JobStatus)
    terminal = threading.Event()
    cleanup = threading.Event()
    finish = threading.Event()
    release = service._tts_session.release

    def blocked_release(*args, **kwargs):
        cleanup.set()
        assert finish.wait(5)
        return release(*args, **kwargs)

    def observe(event):
        if getattr(event, "state", None) == JobState.COMPLETED:
            terminal.set()

    monkeypatch.setattr(service._tts_session, "release", blocked_release)
    first = service.start_conversion(source, callbacks=ConversionCallbacks(on_event=observe))
    try:
        assert terminal.wait(3) and cleanup.wait(3)
        assert service.has_active_job()
        with pytest.raises(ConversionCleanupPendingError) as failure:
            service.start_conversion(source)
        assert failure.value.public_error.code == "conversion_cleanup_pending"
        assert service.get_active_job() is first
        with pytest.raises(RuntimeError, match="queued or running"):
            service.begin_storage_cleanup()
    finally:
        finish.set()
        assert first.wait(3)
        service.cleanup()

    second = service.start_conversion(source)
    try:
        assert second.id != first.id
        assert second.wait(3)
        assert second.status == JobStatus.COMPLETED
        assert not service.has_active_job()
    finally:
        service.cleanup()


def test_running_job_conflict_is_not_a_retryable_cleanup_refusal(tmp_path):
    coordinator = JobCoordinator(job_cls=ConversionJob, status_enum=JobStatus)
    coordinator.reserve_conversion(tmp_path / "book.md", None, None)
    with pytest.raises(RuntimeError, match="already in progress") as failure:
        coordinator.reserve_conversion(tmp_path / "next.md", None, None)
    assert not isinstance(failure.value, ConversionCleanupPendingError)


@pytest.mark.parametrize("worker_finished", [False, True], ids=["live-worker", "exited-worker"])
def test_terminal_cancellation_cleanup_remains_retryable_until_pool_release(tmp_path, worker_finished):
    pool_cleanup_entered = threading.Event()
    release_pool = threading.Event()
    release_worker = threading.Event()
    cancellation_results = []

    class Pool:
        def shutdown(self, **kwargs):
            pool_cleanup_entered.set()
            assert release_pool.wait(5)

    coordinator = JobCoordinator(
        job_cls=ConversionJob, status_enum=JobStatus, shared_worker_pool_manager=Pool(),
    )
    first = coordinator.reserve_conversion(tmp_path / "first.md", None, None)
    worker = threading.Thread(target=lambda: release_worker.wait(5))
    first.begin_running(worker)
    worker.start()
    canceller = threading.Thread(
        target=lambda: cancellation_results.append(coordinator.cancel_conversion(first.id)),
    )
    canceller.start()
    try:
        assert pool_cleanup_entered.wait(3)
        # Cancellation has started, but a nonterminal job is still an ordinary
        # running-job conflict and must not trigger automatic client retries.
        with pytest.raises(RuntimeError, match="already in progress") as failure:
            coordinator.reserve_conversion(tmp_path / "next.md", None, None)
        assert not isinstance(failure.value, ConversionCleanupPendingError)

        first.mark_cancelled("conversion cancelled")
        if worker_finished:
            release_worker.set()
            assert first.wait(3)
        else:
            assert worker.is_alive()

        with pytest.raises(ConversionCleanupPendingError):
            coordinator.reserve_conversion(tmp_path / "next.md", None, None)
        assert coordinator.get_active_job() is first

        release_pool.set()
        canceller.join(3)
        assert not canceller.is_alive()
        assert cancellation_results == [True]
        if not worker_finished:
            # Releasing the cancellation owner alone cannot release a live job.
            with pytest.raises(ConversionCleanupPendingError):
                coordinator.reserve_conversion(tmp_path / "next.md", None, None)
        release_worker.set()
        assert first.wait(3)
        second = coordinator.reserve_conversion(tmp_path / "next.md", None, None)
        assert second.id != first.id
    finally:
        release_pool.set()
        release_worker.set()
        canceller.join(3)
        worker.join(3)
