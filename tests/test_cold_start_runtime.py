"""Cold-start optimizations must preserve this launch's runtime checks."""

from __future__ import annotations

import sys
import asyncio
from types import SimpleNamespace
import threading

import pytest

from modules.app.services.runtime_capability_service import RuntimeCapabilityService
from modules.runtime_setup.status import _probe_profile
from modules.web import server


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("cleaner_ready", [False, True])
def test_combined_probe_imports_once_and_preserves_independent_results(monkeypatch, frozen, cleaner_ready):
    service = RuntimeCapabilityService()
    service._TTS_PACKAGE_PROBE = "import json\nprint(json.dumps({'available': True, 'device': 'cuda'}))"
    service._CLEANER_CUDA_PROBE = (
        "import json\nprint(json.dumps({'available': " + str(cleaner_ready)
        + ", 'error': str(CLEANER_CUDA_QUANTIZATION)}))"
    )
    calls = []

    def execute(script, *_args, **_kwargs):
        calls.append(script)
        if frozen:
            return service._run_in_process_probe(script)
        output = []
        exec(script, {"print": output.append})
        return SimpleNamespace(stdout=output[-1], stderr="", returncode=0)

    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(service, "_run_frozen_probe", execute)
    monkeypatch.setattr("subprocess.run", lambda args, **kwargs: execute(args[-1]))
    groups = ("tts-cpu-cuda", "cleaner-cuda", "cleaner-cuda-large")
    first = _probe_profile(groups, service, cleaner_cuda_quantization="fp16")
    assert first["available"] is cleaner_ready
    assert [check["available"] for check in first["checks"]] == [True, cleaner_ready, cleaner_ready]
    assert first["checks"][0]["device"] == "cuda"
    assert first["checks"][1]["error"] == "fp16"
    assert _probe_profile(groups, service, cleaner_cuda_quantization="fp16") == first
    assert len(calls) == 1
    assert service.probe_package_tts_runtime()["available"] is True
    assert service.probe_cleaner_backend("cuda", cleaner_cuda_quantization="fp16")["available"] is cleaner_ready
    assert len(calls) == 1


@pytest.mark.parametrize("result", [
    {"available": False, "timed_out": True, "error": "timeout"},
    {"available": True, "results": {}},
    {"available": False, "error": "probe process crashed"},
])
def test_combined_probe_never_promotes_missing_results(monkeypatch, result):
    service = RuntimeCapabilityService()

    def probe(script, _timeout):
        if "scripts = " in script:
            return result
        # The independent fallback probes fail the same way, minus combined output.
        return {**{key: value for key, value in result.items() if key != "results"}, "available": False}

    monkeypatch.setattr(service, "_run_probe", probe)
    status = _probe_profile(("tts-cpu-cuda", "cleaner-cuda"), service)
    assert status["available"] is False
    assert all(check["available"] is False for check in status["checks"])
    if result.get("timed_out"):
        assert all(check["state"] == "timeout" for check in status["checks"])


def test_combined_probe_failure_falls_back_to_independent_checks(monkeypatch):
    # A GPU-driver crash in the shared process must not hide working CPU narration.
    service = RuntimeCapabilityService()
    calls = []

    def probe(script, _timeout):
        calls.append(script)
        if "scripts = " in script:
            return {"available": False, "error": "probe process crashed"}
        if script.endswith(service._TTS_PACKAGE_PROBE):
            return {"available": True, "device": "cpu", "error": None}
        return {"available": False, "error": "CUDA driver failure"}

    monkeypatch.setattr(service, "_run_probe", probe)
    tts, cleaner = service.probe_package_and_cuda_cleaner_runtime(cleaner_cuda_quantization="4bit")
    assert len(calls) == 3
    assert tts["available"] is True and tts["device"] == "cpu"
    assert cleaner["available"] is False and cleaner["error"] == "CUDA driver failure"


def test_custom_probe_implementations_are_not_bypassed(monkeypatch):
    class CustomService(RuntimeCapabilityService):
        def probe_package_tts_runtime(self):
            return {"available": True, "device": "cpu"}

    service = CustomService()
    monkeypatch.setattr(service, "probe_cleaner_backend", lambda *args: {"available": False})
    monkeypatch.setattr(service, "_run_probe", lambda *_: pytest.fail("custom probe was bypassed"))
    status = _probe_profile(("tts-cpu-cuda", "cleaner-cuda"), service)
    assert status["checks"][0]["device"] == "cpu"
    assert status["checks"][0]["available"] is True
    assert status["checks"][1]["available"] is False


