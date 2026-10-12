"""Capabilities report the checks of the selected runtime, not MLX everywhere."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from modules.app.controller import AppController
from modules.app.services.runtime_capability_service import RuntimeCapabilityService

MLX_MISSING = {"available": False, "latency_ms": 650.0, "error": "No module named 'mlx'"}


def _service_with(results: dict[str, dict]) -> RuntimeCapabilityService:
    service = RuntimeCapabilityService()
    for kind, result in results.items():
        service._store_result(kind, result)
    # A refresh would replace the seeded results with real probes.
    service._ensure_background_refresh = lambda *_args: False
    return service


@pytest.mark.parametrize("runtime", ["cuda", "cpu"])
def test_non_mlx_runtime_does_not_report_the_mlx_import_failure(runtime) -> None:
    cuda_key = RuntimeCapabilityService()._cleaner_cache_key("cuda")
    service = _service_with({
        "tts": MLX_MISSING, "cleaner": MLX_MISSING,
        "tts-package": {"available": True, "latency_ms": 900.0, "error": None},
        cuda_key: {"available": False, "latency_ms": 700.0, "error": "No NVIDIA GPU with CUDA support was found."},
    })

    snapshot = service.get_system_runtime_snapshot(primary_runtime=runtime)

    assert snapshot["tts"] == {"available": True, "latency_ms": 900.0, "error": None}
    assert snapshot["cleaner"]["error"] == "No NVIDIA GPU with CUDA support was found."
    assert "mlx" not in str(snapshot).lower()


def test_available_cuda_runtime_reports_no_errors() -> None:
    cuda_key = RuntimeCapabilityService()._cleaner_cache_key("cuda")
    service = _service_with({
        "tts": MLX_MISSING, "cleaner": MLX_MISSING,
        "tts-package": {"available": True, "latency_ms": 900.0, "error": None},
        cuda_key: {"available": True, "latency_ms": 700.0, "error": None},
    })

    snapshot = service.get_system_runtime_snapshot(primary_runtime="cuda")

    assert snapshot["tts"]["error"] is None and snapshot["cleaner"]["error"] is None
    assert snapshot["tts"]["available"] and snapshot["cleaner"]["available"]


def test_mlx_runtime_keeps_reporting_the_mlx_checks() -> None:
    service = _service_with({"tts": MLX_MISSING, "cleaner": MLX_MISSING})

    snapshot = service.get_system_runtime_snapshot(primary_runtime="mlx")

    assert snapshot["tts"]["error"] == "No module named 'mlx'"
    assert snapshot["cleaner"]["error"] == "No module named 'mlx'"


@pytest.mark.parametrize("runtime", ["cuda", "cpu"])
def test_background_refresh_probes_the_selected_runtime(runtime, monkeypatch) -> None:
    service = RuntimeCapabilityService()
    calls: list[str] = []
    monkeypatch.setattr(service, "probe_mlx_tts_runtime", lambda: calls.append("mlx-tts"))
    monkeypatch.setattr(service, "probe_package_tts_runtime", lambda: calls.append("package-tts"))
    monkeypatch.setattr(service, "probe_cleaner_backend", lambda backend: calls.append(f"cleaner:{backend}"))

    service._refresh_runtime_snapshot(runtime)

    assert "mlx-tts" not in calls and "package-tts" in calls


@pytest.mark.parametrize("provider", ["cuda", "cpu"])
def test_controller_passes_its_runtime_and_reports_mlx_unavailable(provider) -> None:
    controller = AppController.__new__(AppController)
    controller.config = SimpleNamespace(tts_engine="kokoro", tts_quantization="bf16", enable_parallel=False,
                                        max_parallel_chapters=1)
    controller._runtime_capability_service = MagicMock()
    controller._runtime_capability_service.get_system_runtime_snapshot.return_value = {
        "tts": {"available": True, "latency_ms": 1.0, "error": None},
        "cleaner": {"available": True, "latency_ms": 1.0, "error": None},
    }
    controller._runtime_capability_service.get_cached_cleaner_backend_availability.return_value = {}
    controller._cleaner_api_limiter = MagicMock(snapshot=MagicMock(return_value={}))
    controller._shared_worker_pool_manager = None
    controller._voice_service = MagicMock(runtime_validation_cache_size=0)
    controller._voice_service.metrics_snapshot.return_value = {}
    spec = SimpleNamespace(provider=provider)

    with patch("modules.app.controller.TTSRuntimeSpec.from_legacy", return_value=spec), \
            patch("modules.app.controller.get_device_memory_policy", return_value=None):
        payload = controller.get_system_capabilities()

    controller._runtime_capability_service.get_system_runtime_snapshot.assert_called_once_with(primary_runtime=provider)
    assert payload["primary_runtime"] == provider
    assert payload["mlx_runtime_available"] is False
