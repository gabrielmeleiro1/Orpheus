"""Regression tests for TTS engine module decomposition wrappers."""

from __future__ import annotations

import importlib
import os
import sys
import threading
import time
import builtins
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from modules.app.controller import AppController
from modules.pipeline.model_lifecycle import _tts_strategy_kwargs
from modules.pipeline.models import PipelineConfig
from modules.tts import engine
from modules.tts import engine_runtime
from modules.tts import mlx_compat
from modules.tts import model_cache
from modules.tts.runtime_options import TTSRuntimeOptions
from modules.tts.runtime_spec import TTSRuntimeSpec
from modules.tts.runtimes.errors import TTSRuntimeUnavailable
from tests import test_cleaner_runtime_probes as _runtime_probes

frozen_spawn = _runtime_probes.frozen_spawn


@pytest.fixture(autouse=True)
def _stub_installed_voice_snapshot(monkeypatch):
    monkeypatch.setattr(
        engine_runtime.mlx_compat_backend, "installed_voice_snapshot",
        lambda model_path: (model_path, model_path),
    )


def _runtime_engine(canonical_key, **metadata):
    tts = SimpleNamespace(
        config=SimpleNamespace(
            model_name="Kokoro-82M",
            quantization="bf16",
            runtime_cache_key=canonical_key,
        ),
        _model_loaded=False,
        _model=None,
        _model_path="Kokoro-82M-bf16",
        _model_cache_key=canonical_key,
        **metadata,
    )
    tts._get_model_path = lambda: engine_runtime.get_model_path(tts.config)
    return tts


def _worker_init_config():
    return {
        "tts_runtime": TTSRuntimeSpec.from_legacy(
            "kokoro",
            "bf16",
            model="mlx-community/Kokoro-82M",
            provider="mlx",
        ).to_dict(),
        "tts_engine": "kokoro",
        "tts_model_name": "mlx-community/Kokoro-82M",
        "tts_quantization": "bf16",
        "voice": "af_bella",
        "cleaner_model_name": "mlx-community/Llama-3.2-3B-Instruct-4bit",
        "cleaner_backend": "mlx",
        "cleaner_api_base_url": "https://api.openai.com/v1",
        "cleaner_api_key_env": "OPENAI_API_KEY",
        "cleaner_api_model": "gpt-5-nano",
        "cleaner_api_global_concurrency": 6,
        "cleaner_api_global_limiter": None,
        "cleaner_skip_llm_when_not_needed": False,
    }


def _frozen_speech_package(tmp_path, monkeypatch, package_dir):
    data_dir = package_dir / "espeak-ng-data"
    library_path = package_dir / mlx_compat._espeak_library_name()
    data_dir.mkdir(parents=True)
    library_path.write_bytes(b"dylib")
    fake_loader = SimpleNamespace(
        get_library_path=lambda: "wrong-library",
        get_data_path=lambda: "wrong-data",
    )

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setitem(sys.modules, "espeakng_loader", fake_loader)

    return fake_loader, library_path, data_dir


def test_model_lifecycle_strategy_kwargs_pass_spec_runtime_options_dict() -> None:
    runtime_spec = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model="Kokoro-82M",
        quantization="bf16",
        device="cuda:0",
        runtime_options=TTSRuntimeSpec.canonicalize_runtime_options({"dtype": "fp16"}),
    )
    runtime_options = TTSRuntimeOptions(
        model_name="Kokoro-82M",
        quantization="bf16",
        provider="cuda",
        device="cuda:0",
        dtype="fp16",
    )
    runtime = SimpleNamespace(
        config=PipelineConfig(
            tts_runtime=runtime_spec,
            tts_runtime_options=runtime_options,
        )
    )

    kwargs = _tts_strategy_kwargs(runtime)

    assert kwargs["runtime_spec"] is runtime_spec
    assert kwargs["runtime_options"] == {"dtype": "fp16"}
    assert isinstance(kwargs["runtime_options"], dict)
    assert kwargs["model_name"] == "Kokoro-82M"


def test_kokoro_strategy_import_does_not_import_mlx_engine(monkeypatch) -> None:
    sys.modules.pop("modules.tts.strategies.kokoro", None)
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: ANN001
        if name == "modules.tts.engine":
            raise AssertionError("Kokoro strategy import crossed into MLX engine")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    module = importlib.import_module("modules.tts.strategies.kokoro")

    # Before load the strategy reports the provider this host would resolve to.
    assert module.KokoroTTSStrategy().capabilities.provider == module.TTSRuntimeSpec.default_provider("kokoro")


def test_mlx_runtime_import_failure_is_deferred_until_load(monkeypatch) -> None:
    kokoro = importlib.import_module("modules.tts.strategies.kokoro")
    strategy = kokoro.KokoroTTSStrategy(
        kokoro.KokoroConfig(
            runtime_spec=TTSRuntimeSpec.from_legacy(
                "kokoro",
                "bf16",
                model="mlx-community/Kokoro-82M",
                provider="mlx",
            )
        )
    )
    real_import = builtins.__import__

    assert strategy.supported_voices
    assert strategy.capabilities.provider == "mlx"

    preserved_engine = sys.modules.get("modules.tts.engine")
    sys.modules.pop("modules.tts.engine", None)

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: ANN001
        if name == "modules.tts.engine":
            raise ImportError("missing mlx runtime")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    try:
        with pytest.raises(TTSRuntimeUnavailable, match="MLX Kokoro runtime"):
            strategy.load()
    finally:
        if preserved_engine is not None:
            sys.modules["modules.tts.engine"] = preserved_engine


