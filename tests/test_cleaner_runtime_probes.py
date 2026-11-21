"""Tests for per-backend cleaner runtime capability probes."""

from __future__ import annotations

import multiprocessing
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from modules.app.controller import AppController
from modules.app.services.runtime_capability_service import RuntimeCapabilityService


@pytest.fixture
def frozen_spawn(monkeypatch):
    context = multiprocessing.get_context("spawn")
    original_process = context.Process
    workers = []

    def process(*args, **kwargs):
        worker = original_process(*args, **kwargs)
        start = worker.start

        def start_source_worker():
            # The test interpreter uses Python's CLI instead of a frozen launcher.
            del worker.start
            monkeypatch.delattr(sys, "frozen")
            try:
                start()
            finally:
                monkeypatch.setattr(sys, "frozen", True, raising=False)

        worker.start = start_source_worker
        workers.append(worker)
        return worker

    monkeypatch.setattr(context, "Process", process)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(multiprocessing, "get_context", lambda method: context if method == "spawn" else None)
    yield workers
    for worker in workers:
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=2)
        worker.close()


def test_frozen_probe_timeout_terminates_worker_and_allows_retry(frozen_spawn, tmp_path):
    marker = tmp_path / "probe-started"
    service = RuntimeCapabilityService(timeout_seconds=1.0)
    service._TTS_PACKAGE_PROBE = (
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('started')\n"
        "import time\ntime.sleep(3)\nprint('{\"available\": true}')"
    )
    started = time.monotonic()
    first = service.probe_package_tts_runtime()
    assert time.monotonic() - started < 2.5
    assert first["available"] is False
    assert "timed out" in first["error"].lower()
    assert marker.read_text() == "started"
    assert len(frozen_spawn) == 1
    assert frozen_spawn[0].exitcode is not None

    service._TTS_PACKAGE_PROBE = 'print(\'{"available": true, "device": "cpu"}\')'
    retry = service.probe_package_tts_runtime()
    assert retry["available"] is True
    assert retry["device"] == "cpu"
    assert retry["cache_hit"] is False
    assert len(frozen_spawn) == 2


def test_frozen_probe_allows_cold_imports_beyond_fifteen_seconds(frozen_spawn):
    service = RuntimeCapabilityService()
    service._TTS_PACKAGE_PROBE = "import time\ntime.sleep(16)\nprint('{\"available\": true}')"
    result = service.probe_package_tts_runtime()
    assert result["available"] is True
    assert result.get("timed_out") is not True


def test_frozen_probe_lock_wait_preserves_worker_deadline(frozen_spawn):
    from modules.app.services.runtime_capability_service import _FROZEN_PROBE_LOCK

    locked = threading.Event()

    def hold_lock():
        with _FROZEN_PROBE_LOCK:
            locked.set()
            time.sleep(2.2)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    try:
        assert locked.wait(timeout=1)
        result = RuntimeCapabilityService()._run_probe('print(\'{"available": true}\')', 2.0)
        assert result["available"] is True
        assert len(frozen_spawn) == 1
    finally:
        holder.join(timeout=3)


def test_frozen_probe_lock_wait_is_bounded_separately(frozen_spawn):
    from modules.app.services.runtime_capability_service import _FROZEN_PROBE_LOCK

    locked = threading.Event()
    release = threading.Event()

    def hold_lock():
        with _FROZEN_PROBE_LOCK:
            locked.set()
            release.wait(timeout=3)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    try:
        assert locked.wait(timeout=1)
        service = RuntimeCapabilityService(lock_timeout_seconds=0.05)
        result = service._run_probe('print(\'{"available": true}\')', 2.0)
        assert result["timed_out"] is True
        assert frozen_spawn == []
    finally:
        release.set()
        holder.join(timeout=1)


def test_frozen_probe_reports_worker_exit_without_a_result(frozen_spawn):
    result = RuntimeCapabilityService()._run_probe("import os\nos._exit(3)", 1.0)
    assert result["available"] is False
    assert result["error"] == "Runtime probe exited without a result"
    assert frozen_spawn[0].exitcode == 3


