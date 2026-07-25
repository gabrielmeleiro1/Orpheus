"""Tests for worker startup probes and lifecycle errors."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

TESTS_DIR = Path(__file__).parent
sys.path.insert(0, str(TESTS_DIR))

from modules.pipeline import worker_runtime
from modules.pipeline.pool_manager import (
    SharedWorkerPoolManager,
    _worker_startup_probe,
)
from modules.pipeline.worker_errors import (
    WorkerStartupError,
    WorkerStartupStatus,
    classify_worker_startup_error,
)
from modules.tts.cleaner_errors import CleanerRuntimeMemoryError, CleanerRuntimeUnavailable
from modules.tts.runtime_spec import TTSRuntimeSpec
from worker_pool_test_helpers import (
    PendingStartupProbeExecutor,
    ProbeTrackingExecutor,
    TrackableFuture,
    make_failing_probe_executor,
    make_startup_status,
    reset_executor_class_state,
)


@pytest.fixture(autouse=True)
def restore_worker_runtime_state():
    original_state = {
        "_WORKER_TTS_ENGINE": worker_runtime._WORKER_TTS_ENGINE,
        "_WORKER_TEXT_CLEANER": worker_runtime._WORKER_TEXT_CLEANER,
        "_WORKER_PIPELINE_CONFIG": worker_runtime._WORKER_PIPELINE_CONFIG,
        "_WORKER_INIT_STATUS": worker_runtime._WORKER_INIT_STATUS,
        "_WORKER_PROVIDER": worker_runtime._WORKER_PROVIDER,
        "_WORKER_DEVICE": worker_runtime._WORKER_DEVICE,
    }
    try:
        worker_runtime._WORKER_TTS_ENGINE = None
        worker_runtime._WORKER_TEXT_CLEANER = None
        worker_runtime._WORKER_PIPELINE_CONFIG = None
        worker_runtime._WORKER_INIT_STATUS = None
        worker_runtime._WORKER_PROVIDER = "unknown"
        worker_runtime._WORKER_DEVICE = None
        yield
    finally:
        for name, value in original_state.items():
            setattr(worker_runtime, name, value)


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


def test_startup_probe_timeouts_share_one_deadline_and_are_infrastructure_failures() -> None:
    reset_executor_class_state(PendingStartupProbeExecutor)
    runtime = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        model="mlx-community/Kokoro-82M",
    )

    with patch(
        "modules.pipeline.pool_manager.ProcessPoolExecutor",
        PendingStartupProbeExecutor,
    ):
        manager = SharedWorkerPoolManager(
            max_workers=4,
            shutdown_timeout_seconds=0.0,
            startup_probe_timeout_seconds=0.05,
        )
        started_at = time.perf_counter()
        with pytest.raises(WorkerStartupError) as raised:
            manager.ensure_started(
                "startup-timeout",
                worker_init_config={"tts_runtime": runtime.to_dict()},
            )
        elapsed = time.perf_counter() - started_at

    executor = PendingStartupProbeExecutor.instances[0]
    assert elapsed < 0.14
    assert len(raised.value.statuses) == 4
    assert {status.error_type for status in raised.value.statuses} == {
        "WorkerInfrastructureError"
    }
    assert all(
        "timed out after 0.05s" in (status.message or "")
        for status in raised.value.statuses
    )
    assert all(status.provider == "mlx" for status in raised.value.statuses)
    assert all(
        future.cancelled_explicitly for future in executor.pending_probe_futures
    )


def test_startup_probe_mixed_results_preserve_submission_order() -> None:
    unavailable = WorkerStartupStatus(
        success=False,
        provider="mlx",
        device=None,
        error_type="WorkerRuntimeUnavailableError",
        message="runtime unavailable",
        phase="tts_load",
    )

    class MixedStartupProbeExecutor(PendingStartupProbeExecutor):
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN003
            super().__init__(*args, **kwargs)
            self.probe_calls = 0

        def submit(self, fn, *args, **kwargs):  # noqa: ANN003
            if fn is not _worker_startup_probe:
                return super().submit(fn, *args, **kwargs)
            self.probe_calls += 1
            future = TrackableFuture()
            if self.probe_calls == 1:
                self.pending_probe_futures.append(future)
            else:
                future.set_result(unavailable)
            return future

    reset_executor_class_state(MixedStartupProbeExecutor)
    with patch(
        "modules.pipeline.pool_manager.ProcessPoolExecutor",
        MixedStartupProbeExecutor,
    ):
        manager = SharedWorkerPoolManager(
            max_workers=2,
            shutdown_timeout_seconds=0.0,
            startup_probe_timeout_seconds=0.01,
        )
        with pytest.raises(WorkerStartupError) as raised:
            manager.ensure_started("mixed-startup-results")

    assert [status.error_type for status in raised.value.statuses] == [
        "WorkerInfrastructureError",
        "WorkerRuntimeUnavailableError",
    ]


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


def _cuda_worker_init_config() -> dict:
    return {
        "tts_runtime": TTSRuntimeSpec.from_legacy(
            "kokoro",
            "bf16",
            model="mlx-community/Kokoro-82M",
        ).to_dict(),
        "tts_engine": "kokoro",
        "tts_model_name": "mlx-community/Kokoro-82M",
        "tts_quantization": "bf16",
        "voice": "af_bella",
        "cleaner_model_name": "meta-llama/Llama-3.2-3B-Instruct",
        "cleaner_backend": "cuda",
        "cleaner_cuda_device": "cuda:0",
        "cleaner_cuda_quantization": "4bit",
        "cleaner_api_base_url": "https://api.openai.com/v1",
        "cleaner_api_key_env": "OPENAI_API_KEY",
        "cleaner_api_model": "gpt-5-nano",
        "cleaner_api_global_concurrency": 6,
        "cleaner_api_global_limiter": None,
    }


def test_classify_cleaner_runtime_errors_into_worker_taxonomy() -> None:
    memory_status = classify_worker_startup_error(
        CleanerRuntimeMemoryError("CUDA out of memory"),
        provider="cuda",
        device="cuda:0",
        phase="cleaner_load",
    )
    assert memory_status.error_type == "WorkerRuntimeMemoryError"

    unavailable_status = classify_worker_startup_error(
        CleanerRuntimeUnavailable("CUDA cleaner runtime unavailable"),
        provider="cuda",
        device="cuda:0",
        phase="cleaner_load",
    )
    assert unavailable_status.error_type == "WorkerRuntimeUnavailableError"


def test_worker_init_cuda_cleaner_unavailable_is_fatal_during_warmup() -> None:
    worker_runtime_module = importlib.import_module("modules.pipeline.worker_runtime")
    fake_engine = MagicMock()
    fake_cleaner = MagicMock()
    fake_cleaner.load.side_effect = CleanerRuntimeUnavailable(
        "CUDA cleaner runtime unavailable"
    )

    with patch(
        "modules.tts.factory.TTSEngineFactory.create",
        return_value=fake_engine,
    ), patch(
        "modules.tts.cleaner.TextCleaner",
        return_value=fake_cleaner,
    ):
        worker_runtime_module._init_worker(
            available_vram_gb=8.0,
            active_workers=1,
            worker_init_config=_cuda_worker_init_config(),
        )

    status = worker_runtime_module.get_worker_startup_status()
    assert status.success is False
    assert status.error_type == "WorkerRuntimeUnavailableError"
    assert status.phase == "cleaner_load"
    assert status.provider == "cuda"
    assert status.device == "cuda:0"
    assert "CUDA cleaner runtime unavailable" in (status.message or "")
    assert worker_runtime_module.get_worker_init_error() == status.message


def test_worker_init_cuda_cleaner_memory_error_includes_backend_and_device() -> None:
    worker_runtime_module = importlib.import_module("modules.pipeline.worker_runtime")
    fake_engine = MagicMock()
    fake_cleaner = MagicMock()
    fake_cleaner.load.side_effect = CleanerRuntimeMemoryError("CUDA out of memory")

    with patch(
        "modules.tts.factory.TTSEngineFactory.create",
        return_value=fake_engine,
    ), patch(
        "modules.tts.cleaner.TextCleaner",
        return_value=fake_cleaner,
    ):
        worker_runtime_module._init_worker(
            available_vram_gb=8.0,
            active_workers=1,
            worker_init_config=_cuda_worker_init_config(),
        )

    status = worker_runtime_module.get_worker_startup_status()
    assert status.success is False
    assert status.error_type == "WorkerRuntimeMemoryError"
    assert status.provider == "cuda"
    assert status.device == "cuda:0"
    assert "cuda/cuda:0" in status.format_failure()
    assert "CUDA out of memory" in (status.message or "")