def test_model_cache_evicts_least_recently_used_model() -> None:
    model_cache.clear_model_cache()
    model_a = object()
    model_b = object()
    model_c = object()

    model_cache.set_cached_model("model-a", model_a)
    model_cache.set_cached_model("model-b", model_b)
    assert model_cache.get_cached_model("model-a") is model_a

    model_cache.set_cached_model("model-c", model_c)

    assert model_cache.get_cached_model("model-a") is model_a
    assert model_cache.get_cached_model("model-b") is None
    assert model_cache.get_cached_model("model-c") is model_c


def test_hard_unload_of_a_never_loaded_engine_keeps_the_warm_cached_model(monkeypatch) -> None:
    # A runtime check whose load fails still hard-unloads its fresh engine; that
    # must not evict the model a running conversion keeps warm.
    monkeypatch.setattr(engine, "_ensure_mlx_runtime", lambda: None)
    monkeypatch.setattr(engine, "_ensure_tqdm_thread_lock", lambda: None)
    model_cache.clear_model_cache()
    tts = engine.TTSEngine()
    warm_model = object()
    model_cache.set_cached_model(tts._model_cache_key, warm_model)
    model_cache.set_cached_model("other-runtime", object())

    try:
        tts.unload_model(keep_in_cache=False)
        assert model_cache.get_cached_model(tts._model_cache_key) is warm_model
        assert model_cache.get_cached_model("other-runtime") is not None
    finally:
        model_cache.clear_model_cache()


def test_runtime_hard_unload_preserves_newer_cache_entry_for_same_key() -> None:
    model_cache.clear_model_cache()
    retired_model = object()
    replacement_model = object()
    tts = SimpleNamespace(
        _model_loaded=True,
        _model=retired_model,
        _model_cache_key="shared-key",
        _warmup_complete=True,
        _last_warmup_ms=1.0,
    )
    model_cache.set_cached_model("shared-key", replacement_model)

    try:
        engine_runtime.unload_model(tts, keep_in_cache=False)
        assert model_cache.get_cached_model("shared-key") is replacement_model
    finally:
        model_cache.clear_model_cache()


def test_runtime_hard_unload_releases_detached_cached_model_reference() -> None:
    model_cache.clear_model_cache()
    retired_model = object()
    replacement_model = object()
    tts = SimpleNamespace(
        _model_loaded=True,
        _model=retired_model,
        _model_cache_key="shared-key",
        _warmup_complete=True,
        _last_warmup_ms=1.0,
    )
    engine_runtime.unload_model(tts, keep_in_cache=True)
    model_cache.set_cached_model("shared-key", replacement_model)

    try:
        engine_runtime.unload_model(tts, keep_in_cache=False)
        assert tts._last_cached_model is None
        assert model_cache.get_cached_model("shared-key") is replacement_model
    finally:
        model_cache.clear_model_cache()


def test_patch_espeakng_loader_paths_uses_pyinstaller_internal_dir(
    tmp_path,
    monkeypatch,
) -> None:
    package_dir = tmp_path / "espeakng_loader"
    fake_loader, library_path, data_dir = _frozen_speech_package(tmp_path, monkeypatch, package_dir)

    mlx_compat.patch_espeakng_loader_paths()

    assert fake_loader.get_library_path() == str(library_path)
    assert fake_loader.get_data_path() == str(data_dir)


def test_patch_espeakng_loader_paths_accepts_onedir_root_meipass(
    tmp_path,
    monkeypatch,
) -> None:
    package_dir = tmp_path / "_internal" / "espeakng_loader"
    fake_loader, library_path, data_dir = _frozen_speech_package(tmp_path, monkeypatch, package_dir)

    mlx_compat.patch_espeakng_loader_paths()

    assert fake_loader.get_library_path() == str(library_path)
    assert fake_loader.get_data_path() == str(data_dir)


def test_runtime_capability_service_caches_probe_results() -> None:
    runtime_capability_service = importlib.import_module(
        "modules.app.services.runtime_capability_service"
    )
    RuntimeCapabilityService = runtime_capability_service.RuntimeCapabilityService
    service = RuntimeCapabilityService(cache_ttl_seconds=60.0, timeout_seconds=4.0)
    completed = MagicMock(returncode=0, stdout='{"available": true}', stderr="")

    with patch("modules.app.services.runtime_capability_service.subprocess.run", return_value=completed) as mock_run:
        first = service.probe_mlx_tts_runtime()
        second = service.probe_mlx_tts_runtime()

    assert first["available"] is True
    assert first["cache_hit"] is False
    assert second["available"] is True
    assert second["cache_hit"] is True
    assert mock_run.call_count == 1


