"""Tests for shared process-pool reuse manager."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
import os
import threading
import time
from unittest.mock import patch

import pytest

from modules.pipeline.models import ChapterResult
from modules.pipeline.pool_manager import SharedWorkerPoolManager


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


class _FakeExecutor:
    created = 0

    def __init__(self, *args, **kwargs):  # noqa: ANN003
        _ = args, kwargs
        _FakeExecutor.created += 1

    def submit(self, fn, *args, **kwargs):  # noqa: ANN003
        future: Future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:  # pragma: no cover - defensive
            future.set_exception(exc)
        return future

    def shutdown(self, wait: bool = True, _cancel_futures: bool = False):  # noqa: ARG002
        return None


class _SelectiveSubmitFailureExecutor(_FakeExecutor):
    def __init__(self, *args, **kwargs):  # noqa: ANN003
        super().__init__(*args, **kwargs)
        self._submit_calls = 0

    def submit(self, fn, *args, **kwargs):  # noqa: ANN003
        self._submit_calls += 1
        if self._submit_calls == 2:
            raise RuntimeError("submit boom")
        return super().submit(fn, *args, **kwargs)


class _SelectiveResultFailureExecutor(_FakeExecutor):
    def __init__(self, *args, **kwargs):  # noqa: ANN003
        super().__init__(*args, **kwargs)
        self._submit_calls = 0

    def submit(self, fn, *args, **kwargs):  # noqa: ANN003
        self._submit_calls += 1
        future: Future = Future()
        if self._submit_calls == 2:
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
        type(self).instances.append(self)

    def submit(self, fn, *args, **kwargs):  # noqa: ANN003
        future: Future = Future()
        with self._lock:
            self.submit_calls += 1
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


def test_shared_pool_always_emits_chapter_results_for_success_and_failures() -> None:
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
            config_fingerprint="chapter-result-boundary",
            worker_init_config={"voice": "af_bella"},
        )
        manager.shutdown()

    assert all(isinstance(result, ChapterResult) for result in results)
    assert [result.chapter_number for result in results] == [1, 2, 3]
    assert results[0].duration_ms == 1
    assert results[1].error == "result boom"
    assert results[2].duration_ms == 3


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
            if executor.submit_calls == 2:
                break
        time.sleep(0.01)
    else:
        raise AssertionError("manager did not submit the initial bounded work")

    executor = _AsyncTrackingExecutor.instances[0]
    assert executor.submit_calls == 2
    assert executor.max_in_flight <= 2

    executor.release.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert len(results) == len(chapters)
    assert executor.submit_calls == len(chapters)
    assert executor.max_in_flight <= 2


def test_shared_pool_real_process_pool_runs_worker_tasks_concurrently() -> None:
    chapters = [_Chapter(number=idx + 1, title=f"Chapter {idx + 1}") for idx in range(2)]
    manager = SharedWorkerPoolManager(max_workers=2)

    try:
        results = manager.run_parallel(
            chapters=chapters,
            process_func=_process_with_real_process_probe,
            config_fingerprint="real-process-concurrency",
            worker_init_config={},
        )
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
            if executor.submit_calls == 1:
                break
        time.sleep(0.01)
    else:
        raise AssertionError("manager did not submit the first chapter")

    executor = _InspectableAsyncExecutor.instances[0]
    time.sleep(0.05)
    assert executor.submit_calls == 1
    assert executor.max_in_flight == 1

    executor.release.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert len(results) == len(chapters)
    assert executor.submit_calls == len(chapters)
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
