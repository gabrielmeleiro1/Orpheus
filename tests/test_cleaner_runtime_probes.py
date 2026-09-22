"""Tests for per-backend cleaner runtime capability probes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from modules.app.controller import AppController
from modules.app.services.runtime_capability_service import RuntimeCapabilityService


def test_cleaner_mlx_probe_script_imports_mlx_lm() -> None:
    assert "import mlx_lm" in RuntimeCapabilityService._CLEANER_MLX_PROBE


def test_cleaner_cuda_probe_script_checks_torch_cuda_and_transformers() -> None:
    script = RuntimeCapabilityService._CLEANER_CUDA_PROBE
    assert "import torch" in script
    assert "torch.cuda.is_available()" in script
    assert "import transformers" in script
    assert "import bitsandbytes" in script
    assert "CLEANER_CUDA_QUANTIZATION" in script
    assert "model.load" not in script


def test_probe_cleaner_backend_api_is_always_available_without_subprocess() -> None:
    service = RuntimeCapabilityService(cache_ttl_seconds=60.0, timeout_seconds=4.0)

    with patch(
        "modules.app.services.runtime_capability_service.subprocess.run",
        side_effect=AssertionError("API cleaner must not spawn subprocess"),
    ):
        result = service.probe_cleaner_backend("api")

    assert result["available"] is True
    assert result["latency_ms"] == 0.0
    assert result["error"] is None


def test_build_cleaner_config_from_worker_init_matches_pipeline_defaults() -> None:
    from modules.tts.cleaner_config import build_cleaner_config_from_worker_init

    config = build_cleaner_config_from_worker_init(
        {
            "cleaner_model_name": "meta-llama/Llama-3.2-3B-Instruct",
            "cleaner_backend": "cuda",
            "cleaner_cuda_device": "cuda:1",
            "cleaner_cuda_quantization": "fp16",
            "cleaner_api_base_url": "https://api.example.test/v1",
            "cleaner_api_key_env": "OPENAI_API_KEY",
            "cleaner_api_model": "gpt-test",
        }
    )

    assert config.model_name == "meta-llama/Llama-3.2-3B-Instruct"
    assert config.backend == "cuda"
    assert config.cuda_device == "cuda:1"
    assert config.cuda_quantization == "fp16"
    assert config.skip_llm_when_not_needed is True


def test_probe_cleaner_backend_mlx_uses_subprocess_and_caches() -> None:
    service = RuntimeCapabilityService(cache_ttl_seconds=60.0, timeout_seconds=4.0)
    completed = MagicMock(returncode=0, stdout='{"available": true}', stderr="")

    with patch(
        "modules.app.services.runtime_capability_service.subprocess.run",
        return_value=completed,
    ) as mock_run:
        first = service.probe_cleaner_backend("mlx")
        second = service.probe_cleaner_backend("mlx")

    assert first["available"] is True
    assert first["cache_hit"] is False
    assert second["available"] is True
    assert second["cache_hit"] is True
    assert mock_run.call_count == 1


def test_probe_cleaner_backend_cuda_uses_separate_cache_key() -> None:
    service = RuntimeCapabilityService(cache_ttl_seconds=60.0, timeout_seconds=4.0)

    def fake_run(command, **kwargs):
        script = command[-1]
        if "mlx_lm" in script:
            return MagicMock(returncode=0, stdout='{"available": true}', stderr="")
        if "torch" in script:
            return MagicMock(returncode=0, stdout='{"available": false}', stderr="")
        raise AssertionError(f"unexpected probe script: {script}")

    with patch(
        "modules.app.services.runtime_capability_service.subprocess.run",
        side_effect=fake_run,
    ) as mock_run:
        mlx_result = service.probe_cleaner_backend("mlx")
        cuda_result = service.probe_cleaner_backend("cuda")

    assert mlx_result["available"] is True
    assert cuda_result["available"] is False
    assert mock_run.call_count == 2


def test_probe_local_cleaner_backends_iterates_registered_local_backends() -> None:
    service = RuntimeCapabilityService(cache_ttl_seconds=60.0, timeout_seconds=4.0)
    completed = MagicMock(returncode=0, stdout='{"available": true}', stderr="")

    with patch(
        "modules.app.services.runtime_capability_service.subprocess.run",
        return_value=completed,
    ):
        results = service.probe_local_cleaner_backends()

    assert set(results) == {"basic", "mlx", "cuda"}
    assert all(result["available"] is True for result in results.values())


def test_probe_mlx_cleaner_runtime_delegates_to_mlx_backend_probe() -> None:
    service = RuntimeCapabilityService(cache_ttl_seconds=60.0, timeout_seconds=4.0)
    completed = MagicMock(returncode=0, stdout='{"available": true}', stderr="")

    with patch(
        "modules.app.services.runtime_capability_service.subprocess.run",
        return_value=completed,
    ) as mock_run:
        alias = service.probe_mlx_cleaner_runtime()
        direct = service.probe_cleaner_backend("mlx")

    assert alias["available"] is True
    assert direct["available"] is True
    assert direct["cache_hit"] is True
    assert mock_run.call_count == 1


def test_probe_cleaner_backend_rejects_unknown_backend() -> None:
    service = RuntimeCapabilityService(cache_ttl_seconds=60.0, timeout_seconds=4.0)

    with pytest.raises(ValueError, match="not supported"):
        service.probe_cleaner_backend("bogus")


def test_get_system_capabilities_reads_cached_cleaner_availability_without_probes() -> None:
    controller = AppController.__new__(AppController)
    controller.config = SimpleNamespace(
        tts_engine="kokoro",
        tts_quantization="bf16",
        enable_parallel=False,
        max_parallel_chapters=4,
    )
    controller._runtime_capability_service = MagicMock()
    controller._runtime_capability_service.get_system_runtime_snapshot.return_value = {
        "mode": "import",
        "cache_ttl_seconds": 60,
        "cache_hit": False,
        "tts": {"available": True, "latency_ms": 10.0, "error": None},
        "cleaner": {"available": True, "latency_ms": 5.0, "error": None},
    }
    controller._runtime_capability_service.get_cached_cleaner_backend_availability.return_value = {
        "mlx": True,
        "cuda": False,
    }
    controller._cleaner_api_limiter = MagicMock()
    controller._cleaner_api_limiter.snapshot.return_value = {"wait_count": 0, "wait_ms_total": 0.0}
    controller._shared_worker_pool_manager = None
    controller._voice_service = MagicMock()
    controller._voice_service.runtime_validation_cache_size = 0
    controller._voice_service.metrics_snapshot.return_value = {}

    with patch(
        "modules.app.controller.TTSEngineFactory.available_conversion_engines",
        return_value=["kokoro"],
    ):
        payload = controller.get_system_capabilities()

    controller._runtime_capability_service.probe_local_cleaner_backends.assert_not_called()
    availability = payload["cleaner_runtime_availability"]
    assert set(availability) == {"mlx", "cuda", "api"}
    assert availability["mlx"] is True
    assert availability["cuda"] is False
    assert availability["api"] is True
    assert payload["mlx_cleaner_runtime_available"] is availability["mlx"]


def test_refresh_runtime_snapshot_stores_mlx_alias_and_per_backend_cache() -> None:
    service = RuntimeCapabilityService(cache_ttl_seconds=60.0, timeout_seconds=4.0)

    def fake_run(command, **kwargs):
        script = command[-1]
        if "mlx_audio" in script or "mlx_audio.tts" in script:
            return MagicMock(returncode=0, stdout='{"available": true}', stderr="")
        if "mlx_lm" in script:
            return MagicMock(returncode=0, stdout='{"available": true}', stderr="")
        if "torch" in script:
            return MagicMock(returncode=0, stdout='{"available": false, "error": "no gpu"}', stderr="")
        raise AssertionError(f"unexpected probe script: {script}")

    with patch(
        "modules.app.services.runtime_capability_service.subprocess.run",
        side_effect=fake_run,
    ):
        service._refresh_runtime_snapshot()

    mlx_cached = service._read_cached_cleaner_backend_result("mlx")
    cuda_cached = service._read_cached_cleaner_backend_result("cuda")
    assert mlx_cached is not None and mlx_cached["available"] is True
    assert cuda_cached is not None and cuda_cached["available"] is False

    snapshot = service.get_system_runtime_snapshot()
    assert snapshot["cleaner"]["available"] is True
    assert service.get_cached_cleaner_backend_availability()["mlx"] is True


def test_cuda_probe_reports_unavailable_when_bitsandbytes_missing_for_default_quant(
    monkeypatch,
) -> None:
    service = RuntimeCapabilityService(cache_ttl_seconds=60.0, timeout_seconds=4.0)
    monkeypatch.delenv("CLEANER_CUDA_QUANTIZATION", raising=False)

    def fake_run(command, **kwargs):
        script = command[-1]
        if "bitsandbytes" not in script:
            raise AssertionError("CUDA probe must validate bitsandbytes for default quant")
        return MagicMock(
            returncode=0,
            stdout='{"available": false, "error": "No module named \'bitsandbytes\'"}',
            stderr="",
        )

    with patch(
        "modules.app.services.runtime_capability_service.subprocess.run",
        side_effect=fake_run,
    ):
        result = service.probe_cleaner_backend("cuda")

    assert result["available"] is False
    assert "bitsandbytes" in str(result["error"]).lower()


def test_cuda_probe_reports_unavailable_when_gpu_absent() -> None:
    service = RuntimeCapabilityService(cache_ttl_seconds=60.0, timeout_seconds=4.0)

    with patch(
        "modules.app.services.runtime_capability_service.subprocess.run",
        return_value=MagicMock(
            returncode=0,
            stdout='{"available": false, "error": "CUDA is not available on this system."}',
            stderr="",
        ),
    ):
        result = service.probe_cleaner_backend("cuda")

    assert result["available"] is False
    assert "cuda is not available" in str(result["error"]).lower()
