"""Tests for worker startup probes and lifecycle errors."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from modules.pipeline import worker_runtime
from modules.pipeline.pool_manager import (
    SharedWorkerPoolManager,
    _worker_startup_probe,
)
from modules.pipeline.worker_errors import WorkerStartupError, WorkerStartupStatus
from tests.worker_pool_test_helpers import (
    ProbeTrackingExecutor,
    make_failing_probe_executor,
    make_startup_status,
    reset_executor_class_state,
)


def _fake_worker_initializer_with_error(
    available_vram_gb: float,
    active_workers: int = 1,
    worker_init_config: dict | None = None,
) -> None:
    _ = available_vram_gb, active_workers, worker_init_config
    worker_runtime._WORKER_INIT_STATUS = WorkerStartupStatus(
        success=False,
        provider="mlx",
        device=None,
        error_type="WorkerRuntimeLoadError",
        message="simulated model load failure",
        phase="tts_load",
    )
    worker_runtime._WORKER_PROVIDER = "mlx"


def test_fake_worker_initializer_sets_init_error_for_probe() -> None:
    original_status = worker_runtime._WORKER_INIT_STATUS
    original_provider = worker_runtime._WORKER_PROVIDER
    try:
        worker_runtime._WORKER_INIT_STATUS = None
        worker_runtime._WORKER_PROVIDER = "unknown"
        _fake_worker_initializer_with_error(8.0, 2, {"voice": "af_bella"})

        assert worker_runtime.get_worker_init_error() == "simulated model load failure"
        status = _worker_startup_probe()
        assert status.success is False
        assert status.message == "simulated model load failure"
        assert status.provider == "mlx"
    finally:
        worker_runtime._WORKER_INIT_STATUS = original_status
        worker_runtime._WORKER_PROVIDER = original_provider


def test_worker_startup_probe_reports_stored_init_error() -> None:
    failure = WorkerStartupStatus(
        success=False,
        provider="cuda",
        device="cuda:0",
        error_type="WorkerRuntimeUnavailableError",
        message="cuda driver unavailable",
        phase="tts_load",
    )
    with patch.object(worker_runtime, "_WORKER_INIT_STATUS", failure), patch.object(
        worker_runtime, "_WORKER_PROVIDER", "cuda"
    ), patch.object(worker_runtime, "_WORKER_DEVICE", "cuda:0"):
        status = _worker_startup_probe()

    assert status.success is False
    assert status.provider == "cuda"
    assert status.device == "cuda:0"
    assert status.error_type == "WorkerRuntimeUnavailableError"
    assert status.message == "cuda driver unavailable"


def test_worker_startup_probe_reports_success_without_init_error() -> None:
    with patch.object(worker_runtime, "_WORKER_INIT_STATUS", None), patch.object(
        worker_runtime, "_WORKER_PROVIDER", "mlx"
    ), patch.object(worker_runtime, "_WORKER_DEVICE", None):
        status = _worker_startup_probe()

    assert status.success is True
    assert status.provider == "mlx"
    assert status.error_type is None
    assert status.message is None


def test_ensure_started_submits_startup_probes_for_new_pool() -> None:
    reset_executor_class_state(ProbeTrackingExecutor)

    with patch("modules.pipeline.pool_manager.ProcessPoolExecutor", ProbeTrackingExecutor):
        manager = SharedWorkerPoolManager(max_workers=3)
        manager.ensure_started("probe-fingerprint", worker_init_config={"voice": "af_bella"})
        executor = ProbeTrackingExecutor.instances[0]
        manager.shutdown()

    assert ProbeTrackingExecutor.created == 1
    assert len(executor.submitted_functions) == 3
    assert all(fn is _worker_startup_probe for fn in executor.submitted_functions)


def test_ensure_started_submits_single_startup_probe_in_thread_mode() -> None:
    reset_executor_class_state(ProbeTrackingExecutor)

    with patch("modules.pipeline.pool_manager.ThreadPoolExecutor", ProbeTrackingExecutor):
        manager = SharedWorkerPoolManager(max_workers=3)
        manager._use_processes = False
        manager.ensure_started("thread-probe", worker_init_config={"voice": "af_bella"})
        executor = ProbeTrackingExecutor.instances[0]
        manager.shutdown()

    assert ProbeTrackingExecutor.created == 1
    assert len(executor.submitted_functions) == 1
    assert executor.submitted_functions[0] is _worker_startup_probe


def test_startup_probe_failure_shuts_down_executor_and_raises() -> None:
    failure_status = make_startup_status(
        error_type="WorkerRuntimeLoadError",
        message="probe failed in worker",
        provider="unknown",
        device=None,
        phase="tts_load",
    )
    failing_executor = make_failing_probe_executor(failure_status)
    reset_executor_class_state(failing_executor)

    with patch("modules.pipeline.pool_manager.ProcessPoolExecutor", failing_executor):
        manager = SharedWorkerPoolManager(max_workers=2)
        with pytest.raises(WorkerStartupError, match="probe failed in worker"):
            manager.ensure_started(
                "startup-failure",
                worker_init_config={"voice": "af_bella"},
            )

    assert failing_executor.shutdown_calls >= 1
    assert manager.snapshot()["running"] is False
    assert manager._startup_failures == 1


def test_pool_reuse_uses_health_ping_not_startup_probes() -> None:
    reset_executor_class_state(ProbeTrackingExecutor)

    with patch("modules.pipeline.pool_manager.ProcessPoolExecutor", ProbeTrackingExecutor):
        manager = SharedWorkerPoolManager(max_workers=2)
        manager.ensure_started("reuse-fingerprint", worker_init_config={"voice": "af_bella"})
        first_executor = ProbeTrackingExecutor.instances[0]
        first_probe_count = len(first_executor.submitted_functions)

        manager.ensure_started("reuse-fingerprint", worker_init_config={"voice": "af_bella"})
        reuse_submissions = first_executor.submitted_functions[first_probe_count:]

    manager.shutdown()

    assert ProbeTrackingExecutor.created == 1
    assert first_probe_count == 2
    assert len(reuse_submissions) == 1
    from modules.pipeline.pool_manager import _health_ping

    assert reuse_submissions[0] is _health_ping
    assert manager.snapshot()["reuse_hits"] >= 1