def test_runtime_capability_snapshot_returns_pending_then_refreshes_in_background() -> None:
    runtime_capability_service = importlib.import_module(
        "modules.app.services.runtime_capability_service"
    )
    RuntimeCapabilityService = runtime_capability_service.RuntimeCapabilityService
    service = RuntimeCapabilityService(cache_ttl_seconds=60.0, timeout_seconds=4.0)
    release_probe = threading.Event()
    first_probe_started = threading.Event()
    probe_calls = {"count": 0}

    def fake_run_probe(script: str, _timeout_seconds: float) -> dict[str, object]:
        probe_calls["count"] += 1
        if probe_calls["count"] == 1:
            first_probe_started.set()
            assert release_probe.wait(timeout=2.0)
            return {"available": True, "latency_ms": 12.5, "error": None, "cache_hit": False}
        if "mlx_lm" in script:
            return {"available": True, "latency_ms": 5.0, "error": None, "cache_hit": False}
        if "torch" in script:
            return {"available": False, "latency_ms": 18.0, "error": "missing", "cache_hit": False}
        raise AssertionError(f"unexpected probe script: {script!r}")

    with patch.object(service, "_run_probe", side_effect=fake_run_probe) as mock_run_probe:
        started_at = time.perf_counter()
        snapshot = service.get_system_runtime_snapshot()
        elapsed_ms = (time.perf_counter() - started_at) * 1000

        assert elapsed_ms < 100
        assert snapshot["state"] == "pending"
        assert snapshot["refresh_in_progress"] is True
        assert snapshot["cache_hit"] is False
        assert snapshot["tts"] == {"available": False, "latency_ms": None, "error": None}
        assert snapshot["cleaner"] == {"available": False, "latency_ms": None, "error": None}
        assert first_probe_started.wait(timeout=1.0)

        second = service.get_system_runtime_snapshot()
        assert second["state"] == "pending"
        assert second["refresh_in_progress"] is True
        assert mock_run_probe.call_count == 1

        release_probe.set()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            refreshed = service.get_system_runtime_snapshot()
            if refreshed["state"] == "fresh" and refreshed["refresh_in_progress"] is False:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("runtime snapshot never became fresh")

    assert refreshed["cache_hit"] is True
    assert refreshed["tts"] == {"available": True, "latency_ms": 12.5, "error": None}
    mlx_cached = service._read_cached_cleaner_backend_result("mlx")
    cuda_cached = service._read_cached_cleaner_backend_result("cuda")
    assert mlx_cached is not None and mlx_cached["available"] is True
    assert mlx_cached["latency_ms"] == 5.0
    assert cuda_cached is not None and cuda_cached["available"] is False
    assert cuda_cached["error"] == "missing"
    assert refreshed["cleaner"] == {
        "available": mlx_cached["available"],
        "latency_ms": mlx_cached["latency_ms"],
        "error": mlx_cached["error"],
    }
    assert service.get_cached_cleaner_backend_availability() == {"basic": True, "mlx": True, "cuda": False}
    assert mock_run_probe.call_count == 3


def test_runtime_capability_snapshot_returns_stale_and_deduplicates_refreshes() -> None:
    runtime_capability_service = importlib.import_module(
        "modules.app.services.runtime_capability_service"
    )
    RuntimeCapabilityService = runtime_capability_service.RuntimeCapabilityService
    service = RuntimeCapabilityService(cache_ttl_seconds=60.0, timeout_seconds=4.0)
    release_probe = threading.Event()
    first_probe_started = threading.Event()
    probe_calls = {"count": 0}

    with service._lock:
        service._cache["tts"] = (
            time.monotonic() - 1.0,
            {"available": False, "latency_ms": 101.0, "error": "stale-tts", "cache_hit": False},
        )
        service._cache["cleaner"] = (
            time.monotonic() - 1.0,
            {"available": True, "latency_ms": 202.0, "error": None, "cache_hit": False},
        )

    def fake_run_probe(script: str, _timeout_seconds: float) -> dict[str, object]:
        probe_calls["count"] += 1
        if probe_calls["count"] == 1:
            first_probe_started.set()
            assert release_probe.wait(timeout=2.0)
            return {"available": True, "latency_ms": 8.0, "error": None, "cache_hit": False}
        if "mlx_lm" in script:
            return {"available": True, "latency_ms": 9.0, "error": None, "cache_hit": False}
        if "torch" in script:
            return {"available": False, "latency_ms": 11.0, "error": "no gpu", "cache_hit": False}
        raise AssertionError(f"unexpected probe script: {script!r}")

    with patch.object(service, "_run_probe", side_effect=fake_run_probe) as mock_run_probe:
        started_at = time.perf_counter()
        snapshot = service.get_system_runtime_snapshot()
        elapsed_ms = (time.perf_counter() - started_at) * 1000

        assert elapsed_ms < 100
        assert snapshot["state"] == "stale"
        assert snapshot["refresh_in_progress"] is True
        assert snapshot["cache_hit"] is False
        assert snapshot["tts"] == {"available": False, "latency_ms": 101.0, "error": "stale-tts"}
        assert snapshot["cleaner"] == {"available": True, "latency_ms": 202.0, "error": None}
        assert first_probe_started.wait(timeout=1.0)

        snapshots: list[dict[str, object]] = []

        def read_snapshot() -> None:
            snapshots.append(service.get_system_runtime_snapshot())

        threads = [threading.Thread(target=read_snapshot) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1.0)

        assert all(item["state"] == "stale" for item in snapshots)
        assert all(item["refresh_in_progress"] is True for item in snapshots)
        assert mock_run_probe.call_count == 1

        release_probe.set()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            refreshed = service.get_system_runtime_snapshot()
            if refreshed["state"] == "fresh" and refreshed["refresh_in_progress"] is False:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("stale runtime snapshot never refreshed")

    assert refreshed["tts"] == {"available": True, "latency_ms": 8.0, "error": None}
    mlx_cached = service._read_cached_cleaner_backend_result("mlx")
    cuda_cached = service._read_cached_cleaner_backend_result("cuda")
    assert mlx_cached is not None and mlx_cached["available"] is True
    assert mlx_cached["latency_ms"] == 9.0
    assert cuda_cached is not None and cuda_cached["available"] is False
    assert cuda_cached["error"] == "no gpu"
    assert refreshed["cleaner"] == {
        "available": mlx_cached["available"],
        "latency_ms": mlx_cached["latency_ms"],
        "error": mlx_cached["error"],
    }
    assert service.get_cached_cleaner_backend_availability() == {"basic": True, "mlx": True, "cuda": False}
    assert mock_run_probe.call_count == 3


