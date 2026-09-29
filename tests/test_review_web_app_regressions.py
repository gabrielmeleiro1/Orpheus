"""Regression coverage for the web/app UI-refactor review findings."""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from modules.app.services.library_service import LibraryService
from modules.app.services.conversion_runtime import ConversionRuntimeOptions
from modules.storage.models import BookCreate, SourceType
from modules.storage.sqlite_repo import SQLiteRepository
from modules.web.routes import setup as routes
from modules.web.server import create_app
from scripts import setup_runtime
from tests.test_conversion_service import _make_service, _PipelineStub
from tests import test_conversion_upload_lifecycle as _upload_lifecycle
from tests import test_setup_model_management as _model_management

# Reuse the existing fixtures; pytest resolves them by name in this module.
controller = _upload_lifecycle.controller
storage = _model_management.storage


def test_failed_book_rollback_survives_staging_sweep(tmp_path, monkeypatch):
    repo = SQLiteRepository(tmp_path / "data" / "library.db")
    service = LibraryService(repo, output_dir=tmp_path / "output", temp_dir=tmp_path / "temp")
    source = tmp_path / "temp" / "web_uploads" / "book.txt"
    source.parent.mkdir()
    source.write_text("recoverable")
    book = repo.create_book(BookCreate(title="Book", source_path=str(source), source_type=SourceType.TXT))
    monkeypatch.setattr(repo, "delete_book", Mock(side_effect=RuntimeError("database unavailable")))
    monkeypatch.setattr(service, "_restore_staged_artifacts", lambda _: None)
    try:
        with pytest.raises(RuntimeError, match="database unavailable"):
            service.delete_book(book.id)
        root = tmp_path / "temp" / "delete_staging"
        old = time.time() - 3 * 86400
        for path in root.rglob("*"):
            os.utime(path, (old, old))
        assert service.sweep_delete_staging() == 0
        assert repo.get_book(book.id) is not None
        assert next(root.rglob("book.txt")).read_text() == "recoverable"
    finally:
        repo.close()


@pytest.mark.parametrize("repair", [False, True])
def test_runtime_validation_reserves_slot_until_failure(tmp_path, monkeypatch, repair):
    service = _make_service(tmp_path, _PipelineStub)
    entered, release = Event(), Event()
    def validate(**kwargs):
        entered.set()
        assert release.wait(5)
        raise RuntimeError("validation failed")
    monkeypatch.setattr(service, "_validate_requested_tts_runtime", validate)
    source = tmp_path / "book.txt"
    source.write_text("source")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.start_repair, 1) if repair else pool.submit(service.start_conversion, source)
        try:
            assert entered.wait(5)
            with pytest.raises(RuntimeError, match="conversion"):
                service.begin_storage_cleanup()
            assert service.get_active_job() is not None
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="validation failed"):
            future.result(5)
    assert not service.has_active_job()
    service.begin_storage_cleanup()
    service.end_storage_cleanup()


def test_failed_pipeline_keeps_upload_retryable(controller, monkeypatch):
    uploaded = controller.upload_text_source(text="Retry these words.", title="Book", max_bytes=1024)
    handle = uploaded["source_handle"]
    service = controller._conversion_service
    monkeypatch.setattr(service, "_validate_requested_tts_runtime", lambda **_: None)
    monkeypatch.setattr(service, "_create_pipeline_config", Mock(side_effect=ValueError("invalid cleaner")))
    source = controller.config.temp_dir / "web_uploads" / handle.removeprefix("upload:")
    original = source.read_bytes()
    job = service.start_conversion(source)
    job._thread.join(5)
    assert job.status.value == "failed"
    assert job.book_id is None
    assert source.read_bytes() == original
    retry = service.start_conversion(source)
    retry._thread.join(5)
    assert retry.id != job.id


def test_upload_removed_after_recovery_record_is_persisted(controller, monkeypatch):
    uploaded = controller.upload_text_source(text="Recover these words.", title="Book", max_bytes=1024)
    source = controller.config.temp_dir / "web_uploads" / uploaded["source_handle"].removeprefix("upload:")
    service = controller._conversion_service
    durable = service._durable_source_path_for_conversion(source)
    def persist(source_path, result, **kwargs):
        assert source.exists()
        return controller.repository.create_book(BookCreate(
            title="Book", source_path=str(source_path), source_type=SourceType.MD,
        )).id
    monkeypatch.setattr(service, "_persist_conversion_library_records", persist)
    job = SimpleNamespace(book_id=None, upload_source_path=source.resolve())
    command = SimpleNamespace(job=job, conversion_mode="audiobook", runtime_options=ConversionRuntimeOptions())
    service._checkpoint_conversion(command, durable, None)
    assert not source.exists()
    book = controller.repository.get_book(job.book_id)
    assert book.source_path == str(durable)
    assert durable.exists()


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
        assert next(repos["cleaner-cuda"].parent.glob(".orpheus-delete-*")).is_dir()
        raise setup_runtime.SetupExecutionError("private state path")
    monkeypatch.setattr(setup_runtime, "write_setup_state", fail_write)
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
        monkeypatch.setattr(setup_runtime, "_read_setup_state", Mock(side_effect=setup_runtime.SetupExecutionError("unreadable")))
        return state
    monkeypatch.setattr(setup_runtime, "write_setup_state", write)
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


