"""Per-model storage operations against an isolated HF cache, never user weights."""
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
import json
import os
import shutil
import subprocess
import sys
import time

from fastapi.testclient import TestClient
from huggingface_hub import scan_cache_dir
import pytest

from modules.runtime_setup import hub_cache
from modules.app.services import model_storage_service
from modules.web.server import create_app
from modules.web.routes import setup as routes
from modules.app.services.model_download_service import ModelDownloadService
from scripts import setup_runtime
from scripts.download_models import resolve_model_groups

try:
    from huggingface_hub.utils import _shared_blobs
except ImportError:  # huggingface_hub releases before the shared blob store
    _shared_blobs = None

requires_shared_blob_store = pytest.mark.skipif(
    _shared_blobs is None, reason="huggingface_hub without the shared blob store"
)


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
    monkeypatch.setattr(hub_cache, "scan_cache_dir", lambda: scan_cache_dir(cache))
    monkeypatch.setattr(hub_cache.hf_constants, "HF_HUB_CACHE", str(cache))
    monkeypatch.setattr(routes, "cuda_hardware_error", lambda: None)
    original_delete = model_storage_service._delete_runtime_models
    monkeypatch.setattr(model_storage_service, "_delete_runtime_models", lambda **kwargs: original_delete(cache_scanner=lambda: scan_cache_dir(cache), cache_root=cache, **kwargs))
    state_path = setup_runtime.default_state_path()
    setup_runtime.write_setup_state(state_path, profile_id="cuda", model_groups=("tts-cpu-cuda", "cleaner-cuda"), accepted_risks=())
    repos = {group: make_repo(cache, resolve_model_groups((group,))[0]) for group in ("tts-cpu-cuda", "cleaner-cuda")}
    # Deliberately share the same commit hash with the managed models.
    repos["unrelated"] = make_repo(cache, "someone/unrelated")
    return cache, state_path, repos


@pytest.mark.parametrize("backend", ["cuda", "mlx"])
@pytest.mark.parametrize("deletion", ["group", "runtime", "external"])
def test_deleted_cleaner_cannot_be_reloaded_from_process_cache(storage, monkeypatch, backend, deletion):
    from modules.errors import TTSInstallationError
    from modules.tts import cleaner_cuda, cleaner_mlx

    _, _, repos = storage
    module = cleaner_cuda if backend == "cuda" else cleaner_mlx
    strategy = module.CleanerCUDAStrategy() if backend == "cuda" else module.CleanerMLXStrategy()
    monkeypatch.setattr(module, "_cleaner_model_cache", {})
    monkeypatch.setattr(cleaner_cuda, "torch", None)
    monkeypatch.setattr(cleaner_mlx, "mx", None)
    if backend == "cuda":
        monkeypatch.setattr(module, "ensure_cuda_runtime", lambda: (SimpleNamespace(),))
    else:
        monkeypatch.setattr(module, "ensure_mlx_lm_runtime", lambda: (None,) * 4)

    def installed(name):
        if not repos["cleaner-cuda"].exists():
            raise TTSInstallationError("Model is not installed. Reinstall the model from Setup.")
        return str(repos["cleaner-cuda"])

    monkeypatch.setattr(module, "installed_model_path", installed)
    if backend == "cuda":
        def load_cuda(cleaner, torch_module):
            installed(cleaner.config.model_name)
            return object(), object()
        monkeypatch.setattr(module, "_load_cuda_model", load_cuda)
    else:
        monkeypatch.setattr(module, "load", lambda *args, **kwargs: (object(), object()))
    cleaner = SimpleNamespace(
        config=SimpleNamespace(model_name="local-cleaner", cache_model=True,
                               cuda_device="cuda", cuda_quantization="4bit"),
        _loaded=False, _model=None, _tokenizer=None,
        _manager=SimpleNamespace(ensure_can_load=lambda name: None),
    )
    key = module._cuda_cache_key(cleaner) if backend == "cuda" else cleaner.config.model_name
    module.set_cached_cleaner(key, object(), object())
    strategy.load(cleaner)
    strategy.unload(cleaner)
    if deletion == "external":
        shutil.rmtree(repos["cleaner-cuda"])
    else:
        endpoint = "/api/setup/runtime" if deletion == "runtime" else "/api/setup/models/cleaner-cuda"
        with TestClient(create_app(controller=object())) as client:
            assert client.delete(endpoint).status_code == 200
    assert not repos["cleaner-cuda"].exists()
    with pytest.raises(TTSInstallationError, match="not installed"):
        strategy.load(cleaner)
    assert cleaner._loaded is False


@pytest.mark.parametrize("deletion", ["group", "runtime"])
def test_cleaner_deletion_discards_both_backend_caches(storage, monkeypatch, deletion):
    from modules.tts import cleaner_cuda, cleaner_mlx

    for module in (cleaner_cuda, cleaner_mlx):
        monkeypatch.setattr(module, "_cleaner_model_cache", {"cleaner": (object(), object())})
    monkeypatch.setattr(cleaner_cuda, "torch", None)
    monkeypatch.setattr(cleaner_mlx, "mx", None)
    endpoint = "/api/setup/runtime" if deletion == "runtime" else "/api/setup/models/cleaner-cuda"
    with TestClient(create_app(controller=object())) as client:
        assert client.delete(endpoint).status_code == 200
    assert cleaner_cuda.get_cached_cleaner("cleaner") is None
    assert cleaner_mlx.get_cached_cleaner("cleaner") is None