def test_setup_status_retry_after_frozen_probe_timeout(frozen_spawn):
    from modules.web.setup_status_cache import RuntimeSetupStatusCache

    service = RuntimeCapabilityService(timeout_seconds=0.5)
    cache = RuntimeSetupStatusCache(probe_service=service)
    service._TTS_PACKAGE_PROBE = "import time\ntime.sleep(3)"

    def build(probes):
        # Like the status projection, a timed-out selected probe marks the
        # whole status as still checking.
        probe = probes.probe_package_tts_runtime()
        return {"probe": probe, "checking": bool(probe.get("timed_out"))}

    first = cache.get(("cuda",), build)
    assert first["probe"]["timed_out"] is True
    service._TTS_PACKAGE_PROBE = 'print(\'{"available": true}\')'
    retry = cache.get(("cuda",), build)
    assert retry["probe"]["available"] is True
    assert retry["probe"]["cache_hit"] is False


@pytest.mark.parametrize("gpu, driver_error", [
    (False, None), (True, None),
    (True, "Needs an NVIDIA GPU with a CUDA 12.6 driver"),
])
def test_package_probe_scripts_report_device_and_cleaner_requirement(monkeypatch, gpu, driver_error):
    monkeypatch.setattr("modules.runtime_profiles.cuda_hardware_error", lambda: driver_error)
    # The probe scripts really run in a one-shot child, where the kernel launch
    # happens in-process; here they are executed inside the test process.
    monkeypatch.setattr("modules.tts.runtime_spec._cuda_probe_runs_in_process", lambda: True)
    tensor = MagicMock()
    tensor.add_.return_value.item.return_value = 2
    ones = MagicMock(return_value=tensor)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: gpu), ones=ones,
    ))
    monkeypatch.setitem(sys.modules, "kokoro", SimpleNamespace(KModel=object, KPipeline=object))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "bitsandbytes", SimpleNamespace())
    service = RuntimeCapabilityService()
    tts = service._run_in_process_probe(service._TTS_PACKAGE_PROBE)
    assert tts["available"] is True
    assert tts["device"] == ("cuda" if gpu else "cpu")
    if gpu:
        ones.assert_called_once_with(1, device="cuda")
        tensor.add_.assert_called_once_with(1)
        tensor.add_.return_value.item.assert_called_once_with()
    else:
        ones.assert_not_called()
    cleaner = service._run_in_process_probe(service._CLEANER_CUDA_PROBE)
    assert cleaner["available"] is (gpu and driver_error is None)
    if not gpu or driver_error:
        assert cleaner["error"] == "Needs an NVIDIA GPU with a CUDA 12.6 driver"


def test_cleaner_mlx_probe_requires_mlx_lm(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "mlx", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "mlx.core", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace())
    service = RuntimeCapabilityService()
    assert service._run_in_process_probe(service._CLEANER_MLX_PROBE)["available"] is True

    monkeypatch.setitem(sys.modules, "mlx_lm", None)
    assert service._run_in_process_probe(service._CLEANER_MLX_PROBE)["available"] is False


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
    ), patch(
        "modules.tts.device_memory.MLXDeviceMemoryPolicy._resolve_mlx_core",
        return_value=None,
    ):
        payload = controller.get_system_capabilities()

    controller._runtime_capability_service.probe_cleaner_backend.assert_not_called()
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

    snapshot = service.get_system_runtime_snapshot()
    assert snapshot["cleaner"]["available"] is True
    availability = service.get_cached_cleaner_backend_availability()
    assert availability["mlx"] is True
    assert availability["cuda"] is False


def test_cuda_probe_reports_unavailable_when_gpu_absent() -> None:
    service = RuntimeCapabilityService(cache_ttl_seconds=60.0, timeout_seconds=4.0)

    with patch(
        "modules.app.services.runtime_capability_service.subprocess.run",
        return_value=MagicMock(
            returncode=0,
            stdout='{"available": false, "error": "Needs an NVIDIA GPU with a CUDA 12.6 driver"}',
            stderr="",
        ),
    ):
        result = service.probe_cleaner_backend("cuda")

    assert result["available"] is False
    assert result["error"] == "Needs an NVIDIA GPU with a CUDA 12.6 driver"



