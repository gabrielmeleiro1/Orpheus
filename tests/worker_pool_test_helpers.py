"""Shared fakes and harness helpers for worker pool lifecycle tests."""

from __future__ import annotations

import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable

from modules.pipeline.pool_manager import _health_ping, _worker_startup_probe
from modules.pipeline.worker_errors import WorkerStartupStatus


class TrackableFuture(Future):
    """Future that records explicit cancel() calls from the pool manager."""

    def __init__(self) -> None:
        super().__init__()
        self.cancelled_explicitly = False

    def cancel(self) -> bool:
        self.cancelled_explicitly = True
        return super().cancel()


class FakeExecutor:
    created = 0

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN003
        _ = args, kwargs
        type(self).created += 1

    def submit(self, fn, *args, **kwargs):  # noqa: ANN003
        future: Future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:  # pragma: no cover - defensive
            future.set_exception(exc)
        return future

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:  # noqa: ARG002
        return None


class NeverCompletingChapterExecutor(FakeExecutor):
    instances: list[NeverCompletingChapterExecutor] = []

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN003
        super().__init__(*args, **kwargs)
        self.chapter_submit_calls = 0
        self.pending_futures: list[TrackableFuture] = []
        type(self).instances.append(self)

    def submit(self, fn, *args, **kwargs):  # noqa: ANN003
        if fn in (_worker_startup_probe, _health_ping):
            return super().submit(fn, *args, **kwargs)

        self.chapter_submit_calls += 1
        future = TrackableFuture()
        self.pending_futures.append(future)
        return future


class ProbeTrackingExecutor(FakeExecutor):
    created = 0
    instances: list[ProbeTrackingExecutor] = []

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN003
        super().__init__(*args, **kwargs)
        self.submitted_functions: list[object] = []
        type(self).instances.append(self)

    def submit(self, fn, *args, **kwargs):  # noqa: ANN003
        self.submitted_functions.append(fn)
        return super().submit(fn, *args, **kwargs)


def make_failing_probe_executor(
    failure_status: WorkerStartupStatus,
) -> type[ProbeTrackingExecutor]:
    """Build a ProcessPoolExecutor stand-in that fails startup probes."""

    class FailingProbeExecutor(ProbeTrackingExecutor):
        shutdown_calls = 0

        def submit(self, fn, *args, **kwargs):  # noqa: ANN003
            self.submitted_functions.append(fn)
            if fn is _worker_startup_probe:
                future: Future = Future()
                future.set_result(failure_status)
                return future
            return super().submit(fn, *args, **kwargs)

        def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:  # noqa: ARG002
            type(self).shutdown_calls += 1
            return None

    return FailingProbeExecutor


def reset_executor_class_state(executor_cls: type) -> None:
    """Reset class-level counters/lists on fake executor classes."""
    for attr, empty in (("instances", []), ("created", 0), ("shutdown_calls", 0)):
        if hasattr(executor_cls, attr):
            setattr(executor_cls, attr, empty)


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout_seconds: float = 2.0,
    poll_interval: float = 0.01,
    failure_message: str = "condition not met before timeout",
) -> None:
    deadline = time.perf_counter() + timeout_seconds
    while time.perf_counter() < deadline:
        if predicate():
            return
        time.sleep(poll_interval)
    raise AssertionError(failure_message)


def wait_for_executor_chapter_submits(
    executor_cls: type,
    *,
    min_submits: int = 1,
    timeout_seconds: float = 2.0,
) -> NeverCompletingChapterExecutor:
    executor: NeverCompletingChapterExecutor | None = None

    def _ready() -> bool:
        nonlocal executor
        if not getattr(executor_cls, "instances", None):
            return False
        executor = executor_cls.instances[0]
        return executor.chapter_submit_calls >= min_submits

    wait_until(
        _ready,
        timeout_seconds=timeout_seconds,
        failure_message=(
            f"executor did not submit at least {min_submits} chapter task(s)"
        ),
    )
    assert executor is not None
    return executor


def make_startup_status(
    *,
    error_type: str,
    message: str,
    provider: str = "mlx",
    device: str | None = None,
    phase: str = "tts_load",
) -> WorkerStartupStatus:
    return WorkerStartupStatus(
        success=False,
        provider=provider,
        device=device,
        error_type=error_type,
        message=message,
        phase=phase,
    )


@dataclass
class ProgressCapture:
    payloads: list[dict[str, Any]] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    def callback(
        self,
        stage,  # noqa: ANN001
        chapter_idx: int,
        total_chapters: int,
        chunk_idx: int,
        total_chunks: int,
        message: str,
        eta: float | None,
        performance: dict[str, Any] | None = None,
    ) -> None:
        _ = stage, chapter_idx, total_chapters, chunk_idx, total_chunks, eta
        self.messages.append(message)
        if performance:
            self.payloads.append(performance)

    def assert_fail_fast_startup_observability(
        self,
        *,
        provider: str,
        device: str | None,
        error_type: str,
    ) -> None:
        matching_payloads = [
            payload
            for payload in self.payloads
            if payload.get("startup_failure") is True
            and payload.get("startup_provider") == provider
            and payload.get("startup_device") == device
            and payload.get("startup_error_type") == error_type
        ]
        assert matching_payloads, (
            "expected startup observability payload with "
            f"provider={provider}, device={device}, error_type={error_type}; "
            f"got {self.payloads}"
        )

        label = provider if not device else f"{provider}/{device}"
        matching_messages = [
            message
            for message in self.messages
            if label in message and error_type in message
        ]
        assert matching_messages, (
            f"expected progress message containing {label} and {error_type}; "
            f"got {self.messages}"
        )


class RecordMemoryPolicy:
    """Memory policy stub that records memory-pressure events."""

    def __init__(self) -> None:
        self.errors_recorded = 0

    def allowed_workers(self, requested_max: int) -> int:
        return requested_max

    def record_memory_error(self) -> None:
        self.errors_recorded += 1


def assert_pending_futures_cancelled(executor: NeverCompletingChapterExecutor) -> None:
    assert executor.pending_futures, "expected pending chapter futures to track"
    assert all(
        future.cancelled_explicitly or future.cancelled()
        for future in executor.pending_futures
    ), "expected all pending chapter futures to be cancelled"