def test_uninstall_reset_survives_backend_restart_before_client_delivery(storage, monkeypatch):
    cache, state_path, _ = storage
    monkeypatch.setattr(routes, "_detect_installed_runtime", lambda *args, **kwargs: None)
    with TestClient(create_app(controller=object())) as client:
        response = client.delete("/api/setup/runtime")
        assert response.status_code == 200
        reset_id = response.json()["setup_reset_id"]
    assert not state_path.exists()
    assert not list(cache.glob(".orpheus-delete-*.json"))
    script = """
import json
from fastapi.testclient import TestClient
from modules.web.routes import setup
from modules.web.server import create_app
setup._detect_installed_runtime = lambda *args, **kwargs: None
with TestClient(create_app(controller=object())) as client:
    print(json.dumps(client.get('/api/setup/status').json()), flush=True)
"""
    restarted = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=20,
        env={**os.environ, "HF_HUB_CACHE": str(cache), "HF_HOME": str(cache.parent / "hf-home")},
    )
    assert restarted.returncode == 0, restarted.stderr
    status = json.loads(restarted.stdout.strip().splitlines()[-1])
    assert status["configured"] is False
    assert status.get("setup_reset_id") == reset_id


@pytest.mark.parametrize("deletion", ["group", "runtime"])
def test_model_staging_rename_is_synced_before_setup_commit(storage, monkeypatch, deletion):
    cache, state_path, _ = storage
    events = []
    rename = Path.rename
    unlink = Path.unlink
    sync = model_storage_service.sync_directory
    write = setup_runtime.write_setup_state

    def move(path, target):
        result = rename(path, target)
        if path.parent == cache and path.name.startswith("models--"):
            events.append("rename")
        return result

    def sync_parent(path):
        sync(path)
        if path == cache:
            events.append("sync")

    def commit(*args, **kwargs):
        events.append("commit")
        return write(*args, **kwargs)

    def remove(path, *args, **kwargs):
        if path == state_path:
            events.append("commit")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "rename", move)
    monkeypatch.setattr(Path, "unlink", remove)
    monkeypatch.setattr(model_storage_service, "sync_directory", sync_parent)
    monkeypatch.setattr(routes.setup_runtime, "write_setup_state", commit)
    endpoint = "/api/setup/runtime" if deletion == "runtime" else "/api/setup/models/cleaner-cuda"
    with TestClient(create_app(controller=object())) as client:
        assert client.delete(endpoint).status_code == 200
    before_commit = events[:events.index("commit")]
    assert "rename" in before_commit
    assert before_commit[-1] == "sync", events
    assert before_commit.count("sync") == before_commit.count("rename"), events


@pytest.mark.parametrize("uninstall", [False, True])
def test_recovery_keeps_journal_when_committed_stage_is_missing(storage, uninstall):
    cache, state_path, repos = storage
    stage = cache / (".orpheus-delete-" + "b" * 32)
    manifest = stage.with_suffix(".json")
    previous_id = setup_runtime.read_setup_state(state_path)["installation_id"]
    setup_runtime._atomic_write_json(manifest, {
        "original": repos["cleaner-cuda"].name, "staged": stage.name,
        "settings_committed": True, "installation_id": "c" * 32,
        "previous_installation_id": previous_id,
        **({"uninstall_runtime": True} if uninstall else {}),
    })
    completed = []
    with setup_runtime.SetupLock(state_path):
        deleted, freed = model_storage_service._recover_staged_models(
            cache_root=cache, on_uninstall_complete=completed.append,
        )
    assert manifest.exists(), "Missing staged repository must retain its deletion journal"
    assert repos["cleaner-cuda"].exists()
    assert (deleted, freed) == ([], 0)
    assert completed == []
    assert state_path.exists()


def test_inventory_and_deletion_preserve_other_repos_even_with_same_commit(storage):
    _, state_path, repos = storage
    with TestClient(create_app(controller=object())) as client:
        inventory = client.get("/api/setup/models").json()["models"]
        cleaner = next(item for item in inventory if item["id"] == "cleaner-cuda")
        assert cleaner["downloaded"] and cleaner["size_bytes"] > 0
        # No filesystem paths; deletability is an additive fact for Settings.
        assert set(cleaner) == {"id", "downloaded", "size_bytes", "deletable", "delete_blocked"}
        assert cleaner["deletable"] is True and cleaner["delete_blocked"] is None
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
    monkeypatch.setattr(model_storage_service, "_stage_runtime_models", failed)
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
    original_remove = model_storage_service.shutil.rmtree
    def partial(path, **kwargs):
        if not path.name.startswith(".orpheus-delete-"):
            return original_remove(path, **kwargs)
        (path / victim.name).unlink()
        raise PermissionError("locked")
    monkeypatch.setattr(model_storage_service.shutil, "rmtree", partial)
    with TestClient(create_app(controller=object())) as client:
        response = client.delete("/api/setup/models/cleaner-cuda")
    assert response.status_code == 200
    result = response.json()
    assert result["freed_bytes"] == 7
    assert result["deleted_model_groups"] == []
    assert result["residual_model_groups"] == ["cleaner-cuda"]
    assert "Couldn't delete some files" in result["message"]
    assert setup_runtime.read_setup_state(state_path)["model_groups"] == ["tts-cpu-cuda"]


@requires_shared_blob_store
def test_deleting_voice_model_leaves_runtime_not_ready(storage, monkeypatch):
    _, state_path, _ = storage
    setup_runtime.write_setup_state(state_path, profile_id="cuda", model_groups=("tts-cpu-cuda", "cleaner-cuda"), accepted_risks=(), restart_required=False)
    monkeypatch.setattr(routes, "_check_selected_profile_packages", lambda _: {"ready": True, "missing": [], "incompatible": []})
    monkeypatch.setattr("modules.runtime_setup.status._probe_profile", lambda groups, _, **kwargs: {"available": True, "checks": [{"id": g, "available": True} for g in groups]})
    monkeypatch.setattr(routes, "_detect_installed_runtime", lambda *a, **k: None)
    check_assets = hub_cache._check_model_group_assets
    monkeypatch.setattr(hub_cache, "_check_model_group_assets", lambda groups, **kwargs: check_assets(groups, cache_scanner=hub_cache.scan_cache_dir, **kwargs))
    with TestClient(create_app(controller=object())) as client:
        assert client.delete("/api/setup/models/tts-cpu-cuda").status_code == 200
        status = client.get("/api/setup/status").json()
    assert status["ready"] is False
    assert status["state"] == "unavailable"
    assert "Kokoro voice model is missing" in status["state_error"]