def test_combined_probe_keeps_fresh_tts_and_only_probes_missing_cleaner(monkeypatch):
    service = RuntimeCapabilityService()
    service._store_result("tts-package", {"available": True, "device": "cpu"})
    calls = []

    def probe(script, _timeout):
        calls.append(script)
        return {"available": True}

    monkeypatch.setattr(service, "_run_probe", probe)
    tts, cleaner = service.probe_package_and_cuda_cleaner_runtime(cleaner_cuda_quantization="8bit")
    assert tts["device"] == "cpu"
    assert cleaner["available"] is True
    assert calls == ["CLEANER_CUDA_QUANTIZATION = '8bit'\n" + service._CLEANER_CUDA_PROBE]


@pytest.mark.parametrize("device,precision,cleaner_ready", [
    ("cpu", "4bit", False), ("cuda", "fp16", True), ("cuda", "4bit", False),
])
def test_combined_real_scripts_preserve_cpu_fallback_and_requested_precision(
    monkeypatch, device, precision, cleaner_ready,
):
    from modules.tts.runtime_spec import TTSRuntimeSpec

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: device == "cuda")))
    monkeypatch.setitem(sys.modules, "kokoro", SimpleNamespace(KModel=object, KPipeline=object))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "bitsandbytes", None)
    monkeypatch.setattr(TTSRuntimeSpec, "cuda_available_on_current_platform", staticmethod(lambda: device == "cuda"))
    monkeypatch.setattr("modules.runtime_profiles.cuda_hardware_error", lambda: None)
    service = RuntimeCapabilityService()
    monkeypatch.setattr(service, "_run_probe", lambda script, _: service._run_in_process_probe(script))
    tts, cleaner = service.probe_package_and_cuda_cleaner_runtime(cleaner_cuda_quantization=precision)
    assert tts["available"] is True
    assert tts["device"] == device
    assert cleaner["available"] is cleaner_ready


def test_capability_snapshot_reuses_fresh_setup_checks(monkeypatch):
    service = RuntimeCapabilityService()
    service._store_result("tts", {"available": False})
    service._store_cleaner_backend_result("mlx", {"available": False})
    service._store_cleaner_backend_result("cuda", {"available": True})
    monkeypatch.setattr(service, "_run_probe", lambda *_: pytest.fail("fresh setup checks must not re-import"))
    service._refresh_runtime_snapshot()
    assert service.probe_cleaner_backend("cuda")["available"] is True


def test_frozen_probe_cannot_hold_backend_alive_at_shutdown(monkeypatch):
    from unittest.mock import Mock

    receiver = Mock()
    receiver.poll.return_value = True
    receiver.recv.return_value = {"available": True}
    sender = Mock()
    worker = Mock(pid=1, daemon=False)
    worker.is_alive.return_value = False
    worker.start.side_effect = lambda: None if worker.daemon else pytest.fail("probe must be a daemon")
    context = Mock()
    context.Pipe.return_value = (receiver, sender)
    context.Process.return_value = worker
    monkeypatch.setattr("multiprocessing.get_context", lambda _: context)
    assert RuntimeCapabilityService()._run_frozen_probe("probe", 1) == {"available": True}
    worker.join.assert_called_once()
    receiver.close.assert_called_once()
    sender.close.assert_called()


def test_import_warmup_does_not_bypass_conversion_validation_or_change_409(monkeypatch, tmp_path):
    from unittest.mock import Mock

    from fastapi.testclient import TestClient
    from modules.app.config import AppConfig
    from modules.app.services.voice_service import VoiceService
    from modules.tts.runtime_spec import TTSRuntimeSpec

    config = AppConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "output", temp_dir=tmp_path / "temp")
    strategy = Mock(supported_voices=[SimpleNamespace(id="am_adam")])
    strategy.synthesize.side_effect = RuntimeError("broken synthesis")
    voice_service = VoiceService(config, tts_factory=SimpleNamespace(create=lambda *args, **kwargs: strategy))
    spec = TTSRuntimeSpec(provider="cuda", engine="kokoro", model=None, quantization="bf16")
    monkeypatch.setattr(server.setup_runtime, "_read_setup_state", lambda _: ({"profile": "cuda"}, None))
    monkeypatch.setattr(server.setup, "build_setup_status_response", lambda _: {
        "configured": True, "ready": True, "state": "ready", "selected_profile": "cuda",
    })

    def start(**_kwargs):
        voice_service.validate_tts_runtime("kokoro", runtime_spec=spec)

    app = server.create_app(controller=SimpleNamespace(config=config, start_conversion=start))
    with monkeypatch.context() as imports:
        imports.setattr(server.importlib, "import_module", lambda _: None)
        server._warm_configured_runtime_imports(app, threading.Event())
    response = TestClient(app).post("/api/conversions", json={"source_path": str(tmp_path / "book.txt")})
    assert response.status_code == 409
    assert response.json() == {"detail": f"TTS runtime check failed ({spec.label()}): broken synthesis"}
    strategy.load.assert_called_once_with()
    strategy.synthesize.assert_called_once_with("runtime check", voice="am_adam", speed=1.0)
    strategy.unload.assert_called_once_with(keep_in_cache=False)
    assert voice_service.runtime_validation_cache_size == 0