def test_adoption_keeps_setup_lock_until_preferences_apply(tmp_path, monkeypatch):
    state_path = tmp_path / "setup.json"
    monkeypatch.setattr(setup_runtime, "default_state_path", lambda: state_path)
    monkeypatch.setattr(routes, "build_setup_status_response", lambda _: {})
    def apply(**kwargs):
        with pytest.raises(setup_runtime.SetupExecutionError):
            with setup_runtime.SetupLock(state_path, timeout_seconds=0):
                pass
    with TestClient(create_app(controller=SimpleNamespace(config=SimpleNamespace(apply_setup_preferences=apply)))) as client:
        assert client.post("/api/setup/adopt", json={"profile": "base"}).status_code == 200


def test_conversion_keeps_one_preference_snapshot_after_setup_changes(tmp_path, monkeypatch):
    service = _make_service(tmp_path, _PipelineStub)
    path = tmp_path / "setup.json"
    monkeypatch.setattr(setup_runtime, "default_state_path", lambda: path)
    setup_runtime.write_setup_state(path, profile_id="cuda", model_groups=("tts-cpu-cuda", "cleaner-cuda"), accepted_risks=())
    service._config.apply_setup_preferences()
    source = tmp_path / "book.txt"
    source.write_text("source")
    captured = {}
    def start(**kwargs):
        captured.update(kwargs["kwargs"])
        return kwargs["job"]
    monkeypatch.setattr(service, "_start_pipeline_job", start)
    service.start_conversion(source)
    setup_runtime.write_setup_state(path, profile_id="cuda", model_groups=("tts-cpu-cuda", "cleaner-cuda-large"), accepted_risks=(), cleaner_cuda_quantization="fp16")
    service._config.apply_setup_preferences()
    options = service._normalize_runtime_options(captured["runtime_options"])
    assert (options.cleaner_backend, options.cleaner_model, options.cleaner_cuda_quantization) == (
        "cuda", "Qwen/Qwen2.5-1.5B-Instruct", "4bit",
    )
    assert service._config.cleaner_model_name == "Qwen/Qwen2.5-3B-Instruct"
    assert service._config.cleaner_cuda_quantization == "fp16"


def test_unreadable_setup_keeps_basic_and_api_options(controller, monkeypatch):
    monkeypatch.setattr(setup_runtime, "_read_setup_state", Mock(side_effect=setup_runtime.SetupExecutionError("private state path")))
    with TestClient(create_app(controller=controller)) as client:
        response = client.get("/api/options/models")
    assert response.status_code == 200
    data = response.json()
    assert data["cleaner_models"] == data["cuda_cleaner_models"] == []
    assert {option["name"] for option in data["cleaner_backend_options"]} == {"basic", "api"}
    assert "private state path" not in response.text


@pytest.mark.parametrize("configure", [True, False])
def test_download_validates_active_selection_only_when_configuring(monkeypatch, configure):
    start = Mock(return_value={"lifecycle": "running"})
    monkeypatch.setattr(routes, "_get_download_service", lambda _: SimpleNamespace(start=start))
    monkeypatch.setattr(routes, "cuda_hardware_error", lambda: None)
    with TestClient(create_app(controller=object())) as client:
        response = client.post("/api/setup/models/download", json={
            "profile": "cuda", "model_groups": ["tts-cpu-cuda", "cleaner-cuda", "cleaner-cuda-large"],
            "configure_runtime": configure,
        })
    assert response.status_code == (422 if configure else 200)
    assert start.call_count == (0 if configure else 1)


def test_partial_book_deletion_uses_opaque_folder_handle(controller, monkeypatch):
    from modules.app.services import library_service

    source = controller.config.temp_dir / "web_uploads" / "book.txt"
    source.parent.mkdir()
    source.write_text("recoverable")
    book = controller.repository.create_book(BookCreate(title="Book", source_path=str(source), source_type=SourceType.TXT))
    monkeypatch.setattr(controller._library_service, "_commit_staged_artifacts", Mock(side_effect=OSError("locked")))
    opened = []
    monkeypatch.setattr(library_service, "_default_folder_opener", opened.append)
    with TestClient(create_app(controller=controller)) as client:
        response = client.delete(f"/api/library/{book.id}")
        assert response.status_code == 200
        handle = response.json()["path"]
        assert len(handle) == 32 and all(c in "0123456789abcdef" for c in handle)
        response = client.post("/api/maintenance/delete-staging/open-folder", json={"path": handle})
        assert response.status_code == 200
        assert response.json() == {"folder_path": handle}
        assert opened == [controller.config.temp_dir / "delete_staging" / handle]
        for invalid in (str(opened[0]), "..", "../" + handle):
            assert client.post("/api/maintenance/delete-staging/open-folder", json={"path": invalid}).status_code == 404
