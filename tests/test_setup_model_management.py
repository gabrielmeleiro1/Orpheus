"""Per-model storage operations against an isolated HF cache, never user weights."""
from pathlib import Path
from types import SimpleNamespace
import time

from fastapi.testclient import TestClient
from huggingface_hub import scan_cache_dir
import pytest

from modules.web.server import create_app
from modules.web.routes import setup as routes
from modules.app.services.model_download_service import ModelDownloadService
from scripts import setup_runtime
from scripts.download_models import resolve_model_groups


def make_repo(cache: Path, repo_id: str, commit: str = "a" * 40) -> Path:
    repo = cache / ("models--" + repo_id.replace("/", "--"))
    snapshot = repo / "snapshots" / commit
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "config.json").write_text('{"test": true}')
    (repo / "refs").mkdir(exist_ok=True)
    (repo / "refs/main").write_text(commit)
    return repo


@pytest.fixture
def storage(monkeypatch, tmp_path):
    from modules.runtime_profiles import RuntimeProfile
    monkeypatch.setattr(RuntimeProfile, "is_compatible", lambda self, **kwargs: True)
    cache = tmp_path / "models"
    cache.mkdir()
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(routes, "scan_cache_dir", lambda: scan_cache_dir(cache))
    monkeypatch.setattr(routes.hf_constants, "HF_HUB_CACHE", str(cache))
    monkeypatch.setattr(routes, "cuda_hardware_error", lambda: None)
    original_delete = routes._delete_runtime_models
    monkeypatch.setattr(routes, "_delete_runtime_models", lambda **kwargs: original_delete(cache_scanner=lambda: scan_cache_dir(cache), cache_root=cache, **kwargs))
    state_path = setup_runtime.default_state_path()
    setup_runtime.write_setup_state(state_path, profile_id="cuda", model_groups=("tts-cpu-cuda", "cleaner-cuda"), accepted_risks=())
    repos = {group: make_repo(cache, resolve_model_groups((group,))[0]) for group in ("tts-cpu-cuda", "cleaner-cuda")}
    # Deliberately share the same commit hash with the managed models.
    repos["unrelated"] = make_repo(cache, "someone/unrelated")
    return cache, state_path, repos


def test_inventory_and_deletion_preserve_other_repos_even_with_same_commit(storage):
    _, state_path, repos = storage
    with TestClient(create_app(controller=object())) as client:
        inventory = client.get("/api/setup/models").json()["models"]
        cleaner = next(item for item in inventory if item["id"] == "cleaner-cuda")
        assert cleaner["downloaded"] and cleaner["size_bytes"] > 0
        assert set(cleaner) == {"id", "downloaded", "size_bytes"}
        response = client.delete("/api/setup/models/cleaner-cuda")
        assert response.status_code == 200
        assert response.json()["deleted_model_groups"] == ["cleaner-cuda"]
        assert response.json()["freed_bytes"] > 0
        assert repos["tts-cpu-cuda"].exists()
        assert repos["unrelated"].exists()
        assert not repos["cleaner-cuda"].exists()
        assert setup_runtime.read_setup_state(state_path)["model_groups"] == ["tts-cpu-cuda"]
        assert client.delete("/api/setup/models/cleaner-cuda").json()["freed_bytes"] == 0
        assert client.delete("/api/setup/models/tts-cpu-cuda").status_code == 200
        state = setup_runtime.read_setup_state(state_path)
        assert (state["profile"], state["model_groups"], state["restart_required"]) == ("cuda", ["tts-cpu-cuda"], False)


def test_delete_then_download_again_preserves_the_active_runtime(storage, monkeypatch):
    cache, state_path, repos = storage
    service = ModelDownloadService(downloader=lambda model_id: make_repo(cache, model_id))
    monkeypatch.setattr(routes, "_get_download_service", lambda request: service)
    with TestClient(create_app(controller=object())) as client:
        assert client.delete("/api/setup/models/cleaner-cuda").status_code == 200
        selected_before = setup_runtime.read_setup_state(state_path)
        response = client.post("/api/setup/models/download", json={"profile": "cuda", "model_groups": ["cleaner-cuda"], "configure_runtime": False})
        assert response.status_code == 200
        deadline = time.monotonic() + 3
        while service.status()["lifecycle"] == "running" and time.monotonic() < deadline:
            time.sleep(.01)
        assert service.status()["lifecycle"] == "complete"
        assert service.status()["configure_runtime"] is False
        assert repos["cleaner-cuda"].exists()
        assert repos["tts-cpu-cuda"].exists()
        assert setup_runtime.read_setup_state(state_path) == selected_before


def test_deleting_unselected_model_preserves_configuration(storage):
    _, state_path, _ = storage
    setup_runtime.write_setup_state(state_path, profile_id="base", model_groups=(), accepted_risks=())
    before = state_path.read_bytes()
    with TestClient(create_app(controller=object())) as client:
        assert client.delete("/api/setup/models/cleaner-cuda").status_code == 200
    assert state_path.read_bytes() == before