@pytest.mark.parametrize("state", [None, {"profile": "base"}, {"profile": "mlx"}])
def test_import_warmup_never_runs_for_library_only_or_unconfigured(monkeypatch, state):
    monkeypatch.setattr(server.setup_runtime, "_read_setup_state", lambda _: (state, None))
    monkeypatch.setattr(server.setup, "build_setup_status_response", lambda _: pytest.fail("unexpected probe"))
    monkeypatch.setattr(server.importlib, "import_module", lambda _: pytest.fail("unexpected import"))
    server._warm_configured_runtime_imports(server.create_app(controller=object()), threading.Event())


@pytest.mark.parametrize("status", [
    {"configured": True, "ready": False, "state": "unavailable", "selected_profile": "cuda"},
    {"configured": True, "ready": True, "state": "ready", "selected_profile": "cuda", "restart_required": True},
    {"configured": False, "ready": True, "state": "ready", "selected_profile": "cuda"},
])
def test_import_warmup_requires_verified_ready_selection(monkeypatch, status):
    monkeypatch.setattr(server.setup_runtime, "_read_setup_state", lambda _: ({"profile": "cuda"}, None))
    monkeypatch.setattr(server.setup, "build_setup_status_response", lambda _: status)
    monkeypatch.setattr(server.importlib, "import_module", lambda _: pytest.fail("unexpected import"))
    server._warm_configured_runtime_imports(server.create_app(controller=object()), threading.Event())


def test_import_warmup_only_imports_and_stops_between_imports(monkeypatch):
    cancelled = threading.Event()
    monkeypatch.setattr(server.setup_runtime, "_read_setup_state", lambda _: ({"profile": "cuda"}, None))
    monkeypatch.setattr(server.setup, "build_setup_status_response", lambda _: {
        "configured": True, "ready": True, "state": "ready", "selected_profile": "cuda",
    })
    imports = []

    def warm(module):
        imports.append(module)

    monkeypatch.setattr(server.importlib, "import_module", warm)
    app = server.create_app(controller=object())
    server._warm_configured_runtime_imports(app, cancelled)
    assert imports == ["torch", "kokoro"]
    imports.clear()

    def cancel_on_import(module):
        imports.append(module)
        cancelled.set()

    monkeypatch.setattr(server.importlib, "import_module", cancel_on_import)
    server._warm_configured_runtime_imports(app, cancelled)
    assert imports == ["torch"]
    imports.clear()
    server._warm_configured_runtime_imports(app, cancelled)
    assert imports == []


def test_shutdown_cancels_pending_warmup_before_imports(monkeypatch):
    cancelled = threading.Event()
    monkeypatch.setattr(server.setup_runtime, "_read_setup_state", lambda _: ({"profile": "cuda"}, None))

    def status(_):
        cancelled.set()
        return {"configured": True, "ready": True, "state": "ready", "selected_profile": "cuda"}

    monkeypatch.setattr(server.setup, "build_setup_status_response", status)
    monkeypatch.setattr(server.importlib, "import_module", lambda _: pytest.fail("unexpected import"))
    server._warm_configured_runtime_imports(server.create_app(controller=object()), cancelled)


def test_startup_binds_cache_before_daemon_and_signals_shutdown(monkeypatch):
    monkeypatch.setattr(server, "_acknowledge_runtime_setup_on_startup", lambda _: None)
    app = server.create_app(controller=object(), desktop_mode=True)
    workers = []

    class Thread:
        def __init__(self, *, target, args, daemon, name):
            assert app.state.runtime_setup_status_cache is not None
            assert target is server._warm_configured_runtime_imports
            assert daemon is True
            assert name == "tts-import-warmup"
            self.cancelled = args[1]
            workers.append(self)

        def start(self):
            assert not self.cancelled.is_set()

    monkeypatch.setattr(server.threading, "Thread", Thread)

    async def lifetime():
        async with app.router.lifespan_context(app):
            assert len(workers) == 1
            assert not workers[0].cancelled.is_set()
        assert workers[0].cancelled.is_set()

    asyncio.run(lifetime())
