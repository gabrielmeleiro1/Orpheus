"""Regression coverage for model storage service."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
import json
import threading

from fastapi.testclient import TestClient
from huggingface_hub import scan_cache_dir
import pytest

from modules.app.config import AppConfig
from modules.app.services import model_storage_service, voice_service
from modules.app.services.voice_service import VoiceService
from modules.runtime_setup import hub_cache
from modules.runtime_setup.catalog import resolve_model_groups
from modules.tts import model_cache
from modules.web.routes import setup as setup_route
from modules.web.server import create_app
from scripts import setup_runtime

from tests import (
    test_conversion_upload_lifecycle as _upload_lifecycle,
    test_setup_model_management as _model_management,
)


class _Engine:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.is_loaded = False
        self.model = object()
        self.unloads = []
        self.sample_rate = 24000
        self.supported_voices = [SimpleNamespace(id="am_adam")]

    def load(self):
        assert self.snapshot.exists()
        model_cache.set_cached_model(str(self.snapshot), self.model)
        self.is_loaded = True

    def unload(self, *, keep_in_cache=True):
        self.unloads.append(keep_in_cache)
        if not keep_in_cache:
            model_cache.discard_cached_model_if_same(str(self.snapshot), self.model)
        self.is_loaded = False

    def validate_voice(self, voice):
        return voice == "am_adam"

    def synthesize(self, text, voice, speed=1.0):
        assert self.snapshot.exists()
        return [0.0] * 8


@pytest.fixture
def preview_storage(monkeypatch, tmp_path):
    cache = tmp_path / "models"
    model_id = resolve_model_groups(("tts-cpu-cuda",))[0]
    repo = cache / ("models--" + model_id.replace("/", "--"))
    snapshot = repo / "snapshots" / ("a" * 40)
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    (repo / "refs").mkdir()
    (repo / "refs/main").write_text("a" * 40)
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(hub_cache.hf_constants, "HF_HUB_CACHE", str(cache))
    monkeypatch.setattr(hub_cache, "scan_cache_dir", lambda: scan_cache_dir(cache))
    monkeypatch.setattr(voice_service.sf, "write", lambda *args: None)
    engines = []

    def create(*args, **kwargs):
        engine = _Engine(snapshot)
        engines.append(engine)
        return engine

    config = AppConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "out", temp_dir=tmp_path / "temp")
    config.temp_dir.mkdir(parents=True, exist_ok=True)
    service = VoiceService(config, tts_factory=SimpleNamespace(create=create))
    calls = []
    controller = SimpleNamespace(
        generate_voice_preview=service.generate_voice_preview,
        _conversion_service=SimpleNamespace(
            begin_storage_cleanup=lambda: calls.append("begin"),
            end_storage_cleanup=lambda: calls.append("end"),
        ),
    )
    model_cache.clear_model_cache()
    with TestClient(create_app(controller=controller)) as client:
        try:
            yield SimpleNamespace(service=service, client=client, repo=repo, snapshot=snapshot, engines=engines, calls=calls)
        finally:
            service.cleanup()
            model_cache.clear_model_cache()


@contextmanager
def _paused_call(monkeypatch, target, name, operation):
    entered = threading.Event()
    release = threading.Event()
    original = getattr(target, name)

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5), "Operation was not released"
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(operation)
        try:
            assert entered.wait(5), "Operation did not reach the boundary"
            yield future
        finally:
            release.set()
            future.result(timeout=5)


def _write_staged_manifest(env):
    stage = env.repo.with_name(".orpheus-delete-" + "b" * 32)
    stage.mkdir()
    manifest = stage.with_name(stage.name + ".json")
    manifest.write_text(json.dumps({
        "original": env.repo.name,
        "staged": stage.name,
        "settings_committed": True,
    }))
    return stage, manifest


controller = _upload_lifecycle.controller


storage = _model_management.storage


def test_installed_runtime_adoption_succeeds_during_preview_without_staged_models(preview_storage, monkeypatch):
    env = preview_storage
    monkeypatch.setattr("modules.runtime_profiles.platform.system", lambda: "Windows")
    monkeypatch.setattr(setup_route, "_check_selected_profile_packages", lambda _: {"ready": True})
    monkeypatch.setattr("modules.runtime_setup.status._probe_profile", lambda *args, **kwargs: {"available": True})
    monkeypatch.setattr(hub_cache, "_check_model_group_assets", lambda groups, **kwargs: {
        group: {"ready": True, "reason": None} for group in groups
    })
    monkeypatch.setattr(setup_route, "build_setup_status_response", lambda _: setup_runtime.read_setup_state(setup_runtime.default_state_path()))

    with _paused_call(monkeypatch, _Engine, "synthesize", lambda: env.service.generate_voice_preview("am_adam")):
        response = env.client.post("/api/setup/adopt", json={"profile": "cuda", "model_groups": ["tts-cpu-cuda"]})
        assert response.status_code == 200, response.text
        assert response.json()["model_groups"] == ["tts-cpu-cuda"]
        assert env.repo.exists()


@pytest.mark.parametrize("operation", ["preview", "validation"])
def test_empty_recovery_scan_allows_runtime_use(preview_storage, monkeypatch, operation):
    env = preview_storage
    with _paused_call(monkeypatch, model_storage_service, "_staged_model_manifests", model_storage_service._recover_staged_models) as recovery:
        if operation == "preview":
            env.service.generate_voice_preview("am_adam")
        else:
            env.service.validate_tts_runtime("kokoro")
        assert env.repo.exists()
    assert recovery.result() == ([], 0)


def test_recovery_for_other_groups_allows_active_preview(preview_storage):
    env = preview_storage
    stage, manifest = _write_staged_manifest(env)
    with model_storage_service.preview_model_use(env.service):
        assert model_storage_service._recover_staged_models(model_groups=("cleaner-cuda",)) == ([], 0)
    assert stage.exists()
    assert manifest.exists()


def test_recovery_rechecks_manifests_after_reserving_storage(preview_storage, monkeypatch):
    env = preview_storage
    stage, manifest = _write_staged_manifest(env)
    original = model_storage_service._model_deletion_reservation

    @contextmanager
    def reserve():
        manifest.unlink()
        with original():
            yield

    monkeypatch.setattr(model_storage_service, "_model_deletion_reservation", reserve)
    assert model_storage_service._recover_staged_models() == ([], 0)
    assert stage.exists()


@pytest.mark.parametrize("operation", ["deletion", "staging", "recovery"])
def test_storage_failure_releases_model_ownership(preview_storage, monkeypatch, operation):
    env = preview_storage
    if operation == "recovery":
        _write_staged_manifest(env)

    def fail_discard(groups):
        raise RuntimeError("Model could not be unloaded")

    monkeypatch.setattr(model_storage_service, "_discard_preview_models", fail_discard)
    with pytest.raises(RuntimeError, match="Model could not be unloaded"):
        if operation == "deletion":
            model_storage_service._delete_runtime_models()
        elif operation == "staging":
            with model_storage_service._stage_runtime_models("tts-cpu-cuda", installation_id="test"):
                pytest.fail("Staging must fail before yielding")
        else:
            model_storage_service._recover_staged_models()
    assert env.service.ensure_preview_engine().is_loaded


@pytest.mark.parametrize("boundary", ["load", "synthesize"])
def test_deletion_refuses_loading_and_synthesizing_previews(preview_storage, monkeypatch, boundary):
    env = preview_storage
    with _paused_call(monkeypatch, _Engine, boundary, lambda: env.service.generate_voice_preview("am_adam")):
        response = env.client.delete("/api/setup/models/tts-cpu-cuda")
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "preview_in_progress"
        assert env.repo.exists()
        assert env.calls == ["begin", "end"]
    assert env.client.delete("/api/setup/models/tts-cpu-cuda").status_code == 200


@pytest.mark.parametrize("entry", ["ensure", "generate", "http"])
def test_preview_fails_fast_while_model_files_are_being_deleted(preview_storage, monkeypatch, entry):
    env = preview_storage
    with _paused_call(monkeypatch, model_storage_service.shutil, "rmtree", lambda: env.client.delete("/api/setup/models/tts-cpu-cuda")) as deletion:
        if entry == "http":
            response = env.client.post("/api/voices/preview", json={"voice": "am_adam"})
            assert response.status_code >= 400
            assert "model deletion" in response.json()["detail"].lower()
        else:
            operation = env.service.ensure_preview_engine if entry == "ensure" else lambda: env.service.generate_voice_preview("am_adam")
            with pytest.raises(RuntimeError, match="(?i)model deletion"):
                operation()
        assert env.engines == []
    assert deletion.result().status_code == 200


def test_deletion_discards_preview_engine_and_cached_snapshot(preview_storage):
    env = preview_storage
    engine = env.service.ensure_preview_engine()
    assert model_cache.get_cached_model(str(env.snapshot)) is engine.model
    # A second runtime variant can remain cached after its owning engine retires.
    variant_key = repr((str(env.snapshot), "other-runtime"))
    model_cache.set_cached_model(variant_key, object())
    response = env.client.delete("/api/setup/models/tts-cpu-cuda")
    assert response.status_code == 200
    assert not env.repo.exists()
    assert engine.unloads == [False]
    assert model_cache.get_cached_model(str(env.snapshot)) is None
    assert model_cache.get_cached_model(variant_key) is None


def test_deletion_discards_cached_snapshot_after_preview_idle_unload(preview_storage):
    env = preview_storage
    env.service.ensure_preview_engine()
    env.service._preview_idle_timer.function()
    assert env.service._preview_engine is None
    model_cache.set_cached_model(str(env.snapshot), object())
    assert env.client.delete("/api/setup/models/tts-cpu-cuda").status_code == 200
    assert model_cache.get_cached_model(str(env.snapshot)) is None


def test_deletion_refuses_preview_idle_unload(preview_storage, monkeypatch):
    env = preview_storage
    env.service.ensure_preview_engine()
    with _paused_call(monkeypatch, _Engine, "unload", env.service._preview_idle_timer.function):
        response = env.client.delete("/api/setup/models/tts-cpu-cuda")
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "preview_in_progress"
        assert env.repo.exists()
    assert env.client.delete("/api/setup/models/tts-cpu-cuda").status_code == 200


def test_preview_is_excluded_until_staged_models_are_rolled_back(preview_storage):
    env = preview_storage
    entered = threading.Event()
    release = threading.Event()

    def stage_models():
        with model_storage_service._stage_runtime_models("tts-cpu-cuda", installation_id="test"):
            entered.set()
            assert release.wait(5), "Staged models were not released"

    with ThreadPoolExecutor(max_workers=1) as pool:
        staged = pool.submit(stage_models)
        try:
            assert entered.wait(5)
            assert not env.repo.exists()
            with pytest.raises(RuntimeError, match="(?i)model deletion"):
                env.service.ensure_preview_engine()
        finally:
            release.set()
            staged.result(timeout=5)
    assert env.repo.exists()
    assert env.service.ensure_preview_engine().is_loaded


def test_failed_preview_load_releases_model_ownership(preview_storage, monkeypatch):
    env = preview_storage

    def fail_load(self):
        raise RuntimeError("Model could not be loaded")

    monkeypatch.setattr(_Engine, "load", fail_load)
    with pytest.raises(RuntimeError, match="Model could not be loaded"):
        env.service.ensure_preview_engine()
    assert env.client.delete("/api/setup/models/tts-cpu-cuda").status_code == 200


def test_failed_deletion_releases_preview_ownership(preview_storage, monkeypatch):
    env = preview_storage

    def fail_scan():
        raise OSError("Model storage could not be read")

    with monkeypatch.context() as patch:
        patch.setattr(model_storage_service, "scan_cache_dir", fail_scan)
        assert env.client.delete("/api/setup/models/tts-cpu-cuda").status_code == 409
    assert env.service.ensure_preview_engine().is_loaded


def test_preview_cannot_load_from_within_a_model_deletion(preview_storage):
    env = preview_storage
    with model_storage_service._stage_runtime_models("tts-cpu-cuda", installation_id="test"):
        with pytest.raises(RuntimeError, match="(?i)model deletion"):
            env.service.ensure_preview_engine()
    assert env.service.ensure_preview_engine().is_loaded


def test_idle_unload_is_rescheduled_when_other_model_deletion_owns_storage(preview_storage):
    env = preview_storage
    engine = env.service.ensure_preview_engine()
    timer = env.service._preview_idle_timer
    with model_storage_service._stage_runtime_models("cleaner-cuda", installation_id="test"):
        timer.function()
        assert engine.is_loaded
    assert env.service._preview_idle_timer is not timer
    env.service._preview_idle_timer.function()
    assert not engine.is_loaded


def test_preference_write_failure_restores_selected_model(storage, monkeypatch):
    _, state_path, repos = storage
    before = state_path.read_bytes()
    apply = Mock()
    released = []
    def end():
        released.append(repos["cleaner-cuda"].exists())
    app_controller = SimpleNamespace(
        config=SimpleNamespace(apply_setup_preferences=apply),
        _conversion_service=SimpleNamespace(begin_storage_cleanup=lambda: None, end_storage_cleanup=end),
    )
    def fail_write(*args, **kwargs):
        assert not repos["cleaner-cuda"].exists()
        entries = sorted(repos["cleaner-cuda"].parent.glob(".orpheus-delete-*"), reverse=True)
        staged = [entry for entry in entries if entry.is_dir()]
        assert len(staged) == 1
        assert staged[0].with_name(staged[0].name + ".json").is_file()
        raise setup_runtime.SetupExecutionError("private state path")
    monkeypatch.setattr("modules.runtime_setup.state.write_setup_state", fail_write)
    with TestClient(create_app(controller=app_controller)) as client:
        response = client.delete("/api/setup/models/cleaner-cuda")
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "setup_state_write_failed"
    assert "private state path" not in response.text
    assert state_path.read_bytes() == before
    assert repos["cleaner-cuda"].exists()
    assert released == [True]
    apply.assert_not_called()


def test_model_deletion_publishes_saved_state_without_rereading(storage, controller, monkeypatch):
    _, state_path, repos = storage
    controller.config.apply_setup_preferences()
    original_write = setup_runtime.write_setup_state
    def write(*args, **kwargs):
        state = original_write(*args, **kwargs)
        monkeypatch.setattr("modules.runtime_setup.state._read_setup_state", Mock(side_effect=setup_runtime.SetupExecutionError("unreadable")))
        return state
    monkeypatch.setattr("modules.runtime_setup.state.write_setup_state", write)
    with TestClient(create_app(controller=controller)) as client:
        assert client.delete("/api/setup/models/cleaner-cuda").status_code == 200
    assert controller.config.setup_preferences().cleaner_backend == "basic"
    assert not repos["cleaner-cuda"].exists()


def test_model_preferences_apply_before_releasing_reservation(storage):
    events = []
    app_controller = SimpleNamespace(
        config=SimpleNamespace(apply_setup_preferences=lambda **_: events.append("apply")),
        _conversion_service=SimpleNamespace(begin_storage_cleanup=lambda: events.append("begin"), end_storage_cleanup=lambda: events.append("end")),
    )
    with TestClient(create_app(controller=app_controller)) as client:
        assert client.delete("/api/setup/models/cleaner-cuda").status_code == 200
    assert events == ["begin", "apply", "end"]
