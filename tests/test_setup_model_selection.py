"""Model selection and precision survive setup, restart and model removal."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from modules.app.config import AppConfig
from modules.web.routes import setup as routes
from modules.web.server import create_app
from scripts import setup_runtime
from scripts.download_models import MODEL_GROUPS, MODEL_REVISIONS, resolve_model_groups


@pytest.mark.parametrize("quantization", ["4bit", "8bit", "fp16"])
def test_selection_round_trip_and_delete_preserves_precision(tmp_path, monkeypatch, quantization):
    path = tmp_path / "runtime-setup.json"
    monkeypatch.setattr(setup_runtime, "default_state_path", lambda: path)
    monkeypatch.setattr(routes, "_check_selected_profile_packages", lambda _: {"ready": True})
    monkeypatch.setattr(setup_runtime, "_probe_profile", lambda groups, _: {"available": True, "checks": []})
    monkeypatch.setattr(routes, "_check_model_group_assets", lambda groups, **_: {g: {"ready": True, "reason": None} for g in groups})
    monkeypatch.setattr(routes, "build_setup_status_response", lambda _: setup_runtime.read_setup_state(path))
    monkeypatch.setattr(routes, "_delete_runtime_models", lambda **_: (["cleaner-cuda-large"], 100))
    monkeypatch.setattr(routes, "get_managed_models", lambda: {"models": []})
    monkeypatch.setattr(routes, "scan_cache_dir", lambda: SimpleNamespace(repos=[]))
    monkeypatch.setattr(routes.hf_constants, "HF_HUB_CACHE", str(tmp_path / "models"))
    config = AppConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "output", temp_dir=tmp_path / "temp")
    with TestClient(create_app(controller=SimpleNamespace(config=config))) as client:
        result = client.post("/api/setup/adopt", json={
            "profile": "cuda", "model_groups": ["tts-cpu-cuda", "cleaner-cuda-large"],
            "cleaner_cuda_quantization": quantization,
        })
        assert result.status_code == 200
        assert result.json()["cleaner_cuda_quantization"] == quantization
        assert config.cleaner_model_name == "Qwen/Qwen2.5-3B-Instruct"
        assert config.cleaner_cuda_quantization == quantization
        assert config.cleaner_backend == "cuda"
        restarted = AppConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "output", temp_dir=tmp_path / "temp")
        restarted.apply_setup_preferences()
        assert restarted.cleaner_model_name == config.cleaner_model_name
        assert restarted.cleaner_cuda_quantization == quantization
        assert client.delete("/api/setup/models/cleaner-cuda-large").status_code == 200
        saved = setup_runtime.read_setup_state(path)
        assert saved["model_groups"] == ["tts-cpu-cuda"]
        assert saved["cleaner_cuda_quantization"] == quantization
        assert config.cleaner_backend == "basic"


def test_catalog_downloads_only_requested_model_and_pins_every_revision():
    assert resolve_model_groups(["cleaner-cuda-large"]) == ("Qwen/Qwen2.5-3B-Instruct",)
    for models in MODEL_GROUPS.values():
        assert all(len(MODEL_REVISIONS[model]) == 40 for model in models)


def test_setup_rejects_multiple_active_cleaners_before_probing(monkeypatch):
    def unexpected_probe(*args, **kwargs):
        pytest.fail("An ambiguous selection must be rejected before probing")
    monkeypatch.setattr(setup_runtime, "_probe_profile", unexpected_probe)
    with TestClient(create_app(controller=object())) as client:
        response = client.post("/api/setup/adopt", json={"profile": "cuda", "model_groups": ["cleaner-cuda", "cleaner-cuda-large"]})
        assert response.status_code == 422
        assert "Select one local cleaner" in response.text


def test_invalid_precision_rejected_before_install(monkeypatch):
    calls = []
    monkeypatch.setattr(setup_runtime, "build_install_plan", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(setup_runtime.SetupValidationError):
        setup_runtime.run_setup(profile_id="cuda", cleaner_cuda_quantization="int2")
    assert calls == []
    with TestClient(create_app(controller=object())) as client:
        for endpoint in ("/api/setup/adopt", "/api/setup/models/download"):
            response = client.post(endpoint, json={"profile": "cuda", "model_groups": ["cleaner-cuda"], "cleaner_cuda_quantization": "int2"})
            assert response.status_code == 422
