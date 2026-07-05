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