def _link_shared_blob(cache: Path, repo: Path, xet_hash: str, payload: bytes) -> Path:
    store_file = cache / "blobs" / xet_hash[:2] / xet_hash
    store_file.parent.mkdir(parents=True, exist_ok=True)
    store_file.write_bytes(payload)
    (cache / "blobs" / _shared_blobs.SHARED_BLOBS_MARKER_NAME).write_text("1\n")
    (repo / "blobs").mkdir(exist_ok=True)
    etag = f"etag-{xet_hash[:8]}"
    (repo / "blobs" / etag).symlink_to(Path("../../blobs") / xet_hash[:2] / xet_hash)
    with store_file.with_name(f"{xet_hash}.refs").open("a") as manifest:
        manifest.write(f"{repo.name}/blobs/{etag}\n")
    snapshot = next((repo / "snapshots").iterdir())
    (snapshot / f"{xet_hash[:8]}.safetensors").symlink_to(Path("../../blobs") / etag)
    return store_file


@requires_shared_blob_store
def test_models_in_the_shared_blob_store_report_their_size_and_free_it_on_delete(storage):
    cache, _, repos = storage
    own = _link_shared_blob(cache, repos["cleaner-cuda"], "c1" + "0" * 62, b"x" * 4096)
    shared = _link_shared_blob(cache, repos["cleaner-cuda"], "d2" + "0" * 62, b"y" * 2048)
    _link_shared_blob(cache, repos["unrelated"], shared.name, b"y" * 2048)
    with TestClient(create_app(controller=object())) as client:
        cleaner = next(item for item in client.get("/api/setup/models").json()["models"] if item["id"] == "cleaner-cuda")
        assert cleaner["size_bytes"] >= 4096 + 2048
        response = client.delete("/api/setup/models/cleaner-cuda")
    assert response.status_code == 200
    assert response.json()["freed_bytes"] >= 4096
    assert not own.exists()
    assert not own.with_name(own.name + ".refs").exists()
    assert shared.exists(), "a payload another repo still links to must survive"


def test_staging_serializes_windows_shared_blob_paths_with_slashes(storage, monkeypatch):
    cache, _, _ = storage
    relative = PureWindowsPath("blobs", "c1", "c1" + "0" * 62)
    payload = PureWindowsPath(str(cache.resolve())) / relative
    monkeypatch.setattr(hub_cache, "_shared_store_files", lambda _: {payload})
    model_id = resolve_model_groups(("cleaner-cuda",))[0]

    with model_storage_service._stage_runtime_models("cleaner-cuda", installation_id="b" * 32) as staged:
        path = staged[model_id].with_name(staged[model_id].name + ".json")
        data = json.loads(path.read_text())
        assert data["shared_files"] == [relative.as_posix()]


@requires_shared_blob_store
def test_recovery_collects_windows_manifest_payloads_after_repository_removal(storage):
    cache, _, repos = storage
    repo = repos["cleaner-cuda"]
    payload = _link_shared_blob(cache, repo, "c2" + "0" * 62, b"weights")
    shared = _link_shared_blob(cache, repo, "d3" + "0" * 62, b"keep")
    _link_shared_blob(cache, repos["unrelated"], shared.name, b"keep")
    shutil.rmtree(repo)
    stage_name = ".orpheus-delete-" + "b" * 32
    manifest = cache / (stage_name + ".json")
    setup_runtime._atomic_write_json(manifest, {
        "original": repo.name,
        "staged": stage_name,
        "settings_committed": True,
        "installation_id": "b" * 32,
        "shared_files": [str(PureWindowsPath(path.relative_to(cache))) for path in (payload, shared)],
    })

    with setup_runtime.SetupLock(setup_runtime.default_state_path()):
        deleted, freed = model_storage_service._recover_staged_models(cache_root=cache)

    assert deleted == ["cleaner-cuda"]
    assert freed == len(b"weights")
    assert not payload.exists()
    assert shared.read_bytes() == b"keep"
    assert not manifest.exists()


@pytest.mark.parametrize("store_kind", ["unmarked", "symlink", "external-prefix"])
@requires_shared_blob_store
def test_delete_preserves_payloads_outside_an_owned_shared_store(storage, tmp_path, store_kind):
    cache, _, repos = storage
    payload = _link_shared_blob(cache, repos["cleaner-cuda"], "e3" + "0" * 62, b"keep")
    if store_kind == "unmarked":
        (cache / "blobs" / _shared_blobs.SHARED_BLOBS_MARKER_NAME).unlink()
    else:
        directory = cache / "blobs" if store_kind == "symlink" else payload.parent
        external = tmp_path / "external"
        directory.rename(external)
        directory.symlink_to(external, target_is_directory=True)
    expected_bytes = hub_cache._repo_file_bytes(repos["cleaner-cuda"])
    with TestClient(create_app(controller=object())) as client:
        model = next(item for item in client.get("/api/setup/models").json()["models"] if item["id"] == "cleaner-cuda")
        assert model["size_bytes"] == expected_bytes
        result = client.delete("/api/setup/models/cleaner-cuda").json()
    assert payload.read_bytes() == b"keep"
    assert result["freed_bytes"] == expected_bytes