def test_runtime_capability_service_reports_subprocess_timeout() -> None:
    runtime_capability_service = importlib.import_module(
        "modules.app.services.runtime_capability_service"
    )
    RuntimeCapabilityService = runtime_capability_service.RuntimeCapabilityService
    service = RuntimeCapabilityService(cache_ttl_seconds=0.0, timeout_seconds=4.0)

    with patch(
        "modules.app.services.runtime_capability_service.subprocess.run",
        side_effect=TimeoutError("hung import"),
    ):
        result = service.probe_mlx_tts_runtime()

    assert result["available"] is False
    assert "hung import" in str(result["error"])


@pytest.mark.parametrize("delay", [0, 3])
def test_runtime_capability_service_uses_supervised_spawn_probe_when_frozen(
    frozen_spawn, monkeypatch, tmp_path, delay,
) -> None:
    runtime_capability_service = importlib.import_module(
        "modules.app.services.runtime_capability_service"
    )
    RuntimeCapabilityService = runtime_capability_service.RuntimeCapabilityService
    service = RuntimeCapabilityService(cache_ttl_seconds=60.0, timeout_seconds=1.0)
    marker = tmp_path / "worker-pid"
    service._TTS_PROBE = (
        "import os, time\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        f"time.sleep({delay})\nprint('{{\"available\": true, \"error\": null}}')"
    )

    monkeypatch.setattr(
        service,
        "_run_in_process_probe",
        MagicMock(side_effect=AssertionError("probe must execute in a worker")),
    )

    with patch(
        "modules.app.services.runtime_capability_service.subprocess.run",
        side_effect=AssertionError("frozen sidecar cannot execute Python -c"),
    ):
        started = time.monotonic()
        result = service.probe_mlx_tts_runtime()

    assert time.monotonic() - started < 2.5
    assert int(marker.read_text()) == frozen_spawn[0].pid
    assert frozen_spawn[0].pid != os.getpid()
    assert frozen_spawn[0].exitcode is not None
    service._run_in_process_probe.assert_not_called()
    assert result["available"] is (delay == 0)
    if delay:
        assert result["timed_out"] is True
        assert "timed out" in result["error"].lower()
    else:
        assert result["error"] is None
        assert service.probe_mlx_tts_runtime()["cache_hit"] is True
    assert len(frozen_spawn) == 1
    assert result["cache_hit"] is False
    assert isinstance(result["latency_ms"], float)