def test_deletion_refuses_active_conversions(storage):
    _, state_path, repos = storage
    def busy():
        raise RuntimeError("conversion running")
    controller = SimpleNamespace(_conversion_service=SimpleNamespace(begin_storage_cleanup=busy))
    with TestClient(create_app(controller=controller)) as client:
        response = client.delete("/api/setup/models/cleaner-cuda")
    assert response.status_code == 409
    assert repos["cleaner-cuda"].exists()
    assert len(setup_runtime.read_setup_state(state_path)["model_groups"]) == 2


def test_deletion_refuses_concurrent_downloads_and_releases_conversion_guard(storage):
    _, state_path, repos = storage
    calls = []
    controller = SimpleNamespace(_conversion_service=SimpleNamespace(begin_storage_cleanup=lambda: calls.append("begin"), end_storage_cleanup=lambda: calls.append("end")))
    with TestClient(create_app(controller=controller)) as client:
        with setup_runtime.SetupLock(state_path):
            response = client.delete("/api/setup/models/cleaner-cuda")
    assert response.status_code == 409
    assert calls == ["begin", "end"]
    assert repos["cleaner-cuda"].exists()


def test_staging_failure_does_not_change_selection(storage, monkeypatch):
    _, state_path, _ = storage
    before = setup_runtime.read_setup_state(state_path)
    def failed(*args, **kwargs):
        raise PermissionError("private path")
    monkeypatch.setattr(routes, "_stage_runtime_models", failed)
    with TestClient(create_app(controller=object())) as client:
        response = client.delete("/api/setup/models/cleaner-cuda")
    assert response.status_code == 409
    assert "private path" not in response.text
    assert setup_runtime.read_setup_state(state_path) == before


def test_model_management_requires_auth_and_rejects_unknown_models(storage):
    with TestClient(create_app(controller=object(), desktop_mode=True, desktop_api_token="test-token")) as client:
        assert client.get("/api/setup/models").status_code == 401
        assert client.delete("/api/setup/models/cleaner-cuda").status_code == 401
        assert client.delete("/api/setup/models/unknown", headers={"Authorization": "Bearer test-token"}).status_code == 422


def test_damaged_managed_repo_is_visible_and_removable(storage):
    _, _, repos = storage
    (repos["cleaner-cuda"] / "refs" / "main").write_text("missing-revision")
    with TestClient(create_app(controller=object())) as client:
        item = next(item for item in client.get("/api/setup/models").json()["models"] if item["id"] == "cleaner-cuda")
        assert item["downloaded"] and item["damaged"]
        response = client.delete("/api/setup/models/cleaner-cuda")
    assert response.status_code == 200
    assert not repos["cleaner-cuda"].exists()
    assert repos["unrelated"].exists()


def test_partial_delete_reports_actual_bytes_and_clears_selection(storage, monkeypatch):
    _, state_path, repos = storage
    victim = repos["cleaner-cuda"] / "extra.bin"
    victim.write_bytes(b"1234567")
    original_remove = routes.shutil.rmtree
    def partial(path, **kwargs):
        if not path.name.startswith(".orpheus-delete-"):
            return original_remove(path, **kwargs)
        (path / victim.name).unlink()
        raise PermissionError("locked")
    monkeypatch.setattr(routes.shutil, "rmtree", partial)
    with TestClient(create_app(controller=object())) as client:
        response = client.delete("/api/setup/models/cleaner-cuda")
    assert response.status_code == 200
    result = response.json()
    assert result["freed_bytes"] == 7
    assert result["deleted_model_groups"] == []
    assert result["residual_model_groups"] == ["cleaner-cuda"]
    assert "Couldn't delete some files" in result["message"]
    assert setup_runtime.read_setup_state(state_path)["model_groups"] == ["tts-cpu-cuda"]


def test_deleting_voice_model_leaves_runtime_not_ready(storage, monkeypatch):
    _, state_path, _ = storage
    setup_runtime.write_setup_state(state_path, profile_id="cuda", model_groups=("tts-cpu-cuda", "cleaner-cuda"), accepted_risks=(), restart_required=False)
    monkeypatch.setattr(routes, "_check_selected_profile_packages", lambda _: {"ready": True, "missing": [], "incompatible": []})
    monkeypatch.setattr(setup_runtime, "_probe_profile", lambda groups, _: {"available": True, "checks": [{"id": g, "available": True} for g in groups]})
    monkeypatch.setattr(routes, "_detect_installed_runtime", lambda *a, **k: None)
    check_assets = routes._check_model_group_assets
    monkeypatch.setattr(routes, "_check_model_group_assets", lambda groups, **kwargs: check_assets(groups, cache_scanner=routes.scan_cache_dir, **kwargs))
    with TestClient(create_app(controller=object())) as client:
        assert client.delete("/api/setup/models/tts-cpu-cuda").status_code == 200
        status = client.get("/api/setup/status").json()
    assert status["ready"] is False
    assert status["state"] == "unavailable"
    assert "Kokoro voice model is missing" in status["state_error"]