@pytest.mark.parametrize("precision", [None, "4bit", "8bit", "fp16"])
def test_profile_checks_reuse_timed_out_cleaner_probe_for_shared_backend(precision):
    from scripts import setup_runtime

    service = MagicMock(spec=RuntimeCapabilityService)
    service.probe_cleaner_backend.return_value = {"available": False, "timed_out": True, "error": "slow import"}
    result = setup_runtime._probe_profile(
        ["cleaner-cuda", "cleaner-cuda-large"], service, cleaner_cuda_quantization=precision,
    )
    assert [check["state"] for check in result["checks"]] == ["timeout", "timeout"]
    service.probe_cleaner_backend.assert_called_once_with(
        "cuda", **({"cleaner_cuda_quantization": precision} if precision is not None else {}),
    )


@pytest.mark.parametrize("requested", ["4bit", "8bit", "fp16"])
def test_cuda_cleaner_probe_uses_requested_precision_and_separate_cache(monkeypatch, requested):
    monkeypatch.setenv("CLEANER_CUDA_QUANTIZATION", "fp16")
    monkeypatch.setattr("modules.runtime_profiles.cuda_hardware_error", lambda: None)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True)))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "bitsandbytes", None)
    service = RuntimeCapabilityService()
    calls = []

    def probe(script, timeout):
        calls.append(script)
        return service._run_in_process_probe(script)

    monkeypatch.setattr(service, "_run_probe", probe)
    assert service.probe_cleaner_backend("cuda", cleaner_cuda_quantization="fp16")["available"]
    result = service.probe_cleaner_backend("cuda", cleaner_cuda_quantization=requested)
    assert result["available"] is (requested == "fp16")
    assert len(calls) == (1 if requested == "fp16" else 2)
    assert service.probe_cleaner_backend("cuda", cleaner_cuda_quantization=requested)["cache_hit"]
    assert service.probe_cleaner_backend("cuda", cleaner_cuda_quantization="fp16")["available"]


def test_cuda_cleaner_explicit_fp16_does_not_require_environment_quantization_dependencies(monkeypatch):
    monkeypatch.setenv("CLEANER_CUDA_QUANTIZATION", "4bit")
    monkeypatch.setattr("modules.runtime_profiles.cuda_hardware_error", lambda: None)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True)))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "bitsandbytes", None)
    service = RuntimeCapabilityService()
    monkeypatch.setattr(service, "_run_probe", lambda script, timeout: service._run_in_process_probe(script))
    assert not service.probe_cleaner_backend("cuda")["available"]
    assert service.probe_cleaner_backend("cuda", cleaner_cuda_quantization="fp16")["available"]


@pytest.mark.parametrize("precision", ["4bit", "8bit", "fp16"])
@pytest.mark.parametrize("operation", ["install", "status"])
def test_setup_verifies_selected_precision_instead_of_environment(monkeypatch, tmp_path, precision, operation):
    from huggingface_hub import constants as hf_constants
    from scripts import setup_runtime

    monkeypatch.setattr(hf_constants, "HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setenv("CLEANER_CUDA_QUANTIZATION", "fp16" if precision != "fp16" else "4bit")
    monkeypatch.setattr("modules.runtime_profiles.cuda_hardware_error", lambda: None)
    monkeypatch.setattr(setup_runtime, "cuda_hardware_error", lambda: None)
    monkeypatch.setattr(type(setup_runtime.get_runtime_profile("cuda")), "is_compatible", lambda self, **kwargs: True)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True)))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "bitsandbytes", None)
    service = RuntimeCapabilityService()
    monkeypatch.setattr(service, "_run_probe", lambda script, timeout: service._run_in_process_probe(script))
    monkeypatch.setattr(service, "probe_package_tts_runtime", lambda: {"available": True})
    state_path = tmp_path / "runtime-setup.json"
    selection = {
        "profile_id": "cuda", "model_groups": ["tts-cpu-cuda", "cleaner-cuda"],
        "cleaner_cuda_quantization": precision,
    }
    if operation == "status":
        setup_runtime.write_setup_state(state_path, **selection)
        status = setup_runtime.get_setup_status(state_path=state_path, probe_service=service)
        assert status["ready"] is (precision == "fp16")
    else:
        def install():
            return setup_runtime.run_setup(
                **selection, state_path=state_path, probe_service=service,
                command_runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
            )

        if precision == "fp16":
            install()
            assert setup_runtime.read_setup_state(state_path)["cleaner_cuda_quantization"] == precision
        else:
            with pytest.raises(setup_runtime.SetupExecutionError, match="bitsandbytes"):
                install()
            assert not state_path.exists()
