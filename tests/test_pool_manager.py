"""Tests for shared process-pool reuse manager."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

import pytest

TESTS_DIR = Path(__file__).parent
sys.path.insert(0, str(TESTS_DIR))

from modules.pipeline.models import ChapterResult
from modules.pipeline.pool_manager import (
    SharedWorkerPoolManager,
    _health_ping,
    _worker_startup_probe,
)
from modules.pipeline.worker_errors import WorkerStartupError
from worker_pool_test_helpers import (
    FakeExecutor as _FakeExecutor,
    NeverCompletingChapterExecutor,
    assert_pending_futures_cancelled,
    make_failing_probe_executor,
    make_startup_status,
    reset_executor_class_state,
    wait_for_executor_chapter_submits,
)


def _finish_released_worker(executor, future, fn, args, kwargs):
    executor.release_worker.wait(timeout=3)
    try:
        future.set_result(fn(*args, **kwargs))
    except Exception as exc:  # pragma: no cover - defensive
        future.set_exception(exc)
    finally:
        executor.worker_finished.set()


def _pool_job_thread(manager, chapters, results, **options):
    return threading.Thread(
        target=lambda: results.extend(
            manager.run_parallel(chapters=chapters, process_func=_process, **options)
        ),
        daemon=True,
    )


@dataclass
class _Chapter:
    number: int
    title: str
    content: str = "content"


def test_shared_pool_rejects_conflicting_memory_aliases() -> None:
    with pytest.raises(ValueError, match="Pass only one"):
        SharedWorkerPoolManager(
            max_workers=2,
            available_vram_gb=4.0,
            available_device_memory_gb=10.0,
        )

    manager = SharedWorkerPoolManager(max_workers=2)
    with pytest.raises(ValueError, match="Pass only one"):
        manager.ensure_started(
            "same",
            available_vram_gb=4.0,
            available_device_memory_gb=10.0,
        )


class _SelectiveSubmitFailureExecutor(_FakeExecutor):
    def __init__(self, *args, **kwargs):  # noqa: ANN003
        super().__init__(*args, **kwargs)
        self._chapter_submit_calls = 0

    def submit(self, fn, *args, **kwargs):  # noqa: ANN003
        if fn in (_worker_startup_probe, _health_ping):
            return super().submit(fn, *args, **kwargs)

        self._chapter_submit_calls += 1
        if self._chapter_submit_calls == 2:
            raise RuntimeError("submit boom")
        return super().submit(fn, *args, **kwargs)


class _SelectiveResultFailureExecutor(_FakeExecutor):
    def __init__(self, *args, **kwargs):  # noqa: ANN003
        super().__init__(*args, **kwargs)
        self._chapter_submit_calls = 0

    def submit(self, fn, *args, **kwargs):  # noqa: ANN003
        if fn in (_worker_startup_probe, _health_ping):
            return super().submit(fn, *args, **kwargs)

        self._chapter_submit_calls += 1
        future: Future = Future()
        if self._chapter_submit_calls == 2:
            future.set_exception(RuntimeError("result boom"))
            return future
        return super().submit(fn, *args, **kwargs)


class _BlockingShutdownExecutor(_FakeExecutor):
    shutdown_entered = threading.Event()
    release_shutdown = threading.Event()

    def shutdown(self, wait: bool = True, cancel_futures: bool = False):  # noqa: ARG002
        type(self).shutdown_entered.set()
        if wait:
            type(self).release_shutdown.wait(timeout=3)
        return None


class _FakeWorkerProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.join_calls = 0

    def is_alive(self) -> bool:
        return not self.terminated and not self.killed

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def join(self, timeout: float | None = None) -> None:
        _ = timeout
        self.join_calls += 1


class _ClearingShutdownExecutor(_FakeExecutor):
    instances: list["_ClearingShutdownExecutor"] = []

    def __init__(self, *args, **kwargs):  # noqa: ANN003
        super().__init__(*args, **kwargs)
        self.worker = _FakeWorkerProcess()
        self._processes = {1: self.worker}
        type(self).instances.append(self)

    def shutdown(self, wait: bool = True, cancel_futures: bool = False):  # noqa: ARG002
        self._processes = None
        return None


class _AsyncTrackingExecutor(_FakeExecutor):
    instances: list["_AsyncTrackingExecutor"] = []

    def __init__(self, *args, **kwargs):  # noqa: ANN003
        super().__init__(*args, **kwargs)
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.submit_calls = 0
        self.chapter_submit_calls = 0
        type(self).instances.append(self)

    def submit(self, fn, *args, **kwargs):  # noqa: ANN003
        if fn in (_worker_startup_probe, _health_ping):
            return super().submit(fn, *args, **kwargs)

        future: Future = Future()
        with self._lock:
            self.submit_calls += 1
            self.chapter_submit_calls += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)

        def _run() -> None:
            self.release.wait(timeout=3)
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:  # pragma: no cover - defensive
                with self._lock:
                    self.in_flight -= 1
                future.set_exception(exc)
                return

            with self._lock:
                self.in_flight -= 1
            future.set_result(result)

        threading.Thread(target=_run, daemon=True).start()
        return future


class _QuiescingCancellationExecutor(_FakeExecutor):
    """Executor double that exposes whether cancellation waits for a worker."""

    instances: list["_QuiescingCancellationExecutor"] = []

    def __init__(self, *args, **kwargs):  # noqa: ANN003
        super().__init__(*args, **kwargs)
        self.shutdown_entered = threading.Event()
        self.release_worker = threading.Event()
        self.worker_started = threading.Event()
        self.worker_finished = threading.Event()
        type(self).instances.append(self)

    def submit(self, fn, *args, **kwargs):  # noqa: ANN003
        if fn in (_worker_startup_probe, _health_ping):
            return super().submit(fn, *args, **kwargs)

        future: Future = Future()

        def _run() -> None:
            self.worker_started.set()
            _finish_released_worker(self, future, fn, args, kwargs)

        threading.Thread(target=_run, daemon=True).start()
        return future

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:  # noqa: ARG002
        self.shutdown_entered.set()
        if wait:
            self.worker_finished.wait(timeout=3)


class _UncooperativeThreadExecutor(_FakeExecutor):
    """Thread executor double whose first chapter ignores cancellation."""

    instances: list["_UncooperativeThreadExecutor"] = []

    def __init__(self, *args, **kwargs):  # noqa: ANN003
        super().__init__(*args, **kwargs)
        self.is_first = not type(self).instances
        self.release_worker = threading.Event()
        self.worker_finished = threading.Event()
        self.shutdown_entered = threading.Event()
        type(self).instances.append(self)

    def submit(self, fn, *args, **kwargs):  # noqa: ANN003
        if fn in (_worker_startup_probe, _health_ping):
            return super().submit(fn, *args, **kwargs)

        future: Future = Future()
        if not self.is_first:
            try:
                future.set_result(fn(*args, **kwargs))
            except Exception as exc:  # pragma: no cover - defensive
                future.set_exception(exc)
            return future

        def _run() -> None:
            _finish_released_worker(self, future, fn, args, kwargs)

        threading.Thread(target=_run, daemon=True).start()
        return future

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:  # noqa: ARG002
        self.shutdown_entered.set()
        if wait and self.is_first:
            self.worker_finished.wait(timeout=3)


class _InspectableAsyncExecutor(_AsyncTrackingExecutor):
    instances: list["_InspectableAsyncExecutor"] = []


def _process(chapter, chapter_idx, _file_manager):  # noqa: ANN001
    return {
        "chapter_number": chapter.number,
        "chapter_title": chapter.title,
        "duration_ms": chapter_idx + 1,
    }


def _process_with_real_process_probe(
    chapter,
    chapter_idx,
    _file_manager,
):  # noqa: ANN001
    start_time = time.time()
    time.sleep(0.75)
    end_time = time.time()
    return ChapterResult(
        chapter_number=chapter.number,
        chapter_title=chapter.title,
        duration_ms=chapter_idx + 1,
        stage_timings={
            "pid": float(os.getpid()),
            "start_time": start_time,
            "end_time": end_time,
        },
    )


def _intervals_overlap(first: ChapterResult, second: ChapterResult) -> bool:
    return (
        first.stage_timings["start_time"] < second.stage_timings["end_time"]
        and second.stage_timings["start_time"] < first.stage_timings["end_time"]
    )


def test_shared_pool_reuses_executor_for_same_fingerprint() -> None:
    _FakeExecutor.created = 0
    chapters = [_Chapter(number=1, title="One"), _Chapter(number=2, title="Two")]

    with patch("modules.pipeline.pool_manager.ProcessPoolExecutor", _FakeExecutor):
        manager = SharedWorkerPoolManager(max_workers=2)
        first = manager.run_parallel(
            chapters=chapters,
            process_func=_process,
            config_fingerprint="same",
            worker_init_config={"voice": "af_bella"},
        )
        second = manager.run_parallel(
            chapters=chapters,
            process_func=_process,
            config_fingerprint="same",
            worker_init_config={"voice": "af_bella"},
        )
        snapshot = manager.snapshot()
        manager.shutdown()

    assert len(first) == 2
    assert len(second) == 2
    assert _FakeExecutor.created == 1
    assert snapshot["reuse_hits"] >= 1


def test_shared_pool_restarts_when_fingerprint_changes() -> None:
    _FakeExecutor.created = 0
    chapters = [_Chapter(number=1, title="One")]

    with patch("modules.pipeline.pool_manager.ProcessPoolExecutor", _FakeExecutor):
        manager = SharedWorkerPoolManager(max_workers=2)
        manager.run_parallel(
            chapters=chapters,
            process_func=_process,
            config_fingerprint="fp-1",
            worker_init_config={"voice": "af_bella"},
        )
        manager.run_parallel(
            chapters=chapters,
            process_func=_process,
            config_fingerprint="fp-2",
            worker_init_config={"voice": "af_bella"},
        )
        snapshot = manager.snapshot()
        manager.shutdown()

    assert _FakeExecutor.created == 2
    assert snapshot["restarts"] >= 1


def test_shared_pool_restarts_when_device_memory_budget_changes() -> None:
    _FakeExecutor.created = 0
    chapters = [_Chapter(number=1, title="One")]

    with patch("modules.pipeline.pool_manager.ProcessPoolExecutor", _FakeExecutor):
        manager = SharedWorkerPoolManager(max_workers=1)
        manager.run_parallel(
            chapters=chapters,
            process_func=_process,
            config_fingerprint="same",
            available_device_memory_gb=12.0,
            worker_init_config={"voice": "af_bella"},
        )
        manager.run_parallel(
            chapters=chapters,
            process_func=_process,
            config_fingerprint="same",
            available_device_memory_gb=6.0,
            worker_init_config={"voice": "af_bella"},
        )
        snapshot = manager.snapshot()
        manager.shutdown()

    assert _FakeExecutor.created == 2
    assert snapshot["reuse_hits"] == 0
    assert snapshot["restarts"] >= 1


def test_shared_pool_normalizes_submit_time_failure() -> None:
    chapters = [
        _Chapter(number=1, title="One"),
        _Chapter(number=2, title="Two"),
        _Chapter(number=3, title="Three"),
    ]

    with patch("modules.pipeline.pool_manager.ProcessPoolExecutor", _SelectiveSubmitFailureExecutor):
        manager = SharedWorkerPoolManager(max_workers=2)
        results = manager.run_parallel(
            chapters=chapters,
            process_func=_process,
            config_fingerprint="submit-fail",
            worker_init_config={"voice": "af_bella"},
        )
        manager.shutdown()

    assert len(results) == 3
    assert results[0].duration_ms == 1
    assert results[1] == ChapterResult(
        chapter_number=2,
        chapter_title="Two",
        error="submit boom",
    )
    assert results[2].duration_ms == 3


def test_shared_pool_normalizes_result_time_failure() -> None:
    chapters = [
        _Chapter(number=1, title="One"),
        _Chapter(number=2, title="Two"),
        _Chapter(number=3, title="Three"),
    ]

    with patch("modules.pipeline.pool_manager.ProcessPoolExecutor", _SelectiveResultFailureExecutor):
        manager = SharedWorkerPoolManager(max_workers=2)
        results = manager.run_parallel(
            chapters=chapters,
            process_func=_process,
            config_fingerprint="result-fail",
            worker_init_config={"voice": "af_bella"},
        )
        manager.shutdown()

    assert len(results) == 3
    assert results[0].duration_ms == 1
    assert results[1] == ChapterResult(
        chapter_number=2,
        chapter_title="Two",
        error="result boom",
    )
    assert results[2].duration_ms == 3


def test_shared_pool_cancellation_waits_for_running_worker_quiescence() -> None:
    reset_executor_class_state(_QuiescingCancellationExecutor)
    chapters = [_Chapter(number=1, title="One")]
    cancellation_requested = threading.Event()
    results: list[ChapterResult] = []

    with patch(
        "modules.pipeline.pool_manager.ProcessPoolExecutor",
        _QuiescingCancellationExecutor,
    ):
        manager = SharedWorkerPoolManager(max_workers=1)
        runner = threading.Thread(
            target=lambda: results.extend(
                manager.run_parallel(
                    chapters=chapters,
                    process_func=_process,
                    config_fingerprint="cancel-quiescence",
                    cancel_requested=cancellation_requested.is_set,
                )
            ),
            daemon=True,
        )
        runner.start()

        try:
            deadline = time.monotonic() + 2.0
            while not _QuiescingCancellationExecutor.instances:
                if time.monotonic() >= deadline:
                    raise AssertionError("executor was not created")
                time.sleep(0.01)
            executor = _QuiescingCancellationExecutor.instances[0]

            deadline = time.monotonic() + 2.0
            while not executor.worker_started.is_set():
                if time.monotonic() >= deadline:
                    raise AssertionError("chapter worker was not submitted")
                time.sleep(0.01)

            cancellation_requested.set()
            assert executor.shutdown_entered.wait(timeout=1.0)
            assert not executor.worker_finished.is_set()

            executor.release_worker.set()
            runner.join(timeout=2.0)
            assert not runner.is_alive()
            assert executor.worker_finished.is_set()
            assert results == []
        finally:
            executor = _QuiescingCancellationExecutor.instances[0]
            executor.release_worker.set()
            runner.join(timeout=2.0)
            manager.shutdown(timeout_seconds=0.0)


def test_shared_pool_stopping_barrier_blocks_successor_until_thread_worker_quiesces() -> None:
    reset_executor_class_state(_UncooperativeThreadExecutor)
    chapters = [_Chapter(number=1, title="One")]
    cancellation_requested = threading.Event()
    first_results: list[ChapterResult] = []
    second_results: list[ChapterResult] = []

    with patch(
        "modules.pipeline.pool_manager.ThreadPoolExecutor",
        _UncooperativeThreadExecutor,
    ):
        manager = SharedWorkerPoolManager(max_workers=1, shutdown_timeout_seconds=0.05)
        manager._use_processes = False  # noqa: SLF001
        first_runner = _pool_job_thread(
            manager, chapters, first_results,
            config_fingerprint='thread-cancel',
            cancel_requested=cancellation_requested.is_set,
        )
        first_runner.start()

        try:
            deadline = time.monotonic() + 2.0
            while not _UncooperativeThreadExecutor.instances:
                if time.monotonic() >= deadline:
                    raise AssertionError("thread executor was not created")
                time.sleep(0.01)
            first_executor = _UncooperativeThreadExecutor.instances[0]
            cancellation_requested.set()
            first_runner.join(timeout=1.0)
            assert not first_runner.is_alive()
            assert first_results == []
            assert first_executor.worker_finished.is_set() is False

            second_runner = _pool_job_thread(
                manager, chapters, second_results,
                config_fingerprint='thread-cancel',
            )
            second_runner.start()
            second_runner.join(timeout=0.2)
            assert second_runner.is_alive()
            assert len(_UncooperativeThreadExecutor.instances) == 1

            first_executor.release_worker.set()
            second_runner.join(timeout=2.0)
            assert not second_runner.is_alive()
            assert second_results
            assert len(_UncooperativeThreadExecutor.instances) == 2
        finally:
            if _UncooperativeThreadExecutor.instances:
                _UncooperativeThreadExecutor.instances[0].release_worker.set()
            first_runner.join(timeout=2.0)
            manager.shutdown(timeout_seconds=0.0)


def test_shared_pool_shutdown_barrier_blocks_successor_after_active_run_cancels() -> None:
    reset_executor_class_state(_UncooperativeThreadExecutor)
    chapters = [_Chapter(number=1, title="One")]
    cancellation_requested = threading.Event()
    first_results: list[ChapterResult] = []
    second_results: list[ChapterResult] = []
    shutdown_results: list[bool] = []

    with patch(
        "modules.pipeline.pool_manager.ThreadPoolExecutor",
        _UncooperativeThreadExecutor,
    ):
        manager = SharedWorkerPoolManager(max_workers=1, shutdown_timeout_seconds=0.05)
        manager._use_processes = False  # noqa: SLF001
        first_runner = _pool_job_thread(
            manager, chapters, first_results,
            config_fingerprint='shutdown-race',
            cancel_requested=cancellation_requested.is_set,
        )
        first_runner.start()

        try:
            deadline = time.monotonic() + 2.0
            while not _UncooperativeThreadExecutor.instances:
                if time.monotonic() >= deadline:
                    raise AssertionError("thread executor was not created")
                time.sleep(0.01)
            first_executor = _UncooperativeThreadExecutor.instances[0]

            shutdown_runner = threading.Thread(
                target=lambda: shutdown_results.append(
                    manager.shutdown(timeout_seconds=0.05)
                ),
                daemon=True,
            )
            shutdown_runner.start()
            assert first_executor.shutdown_entered.wait(timeout=1.0)
            shutdown_runner.join(timeout=1.0)
            assert not shutdown_runner.is_alive()
            assert shutdown_results == [False]

            cancellation_requested.set()
            first_runner.join(timeout=1.0)
            assert not first_runner.is_alive()
            assert first_results == []
            assert first_executor.worker_finished.is_set() is False

            second_runner = _pool_job_thread(
                manager, chapters, second_results,
                config_fingerprint='successor',
            )
            second_runner.start()
            second_runner.join(timeout=0.2)
            assert second_runner.is_alive()
            assert len(_UncooperativeThreadExecutor.instances) == 1

            first_executor.release_worker.set()
            second_runner.join(timeout=2.0)
            assert not second_runner.is_alive()
            assert second_results
            assert len(_UncooperativeThreadExecutor.instances) == 2
        finally:
            if _UncooperativeThreadExecutor.instances:
                _UncooperativeThreadExecutor.instances[0].release_worker.set()
            first_runner.join(timeout=2.0)
            manager.shutdown(timeout_seconds=0.0)


def test_shared_pool_cancellation_clears_current_job_metrics() -> None:
    reset_executor_class_state(_QuiescingCancellationExecutor)
    chapters = [_Chapter(number=1, title="One")]
    cancellation_requested = threading.Event()

    with patch(
        "modules.pipeline.pool_manager.ProcessPoolExecutor",
        _QuiescingCancellationExecutor,
    ):
        manager = SharedWorkerPoolManager(max_workers=1)
        runner = threading.Thread(
            target=lambda: manager.run_parallel(
                chapters=chapters,
                process_func=_process,
                config_fingerprint="cancel-metrics",
                cancel_requested=cancellation_requested.is_set,
            ),
            daemon=True,
        )
        runner.start()
        try:
            deadline = time.monotonic() + 2.0
            while not _QuiescingCancellationExecutor.instances:
                if time.monotonic() >= deadline:
                    raise AssertionError("executor was not created")
                time.sleep(0.01)
            executor = _QuiescingCancellationExecutor.instances[0]
            cancellation_requested.set()
            # Generous bounds: thread start-up is slow on a loaded Windows machine.
            assert executor.worker_started.wait(timeout=10.0)
            assert executor.shutdown_entered.wait(timeout=10.0)
            executor.release_worker.set()
            runner.join(timeout=2.0)
            assert not runner.is_alive()
            snapshot = manager.snapshot()
            assert snapshot["running_chapters"] == 0
            assert snapshot["queued_chapters"] == 0
            assert snapshot["completed_chapters"] == 0
            assert snapshot["failed_chapters"] == 0
        finally:
            executor = _QuiescingCancellationExecutor.instances[0]
            executor.release_worker.set()
            runner.join(timeout=2.0)
            manager.shutdown(timeout_seconds=0.0)


def test_shared_pool_progress_callback_includes_performance_snapshot() -> None:
    chapters = [
        _Chapter(number=1, title="One"),
        _Chapter(number=2, title="Two"),
        _Chapter(number=3, title="Three"),
    ]
    progress_snapshots: list[dict] = []

    def _progress(_current: int, _total: int, _message: str, performance: dict) -> None:
        progress_snapshots.append(performance)

    with patch("modules.pipeline.pool_manager.ProcessPoolExecutor", _FakeExecutor):
        manager = SharedWorkerPoolManager(max_workers=2)
        results = manager.run_parallel(
            chapters=chapters,
            process_func=_process,
            progress_callback=_progress,
            config_fingerprint="progress-snapshot",
            worker_init_config={"voice": "af_bella"},
        )
        manager.shutdown()

    assert len(results) == 3
    assert progress_snapshots
    assert any(snapshot["max_workers"] == 2 for snapshot in progress_snapshots)
    assert any(snapshot["active_workers"] <= 2 for snapshot in progress_snapshots)
    assert any(snapshot["active_chapter_titles"] for snapshot in progress_snapshots)
    assert progress_snapshots[-1]["completed_chapters"] == 3
    assert progress_snapshots[-1]["failed_chapters"] == 0


def test_shared_pool_bounds_in_flight_futures_to_worker_count() -> None:
    _AsyncTrackingExecutor.instances = []
    chapters = [_Chapter(number=idx + 1, title=f"Chapter {idx + 1}") for idx in range(8)]
    results: list[dict] = []

    def _run_manager() -> None:
        with patch("modules.pipeline.pool_manager.ProcessPoolExecutor", _AsyncTrackingExecutor):
            manager = SharedWorkerPoolManager(max_workers=2)
            try:
                results.extend(
                    manager.run_parallel(
                        chapters=chapters,
                        process_func=_process,
                        config_fingerprint="bounded-submit",
                        worker_init_config={"voice": "af_bella"},
                    )
                )
            finally:
                manager.shutdown()

    thread = threading.Thread(target=_run_manager)
    thread.start()

    deadline = time.perf_counter() + 2
    while time.perf_counter() < deadline:
        if _AsyncTrackingExecutor.instances:
            executor = _AsyncTrackingExecutor.instances[0]
            if executor.chapter_submit_calls == 2:
                break
        time.sleep(0.01)
    else:
        raise AssertionError("manager did not submit the initial bounded work")

    executor = _AsyncTrackingExecutor.instances[0]
    assert executor.chapter_submit_calls == 2
    assert executor.max_in_flight <= 2

    executor.release.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert len(results) == len(chapters)
    assert executor.chapter_submit_calls == len(chapters)
    assert executor.max_in_flight <= 2


def test_shared_pool_real_process_pool_runs_worker_tasks_concurrently() -> None:
    chapters = [_Chapter(number=idx + 1, title=f"Chapter {idx + 1}") for idx in range(2)]
    manager = SharedWorkerPoolManager(max_workers=2)

    try:
        try:
            results = manager.run_parallel(
                chapters=chapters,
                process_func=_process_with_real_process_probe,
                config_fingerprint="real-process-concurrency",
                worker_init_config={},
            )
        except PermissionError as exc:
            pytest.skip(f"real process pool unavailable: {exc}")
    finally:
        manager.shutdown(timeout_seconds=0.5, cancel_futures=True)

    assert len(results) == 2
    assert [result.chapter_number for result in results] == [1, 2]
    assert len({result.stage_timings["pid"] for result in results}) >= 2
    assert _intervals_overlap(results[0], results[1])


def test_shared_pool_memory_backpressure_waits_before_submitting_more() -> None:
    _InspectableAsyncExecutor.instances = []
    chapters = [_Chapter(number=idx + 1, title=f"Chapter {idx + 1}") for idx in range(3)]
    results: list[dict] = []

    class BackpressurePolicy:
        def should_submit_more(self, active_workers: int) -> bool:
            return active_workers == 0

    def _run_manager() -> None:
        with patch("modules.pipeline.pool_manager.ProcessPoolExecutor", _InspectableAsyncExecutor):
            manager = SharedWorkerPoolManager(max_workers=2)
            try:
                results.extend(
                    manager.run_parallel(
                        chapters=chapters,
                        process_func=_process,
                        config_fingerprint="memory-backpressure",
                        worker_init_config={"voice": "af_bella"},
                        memory_policy=BackpressurePolicy(),
                    )
                )
            finally:
                manager.shutdown()

    thread = threading.Thread(target=_run_manager)
    thread.start()

    deadline = time.perf_counter() + 2
    while time.perf_counter() < deadline:
        if _InspectableAsyncExecutor.instances:
            executor = _InspectableAsyncExecutor.instances[0]
            if executor.chapter_submit_calls == 1:
                break
        time.sleep(0.01)
    else:
        raise AssertionError("manager did not submit the first chapter")

    executor = _InspectableAsyncExecutor.instances[0]
    time.sleep(0.05)
    assert executor.chapter_submit_calls == 1
    assert executor.max_in_flight == 1

    executor.release.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert len(results) == len(chapters)
    assert executor.chapter_submit_calls == len(chapters)
    assert executor.max_in_flight == 1


def test_shared_pool_propagates_executor_startup_failure() -> None:
    chapters = [_Chapter(number=1, title="One")]

    with patch(
        "modules.pipeline.pool_manager.ProcessPoolExecutor",
        side_effect=RuntimeError("executor startup failed"),
    ):
        manager = SharedWorkerPoolManager(max_workers=2)
        with pytest.raises(RuntimeError, match="executor startup failed"):
            manager.run_parallel(
                chapters=chapters,
                process_func=_process,
                config_fingerprint="startup-fail",
                worker_init_config={"voice": "af_bella"},
            )


def test_shared_pool_shutdown_wait_is_bounded() -> None:
    _BlockingShutdownExecutor.shutdown_entered.clear()
    _BlockingShutdownExecutor.release_shutdown.clear()

    with patch("modules.pipeline.pool_manager.ProcessPoolExecutor", _BlockingShutdownExecutor):
        manager = SharedWorkerPoolManager(max_workers=2)
        manager.ensure_started(
            "blocking-shutdown",
            worker_init_config={"voice": "af_bella"},
        )
        shutdown_timeout = 0.05
        started_at = time.perf_counter()
        try:
            finished = manager.shutdown(timeout_seconds=shutdown_timeout)
        finally:
            elapsed = time.perf_counter() - started_at
            _BlockingShutdownExecutor.release_shutdown.set()

    assert finished is False
    # Bounded shutdown waits for the primary timeout plus the secondary join in
    # pool_manager._shutdown_executor_bounded; allow CI scheduling slack.
    assert elapsed < shutdown_timeout + 0.25 + 0.35
    assert _BlockingShutdownExecutor.shutdown_entered.is_set()
    assert manager.snapshot()["running"] is False


def test_shared_pool_shutdown_preserves_quiescence_barrier_after_timeout():
    _BlockingShutdownExecutor.shutdown_entered.clear()
    _BlockingShutdownExecutor.release_shutdown.clear()

    with patch("modules.pipeline.pool_manager.ProcessPoolExecutor", _BlockingShutdownExecutor):
        manager = SharedWorkerPoolManager(max_workers=2)
        manager.ensure_started("blocking-shutdown", worker_init_config={"voice": "af_bella"})
        try:
            assert manager.shutdown(timeout_seconds=0.01) is False
            assert manager.shutdown(timeout_seconds=0.0) is False
        finally:
            _BlockingShutdownExecutor.release_shutdown.set()

        assert manager.shutdown(timeout_seconds=1.0) is True


def test_shared_pool_replacement_preserves_quiescence_barrier_after_timeout():
    _BlockingShutdownExecutor.shutdown_entered.clear()
    _BlockingShutdownExecutor.release_shutdown.clear()

    with patch("modules.pipeline.pool_manager.ProcessPoolExecutor", _BlockingShutdownExecutor):
        manager = SharedWorkerPoolManager(max_workers=2, shutdown_timeout_seconds=0.01)
        manager.ensure_started("old-runtime", worker_init_config={"voice": "af_bella"})
        try:
            with pytest.raises(RuntimeError, match="has not stopped"):
                manager.ensure_started("new-runtime", worker_init_config={"voice": "af_bella"})
            assert manager.cleanup_pending is True
            assert manager.snapshot()["running"] is False
        finally:
            _BlockingShutdownExecutor.release_shutdown.set()

        assert manager.shutdown(timeout_seconds=1.0) is True
        manager.ensure_started("new-runtime", worker_init_config={"voice": "af_bella"})
        assert manager.snapshot()["fingerprint"] == "new-runtime"
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_bounded_shutdown_does_not_report_success_when_shutdown_raises():
    from modules.pipeline.pool_shutdown import _shutdown_executor_bounded

    class FailingExecutor:
        def shutdown(self, **kwargs):
            raise RuntimeError("shutdown failed")

    assert _shutdown_executor_bounded(
        FailingExecutor(), timeout_seconds=1.0, cancel_futures=True,
    ) is False


def test_shared_pool_cancellation_interrupts_blocking_wait_loop() -> None:
    reset_executor_class_state(NeverCompletingChapterExecutor)
    chapters = [
        _Chapter(number=1, title="One"),
        _Chapter(number=2, title="Two"),
        _Chapter(number=3, title="Three"),
    ]
    cancel_flag = threading.Event()
    progress_messages: list[str] = []
    results: list[ChapterResult] = []

    def _cancel_requested() -> bool:
        return cancel_flag.is_set()

    def _progress(_current: int, _total: int, message: str, *_args) -> None:
        progress_messages.append(message)

    def _run_manager() -> None:
        with patch(
            "modules.pipeline.pool_manager.ProcessPoolExecutor",
            NeverCompletingChapterExecutor,
        ):
            manager = SharedWorkerPoolManager(max_workers=2)
            try:
                results.extend(
                    manager.run_parallel(
                        chapters=chapters,
                        process_func=_process,
                        progress_callback=_progress,
                        config_fingerprint="cancel-wait",
                        worker_init_config={"voice": "af_bella"},
                        cancel_requested=_cancel_requested,
                    )
                )
            finally:
                manager.shutdown()

    thread = threading.Thread(target=_run_manager)
    thread.start()

    executor = wait_for_executor_chapter_submits(NeverCompletingChapterExecutor)

    cancel_flag.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert results == []
    assert any("Parallel processing cancelled" in message for message in progress_messages)
    assert_pending_futures_cancelled(executor)


def test_shared_pool_zero_timeout_terminates_workers_even_if_shutdown_clears_processes() -> None:
    _ClearingShutdownExecutor.instances = []

    with patch("modules.pipeline.pool_manager.ProcessPoolExecutor", _ClearingShutdownExecutor):
        manager = SharedWorkerPoolManager(max_workers=2)
        manager.ensure_started(
            "clearing-shutdown",
            worker_init_config={"voice": "af_bella"},
        )
        worker = _ClearingShutdownExecutor.instances[0].worker

        finished = manager.shutdown(timeout_seconds=0.0, cancel_futures=True)

    assert finished is False
    assert worker.terminated is True
    assert worker.join_calls >= 1
    assert manager.snapshot()["running"] is False


def test_shared_pool_snapshot_records_startup_failure() -> None:
    chapters = [_Chapter(number=1, title="One")]
    failure_status = make_startup_status(
        error_type="WorkerRuntimeUnavailableError",
        message="cuda driver unavailable",
        provider="cuda",
        device="cuda:0",
    )
    failing_executor = make_failing_probe_executor(failure_status)

    with patch(
        "modules.pipeline.pool_manager.ProcessPoolExecutor",
        failing_executor,
    ):
        manager = SharedWorkerPoolManager(max_workers=2)
        with pytest.raises(WorkerStartupError):
            manager.run_parallel(
                chapters=chapters,
                process_func=_process,
                config_fingerprint="startup-observability",
                worker_init_config={"voice": "af_bella"},
            )
        snapshot = manager.snapshot()

    assert snapshot["startup_failures"] == 1
    assert snapshot["last_startup_error"] == (
        "cuda/cuda:0 [WorkerRuntimeUnavailableError]: cuda driver unavailable"
    )
    assert snapshot["running"] is False
    assert snapshot["cancelled_jobs"] == 0


def test_shared_pool_snapshot_records_cancellation() -> None:
    reset_executor_class_state(NeverCompletingChapterExecutor)
    chapters = [_Chapter(number=1, title="One")]
    cancel_flag = threading.Event()

    def _cancel_requested() -> bool:
        return cancel_flag.is_set()

    def _run_manager() -> None:
        with patch(
            "modules.pipeline.pool_manager.ProcessPoolExecutor",
            NeverCompletingChapterExecutor,
        ):
            manager = SharedWorkerPoolManager(max_workers=1)
            try:
                manager.run_parallel(
                    chapters=chapters,
                    process_func=_process,
                    config_fingerprint="cancel-observability",
                    worker_init_config={"voice": "af_bella"},
                    cancel_requested=_cancel_requested,
                )
            finally:
                snapshot_holder.append(manager.snapshot())
                manager.shutdown()

    snapshot_holder: list[dict] = []
    thread = threading.Thread(target=_run_manager)
    thread.start()

    executor = wait_for_executor_chapter_submits(NeverCompletingChapterExecutor)

    cancel_flag.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    snapshot = snapshot_holder[0]
    assert snapshot["cancelled_jobs"] == 1
    assert snapshot["startup_failures"] == 0
    assert_pending_futures_cancelled(executor)

