"""Runtime setup regressions without downloads or user-cache mutation."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from fastapi.testclient import TestClient

from modules.runtime_profiles import RuntimeProfile, cuda_hardware_error
from modules.web.routes import setup as routes
from modules.web.server import create_app
from modules.app.services import model_download_service as downloads
from scripts import setup_runtime


@pytest.mark.parametrize("endpoint", ["adopt", "models/download"])
@pytest.mark.parametrize("groups", [[], ["cleaner-cuda"]])
def test_voice_required_before_setup_work(monkeypatch, endpoint, groups):
    monkeypatch.setattr(RuntimeProfile, "is_compatible", lambda self, **_: True)
    with TestClient(create_app(controller=object())) as client:
        response = client.post(f"/api/setup/{endpoint}", json={"profile": "cuda", "model_groups": groups})
    assert response.status_code == 422
    assert "Kokoro voice model" in response.json()["detail"]["message"]


def test_cuda_cleaner_fails_before_install_or_download(monkeypatch, tmp_path):
    monkeypatch.setattr(RuntimeProfile, "is_compatible", lambda self, **_: True)
    monkeypatch.setattr(setup_runtime, "cuda_hardware_error", lambda: "Install the NVIDIA driver.")
    calls = []
    with pytest.raises(setup_runtime.SetupValidationError, match="NVIDIA driver"):
        setup_runtime.run_setup(profile_id="cuda", model_groups=["tts-cpu-cuda", "cleaner-cuda"], state_path=tmp_path / "state.json", command_runner=lambda *a, **k: calls.append(a))
    assert calls == []
    assert not (tmp_path / "state.json").exists()
    monkeypatch.setattr(routes, "cuda_hardware_error", lambda: "Install the NVIDIA driver.")
    card = routes._profile_payload("cuda", accepted_profile=None)
    assert card["compatible"] is True
    assert card["device"] == "cpu"
    assert card["group_availability"]["cleaner-cuda"]["available"] is False
    with TestClient(create_app(controller=object())) as client:
        response = client.post("/api/setup/models/download", json={"profile": "cuda", "model_groups": ["tts-cpu-cuda", "cleaner-cuda"]})
    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "Install the NVIDIA driver."


@pytest.mark.parametrize("count,version,init,ready", [(1, 12060, 0, True), (0, 12060, 0, False), (1, 11000, 0, False), (1, 12060, 100, False)])
def test_cuda_driver_probe(monkeypatch, count, version, init, ready):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    def assign(pointer, value):
        pointer._obj.value = value
        return 0
    driver = SimpleNamespace(cuInit=lambda _: init, cuDeviceGetCount=lambda p: assign(p, count), cuDriverGetVersion=lambda p: assign(p, version))
    monkeypatch.setattr("modules.runtime_profiles.ctypes.CDLL", lambda _: driver)
    assert (cuda_hardware_error() is None) is ready


def test_hidden_cuda_does_not_block_tts_download(monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "-1")
    monkeypatch.setattr(RuntimeProfile, "is_compatible", lambda self, **_: True)
    calls = []
    monkeypatch.setattr(routes, "_get_download_service", lambda _: SimpleNamespace(start=lambda **kwargs: calls.append(kwargs) or {"lifecycle": "running"}))
    with TestClient(create_app(controller=object())) as client:
        response = client.post("/api/setup/models/download", json={"profile": "cuda", "model_groups": ["tts-cpu-cuda"]})
    assert response.status_code == 200
    assert calls[0]["model_groups"] == ("tts-cpu-cuda",)


def _slow_download(model_id, result):
    Path(model_id).write_text("started")
    time.sleep(30)
    result.send(None)


def test_cancellation_terminates_in_flight_process_and_releases_lock(monkeypatch, tmp_path):
    marker = tmp_path / "started"
    monkeypatch.setattr(downloads, "_download_worker", _slow_download)
    service = downloads.ModelDownloadService(group_resolver=lambda _: (str(marker),), lock_factory=nullcontext)
    service.start(profile_id="mlx", model_groups=["tts-mlx"])
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert marker.exists()
        assert service.shutdown(timeout_seconds=3)
        assert service.status()["lifecycle"] == "cancelled"
        assert service.status()["completed_models"] == 0
    finally:
        service.shutdown(timeout_seconds=3)


def test_storage_paths_and_open_are_scoped(monkeypatch, tmp_path):
    config = SimpleNamespace(output_dir=tmp_path / "output", db_path=tmp_path / "data" / "audiobooks.db")
    opened = []
    monkeypatch.setattr(routes, "_default_folder_opener", opened.append)
    with TestClient(create_app(controller=SimpleNamespace(config=config))) as client:
        paths = client.get("/api/system/storage").json()
        assert paths["output_path"] == str(config.output_dir.resolve())
        assert client.post("/api/system/storage/output/open").status_code == 200
        assert client.post("/api/system/storage/arbitrary/open").status_code == 422
    assert opened == [config.output_dir.resolve()]


def test_deletion_invalidation_discards_in_flight_ready_status():
    from modules.web.setup_status_cache import RuntimeSetupStatusCache
    cache = RuntimeSetupStatusCache()
    calls = []
    def build(_):
        calls.append(True)
        if len(calls) == 1:
            cache.invalidate()
            return {"ready": True}
        return {"ready": False}
    assert cache.get(("unchanged-state",), build) == {"ready": False}
    assert len(calls) == 2
    assert cache.get(("unchanged-state",), build) == {"ready": False}