def test_frozen_runtime_probes_serialize_imports_across_service_instances(frozen_spawn, tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from modules.app.services import runtime_capability_service

    services = [runtime_capability_service.RuntimeCapabilityService() for _ in range(4)]
    barrier = threading.Barrier(len(services))
    (tmp_path / "probe_dependency.py").write_text(
        "import os, time\nfrom pathlib import Path\n"
        f"root = Path({str(tmp_path)!r})\n"
        "active = root / 'importing'\nactive.mkdir()\n"
        "try:\n"
        "    time.sleep(0.05)\n"
        "    (root / f'{os.getpid()}.imported').write_text('imported')\n"
        "finally:\n    active.rmdir()\n"
    )
    script = (
        f"import sys\nsys.path.insert(0, {str(tmp_path)!r})\n"
        "import probe_dependency\nprint('{\"available\": true}')"
    )

    def run(service):
        barrier.wait(timeout=5)
        return service._run_frozen_probe(script, 5)

    with ThreadPoolExecutor(max_workers=len(services)) as pool:
        results = list(pool.map(run, services))
    assert all(result["available"] for result in results)
    assert len(list(tmp_path.glob("*.imported"))) == len(services)
    assert len(frozen_spawn) == len(services)
    assert all(worker.exitcode == 0 for worker in frozen_spawn)


def test_in_process_probe_survives_missing_stdout_rebind_and_concurrency(
    monkeypatch,
) -> None:
    """Frozen/GUI probes must not rely on process-global stdout redirection.

    Packaged windowed backends often have sys.stdout is None, and setup status
    plus /api/system/capabilities probe concurrently on separate threads.
    contextlib.redirect_stdout is not thread-safe: racing redirects yield the
    exact production error ``probe produced no output`` even when imports work.
    """
    import threading

    runtime_capability_service = importlib.import_module(
        "modules.app.services.runtime_capability_service"
    )
    RuntimeCapabilityService = runtime_capability_service.RuntimeCapabilityService
    service = RuntimeCapabilityService(cache_ttl_seconds=0.0, timeout_seconds=4.0)

    monkeypatch.setattr(runtime_capability_service.sys, "stdout", None, raising=False)
    monkeypatch.setattr(runtime_capability_service.sys, "stderr", None, raising=False)

    rebind_script = """
import json
import sys
sys.stdout = type("Null", (), {"write": lambda self, s: None, "flush": lambda self: None})()
print(json.dumps({"available": True}))
"""
    rebound = service._run_in_process_probe(rebind_script)
    assert rebound["available"] is True, rebound
    assert rebound.get("error") in (None, "")

    concurrent_script = """
import json
import time
time.sleep(0.03)
print(json.dumps({"available": True}))
"""
    results: list[dict] = []
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            results.append(service._run_in_process_probe(concurrent_script))
        except BaseException as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(results) == 16
    assert all(result.get("available") is True for result in results), results
    assert all(
        "probe produced no output" not in str(result.get("error") or "")
        for result in results
    )


def test_tts_engine_warmup_model_is_idempotent() -> None:
    tts = engine.TTSEngine.__new__(engine.TTSEngine)
    tts._model_loaded = False
    tts._warmup_complete = False
    tts._last_warmup_ms = None
    tts.config = engine.TTSConfig(default_voice="af_bella")

    with patch.object(tts, "load_model") as mock_load, \
         patch.object(tts, "_synthesize_to_temp_audio", return_value=[0.0]) as mock_synth:
        tts.warmup_model()
        tts.warmup_model()

    assert mock_load.call_count == 1
    assert mock_synth.call_count == 1
    assert tts._warmup_complete is True
    assert tts._last_warmup_ms is not None


def test_tts_engine_batch_synthesis_uses_sequential_scheduler_for_serial_mlx_runtime() -> None:
    tts = engine.TTSEngine.__new__(engine.TTSEngine)
    tts.config = SimpleNamespace(sample_rate=24000)

    from modules.tts.strategies.base import SynthesisItem, SynthesisResult

    items = [
        SynthesisItem(text="first", voice="am_adam", speed=1.0, index=0),
        SynthesisItem(text="second", voice="am_adam", speed=1.0, index=1),
        SynthesisItem(text="third", voice="am_adam", speed=1.0, index=2),
    ]
    progress = []

    def _fake_synthesize_single(item):
        return SynthesisResult(audio=[0.0], index=item.index, error=None, duration_ms=1)

    with patch.object(tts, "_synthesize_single", side_effect=_fake_synthesize_single):
        results = list(
            tts.iter_synthesize_batch(
                items,
                progress_callback=lambda completed, total: progress.append((completed, total)),
            )
        )

    assert [result.index for result in results] == [0, 1, 2]
    assert progress == [(1, 3), (2, 3), (3, 3)]


def test_tts_engine_shared_retry_helper_preserves_real_result_types(tmp_path) -> None:
    tts = engine.TTSEngine.__new__(engine.TTSEngine)
    tts._model_path = "mlx-community/Kokoro-82M-bf16"
    tts._retry_count = 0
    audio = np.array([0.0, 0.25], dtype=np.float32)
    audio_attempts = {"count": 0}

    def flaky_audio_operation() -> np.ndarray:
        audio_attempts["count"] += 1
        if audio_attempts["count"] == 1:
            raise OSError("network timeout while downloading model")
        return audio

    output_path = tmp_path / "chunk.wav"
    path_attempts = {"count": 0}

    def flaky_path_operation() -> Path:
        path_attempts["count"] += 1
        if path_attempts["count"] == 1:
            raise RuntimeError("model network load timeout")
        return output_path

    with patch.object(tts, "_retry_wait_seconds", return_value=0), patch.object(
        engine.time,
        "sleep",
    ) as mock_sleep:
        audio_result = tts._synthesize_with_retries(  # noqa: SLF001 - direct retry contract
            flaky_audio_operation,
            max_retries=2,
        )
        path_result = tts._synthesize_with_retries(  # noqa: SLF001 - direct retry contract
            flaky_path_operation,
            max_retries=2,
        )

    assert isinstance(audio_result, np.ndarray)
    np.testing.assert_array_equal(audio_result, audio)
    assert path_result == output_path
    assert audio_attempts["count"] == 2
    assert path_attempts["count"] == 2
    assert tts._retry_count == 2
    assert [call_args.args for call_args in mock_sleep.call_args_list] == [(0,), (0,)]


def test_public_synthesis_methods_clamp_speed_and_default_voice(tmp_path) -> None:
    tts = engine.TTSEngine.__new__(engine.TTSEngine)
    tts.config = SimpleNamespace(default_voice="af_bella", sample_rate=24000)
    tts._model_path = "mlx-community/Kokoro-82M-bf16"
    audio = np.array([0.0, 0.5], dtype=np.float32)
    output_path = tmp_path / "out.wav"

    with patch.object(tts, "_synthesize_once", return_value=audio) as synth_once, \
         patch.object(tts, "_synthesize_to_output_file", return_value=output_path) as synth_file, \
         patch.object(tts, "_update_synthesis_stats"):
        audio_result = tts.synthesize(
            "hello",
            voice=None,
            speed=4.0,
            max_retries=5,
        )
        path_result = tts.synthesize_to_output_file(
            "hello",
            output_path,
            voice=None,
            speed=0.1,
            max_retries=7,
        )

    assert audio_result is audio
    assert path_result == output_path
    synth_once.assert_called_once_with(
        text="hello",
        voice="af_bella",
        speed=3.0,
        output_path=None,
    )
    synth_file.assert_called_once_with(
        text="hello",
        voice="af_bella",
        speed=0.5,
        output_path=output_path,
    )


@pytest.mark.parametrize("schema_path", [
    "modules.web.schemas.conversions:ConversionRuntimeOptionsRequest",
    "modules.web.routes.voices:VoicePreviewRequest",
])
def test_tts_layers_honor_the_fastest_speed_the_api_accepts(schema_path) -> None:
    from modules.tts.strategies.base import TTSStrategy

    module_name, class_name = schema_path.split(":")
    schema = getattr(importlib.import_module(module_name), class_name)
    api_max = next(
        rule.le for rule in schema.model_fields["speed"].metadata if getattr(rule, "le", None)
    )

    assert engine.TTSEngine._clamp_speed(api_max) == api_max
    assert TTSStrategy._clamp_speed(None, api_max) == api_max


def test_engine_runtime_helpers_cover_warmup_and_metrics_contract() -> None:
    tts = engine.TTSEngine.__new__(engine.TTSEngine)
    tts.config = SimpleNamespace(
        model_name="mlx-community/Kokoro-82M",
        quantization="bf16",
        default_voice="af_bella",
    )
    tts._model_loaded = False
    tts._model = None
    tts._model_path = "mlx-community/Kokoro-82M-bf16"
    tts._warmup_complete = False
    tts._last_warmup_ms = None
    tts._engine_start_time = 1.0
    tts._first_audio_time = None
    tts._total_chunks_processed = 0
    tts._total_chars_processed = 0
    tts._total_time_ms = 0.0
    tts._retry_count = 0

    with patch.object(engine_runtime, "_get_cached_model", return_value=None), patch.object(
        engine_runtime.mlx_compat_backend,
        "load_tts_model",
        return_value="loaded-model",
        create=True,
    ):
        engine_runtime.load_model(tts)

    assert tts._model == "loaded-model"
    assert tts._model_loaded is True

    with patch.object(tts, "_synthesize_to_temp_audio", return_value=[0.0]) as mock_synth:
        engine_runtime.warmup_model(tts)

    assert mock_synth.call_count == 1
    assert engine_runtime.get_metrics(tts)["warmup_complete"] is True


def test_engine_runtime_explicit_quantization_override_does_not_reuse_canonical_cache() -> None:
    canonical_key = "('mlx', 'kokoro', 'Kokoro-82M', 'bf16', None, ())"
    bf16_model = object()
    four_bit_model = object()
    cached = {canonical_key: bf16_model}
    tts = _runtime_engine(canonical_key, _canonical_quantization='bf16', _canonical_cache_key=canonical_key)
    tts.config.quantization = "Q4"

    with patch.object(engine_runtime, "_get_cached_model", side_effect=cached.get) as mock_get, \
         patch.object(
             engine_runtime.mlx_compat_backend,
             "load_tts_model",
             return_value=four_bit_model,
             create=True,
         ) as mock_load:
        engine_runtime.load_model(tts, quantization="Q4")

    assert tts._model is four_bit_model
    assert tts._model is not bf16_model
    assert tts._model_path == "Kokoro-82M-4bit"
    assert tts._model_cache_key != canonical_key
    assert tts.config.runtime_cache_key == canonical_key
    mock_load.assert_called_once_with("Kokoro-82M-4bit")
    assert mock_get.call_args.args[0] == tts._model_cache_key


def test_engine_runtime_quantization_aliases_share_override_cache_identity() -> None:
    canonical_key = "('mlx', 'kokoro', 'Kokoro-82M', 'bf16', None, ())"
    four_bit_model = object()
    cache: dict[str, object] = {}

    def load_engine(quantization: str):
        tts = _runtime_engine(canonical_key, _canonical_quantization='bf16', _canonical_cache_key=canonical_key)
        with patch.object(engine_runtime, "_get_cached_model", side_effect=cache.get), \
             patch.object(
                 engine_runtime.mlx_compat_backend,
                 "load_tts_model",
                 return_value=four_bit_model,
                 create=True,
             ) as mock_load:
            engine_runtime.load_model(tts, quantization=quantization)
        return tts, mock_load

    first, first_load = load_engine("Q4")
    cache[first._model_cache_key] = first._model
    second, second_load = load_engine("4BIT")

    assert first._model_cache_key == second._model_cache_key
    assert second._model is four_bit_model
    first_load.assert_called_once()
    second_load.assert_not_called()


def test_engine_runtime_repeated_override_alias_keeps_effective_private_key() -> None:
    canonical_key = "('mlx', 'kokoro', 'Kokoro-82M', 'bf16', None, ())"
    private_key = repr((canonical_key, "Kokoro-82M-4bit", "4bit"))
    bf16_model = object()
    four_bit_model = object()
    cache = {canonical_key: bf16_model, private_key: four_bit_model}
    tts = _runtime_engine(canonical_key)

    with patch.object(engine_runtime, "_get_cached_model", side_effect=cache.get):
        engine_runtime.load_model(tts, quantization="Q4")
        tts._model_loaded = False
        engine_runtime.load_model(tts, quantization="4BIT")
        assert tts._model_cache_key == private_key
        assert tts._model is four_bit_model

        tts._model_loaded = False
        engine_runtime.load_model(tts, quantization="bf16")

    assert tts._model_cache_key == canonical_key
    assert tts._model is bf16_model


def test_engine_runtime_no_arg_load_tracks_mutated_config_quantization() -> None:
    canonical_key = "('mlx', 'kokoro', 'Kokoro-82M', 'bf16', None, ())"
    private_key = repr((canonical_key, "Kokoro-82M-4bit", "4bit"))
    bf16_model = object()
    four_bit_model = object()
    cache = {canonical_key: bf16_model, private_key: four_bit_model}
    tts = _runtime_engine(canonical_key)
    tts.config.quantization = "4bit"

    with patch.object(engine_runtime, "_get_cached_model", side_effect=cache.get):
        engine_runtime.load_model(tts)

    assert tts._model is four_bit_model
    assert tts._model_path == "Kokoro-82M-4bit"
    assert tts._model_cache_key == private_key


def test_engine_runtime_no_arg_load_switches_private_variants_and_canonical_model() -> None:
    canonical_key = "('mlx', 'kokoro', 'Kokoro-82M', 'bf16', None, ())"
    q4_key = repr((canonical_key, "Kokoro-82M-4bit", "4bit"))
    q8_key = repr((canonical_key, "Kokoro-82M-8bit", "8bit"))
    bf16_model = object()
    four_bit_model = object()
    eight_bit_model = object()
    cache = {canonical_key: bf16_model, q4_key: four_bit_model, q8_key: eight_bit_model}
    tts = _runtime_engine(canonical_key)

    with patch.object(engine_runtime, "_get_cached_model", side_effect=cache.get):
        engine_runtime.load_model(tts, quantization="4bit")
        tts._model_loaded = False
        tts.config.quantization = "8bit"
        engine_runtime.load_model(tts)
        assert tts._model is eight_bit_model
        assert tts._model_path == "Kokoro-82M-8bit"
        assert tts._model_cache_key == q8_key

        tts._model_loaded = False
        tts.config.quantization = "bf16"
        engine_runtime.load_model(tts)

    assert tts._model is bf16_model
    assert tts._model_path == "Kokoro-82M-bf16"
    assert tts._model_cache_key == canonical_key


def test_engine_runtime_model_path_mutation_does_not_reuse_canonical_model() -> None:
    canonical_key = "('mlx', 'kokoro', 'Model-A', 'bf16', None, ())"
    canonical_model = object()
    model_b = object()
    cache = {canonical_key: canonical_model}
    tts = SimpleNamespace(
        config=SimpleNamespace(
            model_name="Model-A",
            quantization="bf16",
            runtime_cache_key=canonical_key,
        ),
        _model_loaded=False,
        _model=None,
        _model_path="Model-A-bf16",
        _model_cache_key=canonical_key,
        _canonical_quantization="bf16",
        _canonical_cache_key=canonical_key,
        _canonical_model_path="Model-A-bf16",
    )
    tts._get_model_path = lambda: engine_runtime.get_model_path(tts.config)
    tts.config.model_name = "Model-B"

    with patch.object(engine_runtime, "_get_cached_model", side_effect=cache.get), \
         patch.object(
             engine_runtime.mlx_compat_backend,
             "load_tts_model",
             return_value=model_b,
             create=True,
         ) as mock_load:
        engine_runtime.load_model(tts)

    assert tts._model is model_b
    assert tts._model is not canonical_model
    assert tts._model_path == "Model-B-bf16"
    assert tts._model_cache_key != canonical_key
    mock_load.assert_called_once_with("Model-B-bf16")


def test_app_controller_capabilities_use_runtime_probe_service(monkeypatch) -> None:
    monkeypatch.setattr("modules.tts.runtime_spec.platform.system", lambda: "Linux")
    monkeypatch.setattr("modules.tts.runtime_spec.platform.machine", lambda: "x86_64")
    monkeypatch.setattr(
        TTSRuntimeSpec,
        "cuda_available_on_current_platform",
        staticmethod(lambda: False),
    )
    controller = AppController.__new__(AppController)
    controller.config = SimpleNamespace(
        tts_engine="kokoro",
        tts_quantization="bf16",
        enable_parallel=True,
    )
    controller._runtime_capability_service = MagicMock()
    controller._runtime_capability_service.get_system_runtime_snapshot.return_value = {
        "mode": "import",
        "cache_ttl_seconds": 60,
        "cache_hit": False,
        "tts": {"available": False, "latency_ms": 10.0, "error": "missing"},
        "cleaner": {"available": True, "latency_ms": 12.0, "error": None},
    }
    controller._runtime_capability_service.get_cached_cleaner_backend_availability.return_value = {
        "mlx": True,
        "cuda": False,
    }
    controller._cleaner_api_limiter = MagicMock()
    controller._cleaner_api_limiter.snapshot.return_value = {"wait_count": 0, "wait_ms_total": 0.0}
    controller._shared_worker_pool_manager = None
    controller._voice_service = MagicMock()
    controller._voice_service.runtime_validation_cache_size = 3
    controller._voice_service.metrics_snapshot.return_value = {}

    with patch("modules.app.controller.TTSEngineFactory.available_engines", return_value=["kokoro"]):
        payload = controller.get_system_capabilities()

    assert payload["mlx_runtime_available"] is False
    assert payload["primary_runtime"] == "cpu"
    assert payload["mlx_cleaner_runtime_available"] is True
    assert payload["runtime_probe"]["mode"] == "import"


def test_app_controller_capabilities_reports_conversion_filtered_tts_engines() -> None:
    controller = AppController.__new__(AppController)
    controller.config = SimpleNamespace(
        tts_engine="kokoro",
        tts_quantization="bf16",
        enable_parallel=True,
    )
    controller._runtime_capability_service = MagicMock()
    controller._runtime_capability_service.get_system_runtime_snapshot.return_value = {
        "mode": "import",
        "cache_ttl_seconds": 60,
        "cache_hit": False,
        "tts": {"available": True, "latency_ms": 10.0, "error": None},
        "cleaner": {"available": True, "latency_ms": 12.0, "error": None},
    }
    controller._cleaner_api_limiter = MagicMock()
    controller._cleaner_api_limiter.snapshot.return_value = {"wait_count": 0, "wait_ms_total": 0.0}
    controller._shared_worker_pool_manager = None
    controller._voice_service = MagicMock()
    controller._voice_service.runtime_validation_cache_size = 0
    controller._voice_service.metrics_snapshot.return_value = {}

    with patch(
        "modules.app.controller.TTSEngineFactory.available_engines",
        return_value=["kokoro", "removed_engine"],
    ), patch(
        "modules.app.controller.TTSEngineFactory.available_conversion_engines",
        return_value=["kokoro"],
    ):
        payload = controller.get_system_capabilities()

    assert payload["available_tts_engines"] == ["kokoro"]


def test_pipeline_warmup_models_calls_tts_warmup_model() -> None:
    model_lifecycle = importlib.import_module("modules.pipeline.model_lifecycle")
    pipeline = SimpleNamespace(
        config=SimpleNamespace(
            tts_engine="kokoro",
            tts_model_name="mlx-community/Kokoro-82M",
            tts_quantization="bf16",
            voice="af_bella",
            cleaner_model_name="mlx-community/Llama-3.2-3B-Instruct-4bit",
            cleaner_backend="mlx",
            cleaner_api_base_url="https://api.openai.com/v1",
            cleaner_api_key_env="OPENAI_API_KEY",
            cleaner_api_key=None,
            cleaner_api_model="gpt-5-nano",
            cleaner_api_global_concurrency=6,
            cleaner_api_global_limiter=None,
            cleaner_skip_llm_when_not_needed=False,
        ),
        state=SimpleNamespace(tts_engine=None, text_cleaner=MagicMock()),
        log_verbose=MagicMock(),
    )

    fake_engine = MagicMock()
    fake_cleaner = MagicMock()

    with patch("modules.tts.factory.TTSEngineFactory.create", return_value=fake_engine) as mock_create, \
         patch("modules.tts.cleaner.TextCleaner", return_value=fake_cleaner):
        model_lifecycle.warmup_models(pipeline)

    mock_create.assert_called_once()
    fake_engine.load.assert_called_once_with()
    fake_engine.warmup.assert_called_once_with(voice="af_bella")


def test_worker_init_warms_tts_model_before_reporting_ready() -> None:
    worker_runtime = importlib.import_module("modules.pipeline.worker_runtime")
    fake_engine = MagicMock()
    fake_cleaner = MagicMock()
    worker_init_config = _worker_init_config()

    with patch("modules.tts.factory.TTSEngineFactory.create", return_value=fake_engine) as mock_create, \
         patch("modules.tts.cleaner.TextCleaner", return_value=fake_cleaner), \
         patch.object(worker_runtime.logger, "info") as mock_info, \
         patch.object(worker_runtime.logger, "warning") as mock_warning:
        worker_runtime._init_worker(available_vram_gb=8.0, active_workers=2, worker_init_config=worker_init_config)

    mock_create.assert_called_once()
    fake_engine.load.assert_called_once_with()
    fake_engine.warmup.assert_called_once_with(voice="af_bella")
    fake_cleaner.load.assert_called_once_with()
    assert worker_runtime.get_worker_startup_status().success is True
    assert worker_runtime.get_worker_cached_models() == (fake_engine, fake_cleaner)
    mock_info.assert_called_once()
    assert "warmed" in mock_info.call_args.args[0].lower()
    mock_warning.assert_not_called()


def test_worker_init_records_warmup_failure_without_dropping_loaded_engine() -> None:
    worker_runtime = importlib.import_module("modules.pipeline.worker_runtime")
    fake_engine = MagicMock()
    fake_engine.warmup.side_effect = RuntimeError("probe warmup failed")
    fake_cleaner = MagicMock()
    worker_init_config = _worker_init_config()

    with patch("modules.tts.factory.TTSEngineFactory.create", return_value=fake_engine) as mock_create, \
         patch("modules.tts.cleaner.TextCleaner", return_value=fake_cleaner), \
         patch.object(worker_runtime.logger, "info") as mock_info, \
         patch.object(worker_runtime.logger, "warning") as mock_warning:
        worker_runtime._init_worker(available_vram_gb=8.0, active_workers=2, worker_init_config=worker_init_config)

    mock_create.assert_called_once()
    fake_engine.load.assert_called_once_with()
    fake_engine.warmup.assert_called_once_with(voice="af_bella")
    fake_cleaner.load.assert_called_once_with()
    startup_status = worker_runtime.get_worker_startup_status()
    assert startup_status.success is True
    assert startup_status.provider == "mlx"
    assert worker_runtime.get_worker_cached_models()[0] is fake_engine
    mock_warning.assert_called_once()
    assert "warm" in mock_warning.call_args.args[0].lower()
    assert "warmed" not in " ".join(str(call.args[0]).lower() for call in mock_info.call_args_list)