@pytest.mark.parametrize("failure", ["missing-marker-helper", "missing-sweep-helper", "marker-error", "sweep-error", "unreadable-manifest"])
@requires_shared_blob_store
def test_delete_keeps_shared_payload_when_hub_cannot_validate_ownership(storage, monkeypatch, failure):
    cache, _, repos = storage
    payload = _link_shared_blob(cache, repos["cleaner-cuda"], "e4" + "0" * 62, b"keep")
    expected_bytes = hub_cache._repo_file_bytes(repos["cleaner-cuda"])
    if failure.startswith("missing-"):
        helper = "is_shared_blobs_dir" if failure == "missing-marker-helper" else "sweep_shared_blob"
        monkeypatch.delattr(_shared_blobs, helper)
    elif failure == "unreadable-manifest":
        original_read = Path.read_text
        def read(path, *args, **kwargs):
            if path == payload.with_name(payload.name + ".refs"):
                raise PermissionError("locked")
            return original_read(path, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", read)
    else:
        def fail(*args, **kwargs):
            raise RuntimeError("unavailable")
        helper = "is_shared_blobs_dir" if failure == "marker-error" else "sweep_shared_blob"
        monkeypatch.setattr(_shared_blobs, helper, fail)
    model_id = resolve_model_groups(("cleaner-cuda",))[0]
    _, freed = model_storage_service._delete_runtime_models(model_paths={model_id: repos["cleaner-cuda"]})
    assert payload.read_bytes() == b"keep"
    assert freed == expected_bytes


@requires_shared_blob_store
def test_shared_blob_sweep_preserves_a_reference_published_during_deletion(storage, monkeypatch):
    cache, _, repos = storage
    payload = _link_shared_blob(cache, repos["cleaner-cuda"], "e5" + "0" * 62, b"keep")
    expected_bytes = hub_cache._repo_file_bytes(repos["cleaner-cuda"])
    sweep = _shared_blobs.sweep_shared_blob
    calls = []
    def publish_then_sweep(store_path, *, cache_dir):
        calls.append(store_path)
        assert not repos["cleaner-cuda"].exists()
        _link_shared_blob(cache, repos["unrelated"], payload.name, b"keep")
        return sweep(store_path, cache_dir=cache_dir)
    monkeypatch.setattr(_shared_blobs, "sweep_shared_blob", publish_then_sweep)
    with TestClient(create_app(controller=object())) as client:
        result = client.delete("/api/setup/models/cleaner-cuda").json()
    assert calls == [payload]
    assert payload.read_bytes() == b"keep"
    assert result["freed_bytes"] == expected_bytes


@requires_shared_blob_store
def test_delete_reports_removed_payload_bytes_when_reference_cleanup_is_locked(storage, monkeypatch):
    cache, _, repos = storage
    payload = _link_shared_blob(cache, repos["cleaner-cuda"], "e6" + "0" * 62, b"weights")
    expected_bytes = hub_cache._repo_file_bytes(repos["cleaner-cuda"]) + len(b"weights")
    unlink = Path.unlink
    def remove(path, **kwargs):
        if path == payload.with_name(payload.name + ".refs"):
            raise PermissionError("locked")
        return unlink(path, **kwargs)
    monkeypatch.setattr(Path, "unlink", remove)
    with TestClient(create_app(controller=object())) as client:
        result = client.delete("/api/setup/models/cleaner-cuda").json()
    assert not payload.exists()
    assert result["freed_bytes"] == expected_bytes


@pytest.mark.parametrize("unreadable_stage", [False, True])
@requires_shared_blob_store
def test_delete_keeps_payloads_referenced_by_another_staged_repo(storage, monkeypatch, unreadable_stage):
    cache, _, repos = storage
    repo = repos["cleaner-cuda"]
    payload = _link_shared_blob(cache, repo, "f7" + "0" * 62, b"weights")
    other_id = resolve_model_groups(("cleaner-cuda-large",))[0]
    other = make_repo(cache, other_id)
    _link_shared_blob(cache, other, payload.name, b"weights")
    expected_bytes = hub_cache._repo_file_bytes(repo)
    with model_storage_service._stage_runtime_models("cleaner-cuda-large", installation_id="b" * 32) as staged:
        if unreadable_stage:
            shared_files = hub_cache._shared_store_files
            monkeypatch.setattr(hub_cache, "_shared_store_files", lambda path: set() if path == staged[other_id] else shared_files(path))
        model_id = resolve_model_groups(("cleaner-cuda",))[0]
        _, freed = model_storage_service._delete_runtime_models(model_paths={model_id: repo})
        assert payload.read_bytes() == b"weights"
        assert freed == expected_bytes
    assert (other / "blobs" / f"etag-{payload.name[:8]}").read_bytes() == b"weights"


@requires_shared_blob_store
def test_inventory_counts_shared_payload_once_when_a_staged_repo_is_recreated(storage):
    cache, state_path, repos = storage
    model_id = resolve_model_groups(("cleaner-cuda",))[0]
    payload = _link_shared_blob(cache, repos["cleaner-cuda"], "f8" + "0" * 62, b"weights")
    with setup_runtime.SetupLock(state_path):
        with model_storage_service._stage_runtime_models("cleaner-cuda", installation_id="b" * 32) as staged:
            recreated = make_repo(cache, model_id)
            _link_shared_blob(cache, recreated, payload.name, b"weights")
            expected_bytes = hub_cache._repo_file_bytes(staged[model_id]) + hub_cache._repo_file_bytes(recreated) + len(b"weights")
            item = next(item for item in routes.get_managed_models()["models"] if item["id"] == "cleaner-cuda")
            assert item["downloaded"] and item["damaged"]
            assert item["size_bytes"] == expected_bytes


def test_inventory_reports_unreadable_staging_metadata(storage, monkeypatch):
    _, state_path, _ = storage
    model_id = resolve_model_groups(("cleaner-cuda",))[0]
    read = Path.read_text
    with setup_runtime.SetupLock(state_path):
        with model_storage_service._stage_runtime_models("cleaner-cuda", installation_id="b" * 32) as staged:
            manifest = staged[model_id].with_name(staged[model_id].name + ".json")
            def locked(path, *args, **kwargs):
                if path == manifest:
                    raise PermissionError("private path")
                return read(path, *args, **kwargs)
            with monkeypatch.context() as patch:
                patch.setattr(Path, "read_text", locked)
                with TestClient(create_app(controller=object())) as client:
                    response = client.get("/api/setup/models")
                assert response.status_code == 503
                assert response.json()["detail"]["error"] == "cache_unavailable"
                assert "private path" not in response.text
                assert staged[model_id].exists()


def _interrupt_cleaner_deletion(cache: Path, position: str) -> None:
    script = """
import os
from pathlib import Path
import sys
from fastapi.testclient import TestClient
from modules.runtime_profiles import RuntimeProfile
from modules.web.routes import setup as routes
from modules.app.services import model_storage_service
from modules.web.server import create_app
from scripts import setup_runtime

RuntimeProfile.is_compatible = lambda self, **kwargs: True
position = sys.argv[1]
rename = Path.rename
def stage(path, target):
    if Path(target).name.startswith('.orpheus-delete-'):
        if position == 'before-rename':
            os._exit(73)
        result = rename(path, target)
        if position == 'after-rename':
            os._exit(73)
        return result
    return rename(path, target)
Path.rename = stage
atomic_write = setup_runtime._atomic_write_json
def replace(path, payload):
    atomic_write(path, payload)
    if Path(path) == setup_runtime.default_state_path() and position == 'after-settings-replace':
        os._exit(73)
routes.setup_runtime._atomic_write_json = replace
write = setup_runtime.write_setup_state
def save(*args, **kwargs):
    result = write(*args, **kwargs)
    if position == 'after-settings':
        os._exit(73)
    return result
routes.setup_runtime.write_setup_state = save
remove = model_storage_service.shutil.rmtree
def delete(path, **kwargs):
    if Path(path).name.startswith('.orpheus-delete-') and position == 'during-delete':
        (Path(path) / 'partial.bin').unlink()
        os._exit(73)
    return remove(path, **kwargs)
model_storage_service.shutil.rmtree = delete
with TestClient(create_app(controller=object())) as client:
    client.delete('/api/setup/models/cleaner-cuda')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, position],
        env={**os.environ, "HF_HUB_CACHE": str(cache), "HF_HOME": str(cache.parent / "hf-home")},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 73, result.stderr


@pytest.mark.parametrize("position", ["before-rename", "after-rename", "after-settings-replace", "after-settings", "during-delete"])
@requires_shared_blob_store
def test_inventory_recovers_interrupted_cleaner_deletion(storage, position):
    cache, state_path, repos = storage
    before = state_path.read_bytes()
    repo = repos["cleaner-cuda"]
    (repo / "partial.bin").write_bytes(b"payload")
    payload = _link_shared_blob(cache, repo, "f6" + "0" * 62, b"weights")
    _interrupt_cleaner_deletion(cache, position)

    manifests = list(cache.glob(".orpheus-delete-*.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text())
    assert manifest["original"] == repo.name
    assert manifest["staged"] == manifests[0].stem
    assert manifest["settings_committed"] is (position in {"after-settings", "during-delete"})
    with TestClient(create_app(controller=object())) as client:
        item = next(item for item in client.get("/api/setup/models").json()["models"] if item["id"] == "cleaner-cuda")
    committed = position in {"after-settings-replace", "after-settings", "during-delete"}
    assert item["downloaded"] is not committed
    assert repo.exists() is not committed
    assert not list(cache.glob(".orpheus-delete-*"))
    if committed:
        assert setup_runtime.read_setup_state(state_path)["model_groups"] == ["tts-cpu-cuda"]
        assert not payload.exists()
    else:
        assert state_path.read_bytes() == before
        assert (repo / "partial.bin").read_bytes() == b"payload"
        assert payload.read_bytes() == b"weights"


@pytest.mark.parametrize("position", ["after-rename", "after-settings"])
def test_inventory_shows_staged_models_when_recovery_is_locked(storage, monkeypatch, position):
    cache, _, repos = storage
    _interrupt_cleaner_deletion(cache, position)
    manifests = list(cache.glob(".orpheus-delete-*.json"))
    assert len(manifests) == 1
    stage = cache / manifests[0].stem
    rename = Path.rename
    remove = model_storage_service.shutil.rmtree
    def locked_rename(path, target):
        if path == stage:
            raise PermissionError("locked")
        return rename(path, target)
    def locked_remove(path, **kwargs):
        if path == stage:
            raise PermissionError("locked")
        return remove(path, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", locked_rename)
        patch.setattr(model_storage_service.shutil, "rmtree", locked_remove)
        with TestClient(create_app(controller=object())) as client:
            response = client.get("/api/setup/models")
            assert response.status_code == 200
            item = next(item for item in response.json()["models"] if item["id"] == "cleaner-cuda")
            assert item["downloaded"] and item["damaged"] and item["size_bytes"] > 0
    with TestClient(create_app(controller=object())) as client:
        item = next(item for item in client.get("/api/setup/models").json()["models"] if item["id"] == "cleaner-cuda")
    assert item["downloaded"] is (position == "after-rename")
    assert repos["cleaner-cuda"].exists() is (position == "after-rename")
    assert not list(cache.glob(".orpheus-delete-*"))


@pytest.mark.parametrize("residual", [False, True])
def test_runtime_reset_restores_live_defaults_only_after_complete_removal(storage, monkeypatch, tmp_path, residual):
    from modules.app.config import AppConfig

    _, state_path, _ = storage
    config = AppConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "output", temp_dir=tmp_path / "temp",
                       cleaner_backend="api", cleaner_model_name="custom-model", cleaner_cuda_quantization="fp16")
    monkeypatch.setattr(routes, "_detect_installed_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr("modules.runtime_setup.status._probe_profile", lambda *args, **kwargs: {"available": True, "checks": []})
    baseline = config.setup_preferences()
    config.apply_setup_preferences(state=setup_runtime.read_setup_state(state_path))
    if residual:
        monkeypatch.setattr(model_storage_service.shutil, "rmtree", lambda path: None)
    app = create_app(controller=SimpleNamespace(config=config))
    with TestClient(app) as client:
        response = client.delete("/api/setup/runtime")
        assert response.status_code == 200
        status = client.get("/api/setup/status").json()
    if residual:
        assert config.setup_preferences().cleaner_backend == "cuda"
        assert not status.get("setup_reset_id")
        assert not response.json().get("setup_reset_id")
    else:
        assert config.setup_preferences() == baseline
        assert status["setup_reset_id"]
        assert response.json()["setup_reset_id"] == status["setup_reset_id"]


def test_ambiguous_legacy_deletion_is_preserved_and_blocks_state_replacement(storage):
    cache, state_path, repos = storage
    stage = cache / (".orpheus-delete-" + "b" * 32)
    original = repos["cleaner-cuda"]
    original.rename(stage)
    manifest = stage.with_suffix(".json")
    setup_runtime._atomic_write_json(manifest, {
        "original": original.name, "staged": stage.name,
        "settings_committed": False, "installation_id": "c" * 32,
    })
    before = state_path.read_bytes()
    model_storage_service._recover_staged_models(cache_root=cache)
    assert stage.exists()
    assert manifest.exists()
    assert not original.exists()
    with pytest.raises(setup_runtime.SetupExecutionError, match="deletion"):
        setup_runtime.write_setup_state(state_path, profile_id="base", model_groups=(), accepted_risks=())
    assert state_path.read_bytes() == before


def test_deletion_commit_is_recorded_before_state_writer_returns(storage):
    _, state_path, _ = storage
    installation_id = "c" * 32
    with model_storage_service._stage_runtime_models("cleaner-cuda", installation_id=installation_id) as staged:
        setup_runtime.write_setup_state(state_path, profile_id="cuda", model_groups=("tts-cpu-cuda",),
                                        accepted_risks=(), installation_id=installation_id)
        for path in staged.values():
            manifest = json.loads(path.with_suffix(".json").read_text())
            assert manifest["settings_committed"] is True


def test_unresolved_deletion_blocks_other_group_settings_changes(storage, monkeypatch):
    cache, state_path, _ = storage
    stage = cache / (".orpheus-delete-" + "b" * 32)
    stage.mkdir()
    manifest = stage.with_suffix(".json")
    setup_runtime._atomic_write_json(manifest, {
        "original": "models--" + resolve_model_groups(("cleaner-cuda-large",))[0].replace("/", "--"),
        "staged": stage.name, "settings_committed": True, "installation_id": "c" * 32,
    })
    monkeypatch.setattr(model_storage_service.shutil, "rmtree", lambda path: None)
    before = state_path.read_bytes()
    with TestClient(create_app(controller=object())) as client:
        response = client.delete("/api/setup/models/cleaner-cuda")
    assert response.status_code in {409, 503}
    assert state_path.read_bytes() == before
    assert manifest.exists()


@pytest.mark.parametrize("position", ["after-settings-replace", "after-settings"])
def test_adoption_cannot_erase_unresolved_deletion_evidence(storage, monkeypatch, position):
    cache, state_path, _ = storage
    _interrupt_cleaner_deletion(cache, position)
    before = state_path.read_bytes()
    remove = model_storage_service.shutil.rmtree
    monkeypatch.setattr(model_storage_service.shutil, "rmtree", lambda path, **kwargs: None if Path(path).parent == cache else remove(path, **kwargs))
    with TestClient(create_app(controller=object())) as client:
        response = client.post("/api/setup/adopt", json={"profile": "base"})
    assert response.status_code == 409
    assert state_path.read_bytes() == before
    manifests = list(cache.glob(".orpheus-delete-*.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text())["settings_committed"] is True


@pytest.mark.parametrize("failure", ["exception", "zero", "unreadable-references"])
@requires_shared_blob_store
def test_failed_blob_sweep_keeps_manifest_until_cleanup_succeeds(storage, monkeypatch, failure):
    cache, _, repos = storage
    payload = _link_shared_blob(cache, repos["cleaner-cuda"], "f9" + "0" * 62, b"weights")
    with monkeypatch.context() as patch:
        if failure == "exception":
            def locked(*args, **kwargs):
                raise PermissionError("locked")
            patch.setattr(_shared_blobs, "sweep_shared_blob", locked)
        elif failure == "zero":
            patch.setattr(_shared_blobs, "sweep_shared_blob", lambda *args, **kwargs: 0)
        else:
            read = Path.read_text
            def locked_read(path, *args, **kwargs):
                if path == payload.with_name(payload.name + ".refs"):
                    raise PermissionError("locked")
                return read(path, *args, **kwargs)
            patch.setattr(Path, "read_text", locked_read)
        with TestClient(create_app(controller=object())) as client:
            response = client.delete("/api/setup/models/cleaner-cuda")
        assert response.status_code == 200
        assert not repos["cleaner-cuda"].exists()
        assert payload.exists()
        assert list(cache.glob(".orpheus-delete-*.json"))
    for _ in range(2):
        with setup_runtime.SetupLock(setup_runtime.default_state_path()):
            model_storage_service._recover_staged_models(cache_root=cache)
        assert not payload.exists()
        assert not list(cache.glob(".orpheus-delete-*"))


def test_locked_manifest_retirement_does_not_change_successful_deletion_response(storage, monkeypatch):
    cache, _, repos = storage
    unlink = Path.unlink
    with monkeypatch.context() as patch:
        def locked(path, **kwargs):
            if path.name.startswith(".orpheus-delete-") and path.suffix == ".json":
                raise PermissionError("locked")
            return unlink(path, **kwargs)
        patch.setattr(Path, "unlink", locked)
        with TestClient(create_app(controller=object())) as client:
            response = client.delete("/api/setup/models/cleaner-cuda")
        assert response.status_code == 200
        assert not repos["cleaner-cuda"].exists()
        assert list(cache.glob(".orpheus-delete-*.json"))
    model_storage_service._recover_staged_models(cache_root=cache)
    assert not list(cache.glob(".orpheus-delete-*"))


def test_partial_uninstall_preserves_state_bytes_until_recovery_finishes(storage, monkeypatch):
    cache, state_path, repos = storage
    state = json.loads(state_path.read_text())
    state["extra_metadata"] = "preserve"
    state_path.write_text(json.dumps(state, indent=2))
    before = state_path.read_bytes()
    with monkeypatch.context() as patch:
        remove = model_storage_service.shutil.rmtree
        def locked(path, **kwargs):
            if Path(path).parent != cache:
                return remove(path, **kwargs)
        patch.setattr(model_storage_service.shutil, "rmtree", locked)
        with TestClient(create_app(controller=object())) as client:
            response = client.delete("/api/setup/runtime")
        assert response.status_code == 200
        assert response.json()["state_deleted"] is False
        assert state_path.read_bytes() == before
        assert list(cache.glob(".orpheus-delete-*.json"))
    app_state = SimpleNamespace()
    for _ in range(2):
        with setup_runtime.SetupLock(state_path):
            model_storage_service._recover_staged_models(
                cache_root=cache,
                on_uninstall_complete=lambda reset_id: model_storage_service.reset_setup_preferences(None, app_state, reset_id),
            )
        assert not state_path.exists()
        assert not repos["tts-cpu-cuda"].exists()
        assert not repos["cleaner-cuda"].exists()
        assert repos["unrelated"].exists()
        assert not list(cache.glob(".orpheus-delete-*"))


def test_inventory_completes_partial_uninstall_and_resets_preferences(storage, monkeypatch, tmp_path):
    from modules.app.config import AppConfig

    cache, state_path, repos = storage
    config = AppConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "output", temp_dir=tmp_path / "temp",
                       cleaner_backend="api", cleaner_model_name="custom-model", cleaner_cuda_quantization="fp16")
    baseline = config.setup_preferences()
    config.apply_setup_preferences(state=setup_runtime.read_setup_state(state_path))
    monkeypatch.setattr(routes, "_detect_installed_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr("modules.runtime_setup.status._probe_profile", lambda *args, **kwargs: {"available": True, "checks": []})
    app = create_app(controller=SimpleNamespace(config=config))
    with TestClient(app) as client:
        with monkeypatch.context() as patch:
            remove = model_storage_service.shutil.rmtree

            def locked(path, **kwargs):
                if Path(path).parent == cache:
                    raise PermissionError("File in use")
                return remove(path, **kwargs)

            patch.setattr(model_storage_service.shutil, "rmtree", locked)
            response = client.delete("/api/setup/runtime")
            assert response.status_code == 200
            assert response.json()["state_deleted"] is False
            assert not response.json().get("setup_reset_id")
            assert config.setup_preferences().cleaner_backend == "cuda"
            assert state_path.exists()
        response = client.get("/api/setup/models")
        assert response.status_code == 200
        assert not any(model["downloaded"] for model in response.json()["models"])
        assert not state_path.exists()
        assert repos["unrelated"].exists()
        assert not list(cache.glob(".orpheus-delete-*"))
        assert config.setup_preferences() == baseline
        status = client.get("/api/setup/status").json()
        assert status["setup_reset_id"] == app.state.setup_reset_id
        assert status["setup_reset_id"]
        client.get("/api/setup/models")
        assert client.get("/api/setup/status").json()["setup_reset_id"] == status["setup_reset_id"]


def test_inventory_preserves_uninstall_evidence_without_request(storage, monkeypatch):
    cache, state_path, _ = storage
    with monkeypatch.context() as patch:
        remove = model_storage_service.shutil.rmtree
        patch.setattr(model_storage_service.shutil, "rmtree",
                      lambda path, **kwargs: None if Path(path).parent == cache else remove(path, **kwargs))
        with TestClient(create_app(controller=object())) as client:
            assert client.delete("/api/setup/runtime").status_code == 200
    before = state_path.read_bytes()
    journals = {path: path.read_bytes() for path in cache.glob(".orpheus-delete-*.json")}
    assert journals

    routes.get_managed_models()

    assert state_path.read_bytes() == before
    assert all(path.read_bytes() == data for path, data in journals.items())
    assert all(path.with_suffix("").exists() for path in journals)


def test_recovered_uninstall_resets_once_even_when_journal_removal_fails(storage, monkeypatch):
    cache, state_path, _ = storage
    resets = []
    config = SimpleNamespace(reset_setup_preferences=lambda: resets.append(True))
    app = create_app(controller=SimpleNamespace(config=config))
    with TestClient(app) as client:
        with monkeypatch.context() as patch:
            remove = model_storage_service.shutil.rmtree
            patch.setattr(model_storage_service.shutil, "rmtree",
                          lambda path, **kwargs: None if Path(path).parent == cache else remove(path, **kwargs))
            assert client.delete("/api/setup/runtime").status_code == 200
        assert state_path.exists()
        unlink = Path.unlink

        def locked(path, **kwargs):
            if path.name.startswith(".orpheus-delete-") and path.suffix == ".json":
                raise PermissionError("File in use")
            return unlink(path, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", locked)
            assert client.get("/api/setup/models").status_code == 200
            assert not state_path.exists()
            assert list(cache.glob(".orpheus-delete-*.json"))
            assert resets == [True]
            reset_id = app.state.setup_reset_id
            assert {json.loads(path.read_text())["installation_id"]
                    for path in cache.glob(".orpheus-delete-*.json")} == {reset_id}
            for _ in range(2):
                assert client.get("/api/setup/models").status_code == 200
                assert app.state.setup_reset_id == reset_id
                assert resets == [True]
            restarted = create_app(controller=SimpleNamespace(config=config))
            with TestClient(restarted) as other_client:
                assert other_client.get("/api/setup/models").status_code == 200
                assert restarted.state.setup_reset_id == reset_id
            assert resets == [True, True]
        assert client.get("/api/setup/models").status_code == 200
        assert not list(cache.glob(".orpheus-delete-*.json"))
        assert resets == [True, True]


def test_uninstall_completion_replay_preserves_preferences_after_a_later_reset():
    resets = []
    config = SimpleNamespace(reset_setup_preferences=lambda: resets.append(True))
    state = SimpleNamespace()
    model_storage_service.reset_setup_preferences(config, state, "first")
    model_storage_service.reset_setup_preferences(config, state, "second")

    model_storage_service.reset_setup_preferences(config, state, "first")

    assert resets == [True, True]
    assert state.setup_reset_id == "second"


def test_unconfigured_uninstall_rename_error_rolls_back_and_allows_retry(storage, monkeypatch):
    cache, state_path, repos = storage
    state_path.unlink()
    rename = Path.rename

    def locked(path, target):
        if path == repos["cleaner-cuda"]:
            raise PermissionError("File in use")
        return rename(path, target)

    with TestClient(create_app(controller=object()), raise_server_exceptions=False) as client:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "rename", locked)
            assert client.delete("/api/setup/runtime").status_code == 409
        assert not list(cache.glob(".orpheus-delete-*"))
        assert repos["tts-cpu-cuda"].exists()
        assert repos["cleaner-cuda"].exists()
        assert not state_path.exists()
        assert client.delete("/api/setup/runtime").status_code == 200
        assert not repos["tts-cpu-cuda"].exists()
        assert not repos["cleaner-cuda"].exists()
        assert repos["unrelated"].exists()


def test_unconfigured_uninstall_keeps_commit_witness_when_a_companion_flag_cannot_be_written(storage, monkeypatch):
    cache, state_path, repos = storage
    state_path.unlink()
    write = setup_runtime._atomic_write_json
    witness = []

    def fail_companion(path, payload):
        if path.name.startswith(".orpheus-delete-") and payload["settings_committed"]:
            if witness and path != witness[0]:
                raise PermissionError("Journal in use")
            if not witness:
                witness.append(path)
        return write(path, payload)

    with TestClient(create_app(controller=object())) as client:
        with monkeypatch.context() as patch:
            patch.setattr(model_storage_service.setup_runtime, "_atomic_write_json", fail_companion)
            assert client.delete("/api/setup/runtime").status_code == 409
            for _ in range(2):
                assert client.get("/api/setup/models").status_code == 200
                journals = list(cache.glob(".orpheus-delete-*.json"))
                assert len(journals) == 2
                assert witness[0].exists()
                assert json.loads(witness[0].read_text())["settings_committed"]
                assert not state_path.exists()
        assert client.get("/api/setup/models").status_code == 200
        assert not repos["tts-cpu-cuda"].exists()
        assert not repos["cleaner-cuda"].exists()
        assert repos["unrelated"].exists()
        assert not list(cache.glob(".orpheus-delete-*"))


@pytest.mark.parametrize("legacy_redownload", [False, True])
def test_downloads_wait_for_uninstall_journals_and_legacy_repos_survive_recovery(storage, monkeypatch, legacy_redownload):
    cache, state_path, repos = storage
    downloads = []
    model_id = resolve_model_groups(("tts-cpu-cuda",))[0]

    def download(model):
        downloads.append(model)
        make_repo(cache, model)

    service = ModelDownloadService(downloader=download)
    monkeypatch.setattr(routes, "_get_download_service", lambda request: service)
    monkeypatch.setattr(routes, "build_setup_status_response", lambda request: {"configured": state_path.exists()})
    unlink = Path.unlink

    def locked(path, **kwargs):
        if path.name.startswith(".orpheus-delete-") and path.suffix == ".json":
            raise PermissionError("File in use")
        return unlink(path, **kwargs)

    payload = {"profile": "cuda", "model_groups": ["tts-cpu-cuda"]}
    with TestClient(create_app(controller=object())) as client:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", locked)
            assert client.delete("/api/setup/runtime").status_code == 200
            assert list(cache.glob(".orpheus-delete-*.json"))
            blocked = client.post("/api/setup/models/download", json=payload)
            assert blocked.status_code == 409
            assert service.status()["lifecycle"] == "idle"
            assert not downloads
            assert not repos["tts-cpu-cuda"].exists()
            if legacy_redownload:
                make_repo(cache, model_id)  # An older app allowed this while cleanup was pending.
        assert client.get("/api/setup/models").status_code == 200
        if legacy_redownload:
            assert list(cache.glob(".orpheus-delete-*.json"))
            assert repos["tts-cpu-cuda"].exists()
            assert client.post("/api/setup/models/download", json=payload).status_code == 409
            assert client.post("/api/setup/adopt", json={"profile": "base", "model_groups": []}).status_code == 409
            assert client.delete("/api/setup/runtime").status_code == 409
            assert repos["tts-cpu-cuda"].exists()
            return
        assert not list(cache.glob(".orpheus-delete-*"))
        assert repos["tts-cpu-cuda"].exists() is legacy_redownload
        if not legacy_redownload:
            assert client.post("/api/setup/models/download", json=payload).status_code == 200
            deadline = time.monotonic() + 3
            while service.status()["lifecycle"] == "running" and time.monotonic() < deadline:
                time.sleep(.01)
            assert service.status()["lifecycle"] == "complete"
            assert downloads == [model_id]
        assert client.post("/api/setup/adopt", json={"profile": "base", "model_groups": []}).status_code == 200
        assert client.delete("/api/setup/runtime").status_code == 200
        assert not state_path.exists()
        assert not repos["tts-cpu-cuda"].exists()
        assert not list(cache.glob(".orpheus-delete-*"))
