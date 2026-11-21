"""Public HTTP errors preserve messages, status codes, and compatibility fields."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.app.services import model_storage_service
from modules.app.services.model_download_service import ModelDownloadBusyError
from modules.errors import ConversionCleanupPendingError, TTSInstallationError
from modules.storage.sqlite_repo import LibraryReplacedError
from modules.web.routes import conversions, library, maintenance, setup, uploads


@pytest.fixture
def error_client(monkeypatch, tmp_path):
    app = FastAPI()
    controller = SimpleNamespace()
    app.state.controller = controller
    app.state.event_broker = SimpleNamespace(publish=lambda event: None)
    for route in (setup, library, conversions, maintenance, uploads):
        app.include_router(route.router)
    monkeypatch.setattr(setup, "_get_status_cache", lambda request: Mock())
    monkeypatch.setattr(setup.setup_runtime, "default_state_path", lambda: tmp_path / "setup.json")
    monkeypatch.setattr(setup.setup_runtime, "SetupLock", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(setup.setup_selection, "_validated_profile", lambda value: SimpleNamespace(id=value))
    monkeypatch.setattr(setup.setup_selection, "_validated_model_groups", lambda profile, groups: groups)
    monkeypatch.setattr(setup.setup_selection, "validate_model_selection", lambda groups: None)
    monkeypatch.setattr(setup.setup_selection, "require_voice_model", lambda *args: None)
    monkeypatch.setattr(model_storage_service, "_recover_staged_models", lambda **kwargs: ([], 0))
    with TestClient(app) as client:
        yield client, controller


SETUP_ERRORS = [
    ("invalid_request", 422, "At least one model group is required."),
    ("packages_not_ready", 409, "Runtime dependencies are missing. Run runtime setup to repair them."),
    ("probe_unavailable", 409, "The runtime check could not run. Check runtime and try again."),
    ("probe_failed", 409, "The selected runtime is unavailable. Check its dependencies and device support."),
    ("assets_missing", 409, "Required model files are missing. Install or repair the selected models."),
    ("download_in_progress", 409, "A model download is already running. Wait for it to finish or cancel it."),
    ("setup_in_progress", 409, "Setup is already running."),
    ("cache_unavailable", 503, "Model storage could not be read. Try again."),
    ("invalid_model", 422, "Unknown managed model."),
    ("conversion_in_progress", 409, "Wait for the current conversion to finish before deleting a model."),
    ("preview_in_progress", 409, "Wait for the preview to finish."),
    ("model_delete_failed", 409, "The model could not be deleted. Close any app using it and try again."),
    ("setup_state_write_failed", 503, "Model settings could not be saved. Try again."),
]


@pytest.mark.parametrize("code,status,message", SETUP_ERRORS, ids=[case[0] for case in SETUP_ERRORS])
def test_setup_errors_preserve_status_message_and_legacy_fields(error_client, monkeypatch, code, status, message):
    client, controller = error_client
    method, path = "post", "/api/setup/adopt"
    payload = {"profile": "cuda", "model_groups": ["tts-cpu-cuda"]}
    extras = {}
    monkeypatch.setattr(setup, "_check_selected_profile_packages", lambda profile: {"ready": True})
    monkeypatch.setattr(setup.setup_status, "_probe_profile", lambda *args, **kwargs: {"available": True})
    monkeypatch.setattr(setup.hub_cache, "_check_model_group_assets", lambda groups, **kwargs: {
        group: {"ready": True} for group in groups
    })
    if code == "invalid_request":
        payload["model_groups"] = []
    elif code == "packages_not_ready":
        extras = {"packages": {"ready": False}}
        monkeypatch.setattr(setup, "_check_selected_profile_packages", lambda profile: extras["packages"])
    elif code == "probe_unavailable":
        monkeypatch.setattr(setup.setup_status, "_probe_profile", Mock(side_effect=RuntimeError("private diagnostics")))
    elif code == "probe_failed":
        extras = {"probe": {"checks": [{"id": "tts-cpu-cuda", "ready": False}]}}
        monkeypatch.setattr(setup.setup_status, "_probe_profile", lambda *args, **kwargs: {
            "available": False, "checks": [{"id": "tts-cpu-cuda", "available": False, "path": "private"}],
        })
    elif code == "assets_missing":
        extras = {"model_groups": [{"id": "tts-cpu-cuda", "reason": "missing"}]}
        monkeypatch.setattr(setup.hub_cache, "_check_model_group_assets", lambda groups, **kwargs: {
            "tts-cpu-cuda": {"ready": False, "reason": "missing"},
        })
    elif code in {"download_in_progress", "setup_in_progress"}:
        path = "/api/setup/models/download"
        error = ModelDownloadBusyError("private diagnostics") if code == "download_in_progress" else setup.setup_runtime.SetupExecutionError(message)
        monkeypatch.setattr(setup, "_get_download_service", lambda request: SimpleNamespace(start=Mock(side_effect=error)))
    elif code == "cache_unavailable":
        method, path = "get", "/api/setup/models"
        monkeypatch.setattr(model_storage_service, "_staged_model_manifests", Mock(side_effect=OSError("private path")))
    else:
        method, path = "delete", "/api/setup/models/cleaner-cuda"
        if code == "invalid_model":
            path = "/api/setup/models/unknown"
        elif code == "conversion_in_progress":
            controller._conversion_service = SimpleNamespace(
                begin_storage_cleanup=Mock(side_effect=RuntimeError("busy")),
                storage_cleanup_blocker=lambda: "conversion_in_progress",
            )
        elif code == "preview_in_progress":
            monkeypatch.setattr(model_storage_service, "_recover_staged_models", Mock(side_effect=model_storage_service.PreviewInProgress(message)))
        elif code == "model_delete_failed":
            monkeypatch.setattr(model_storage_service, "_recover_staged_models", Mock(side_effect=OSError("private path")))
        elif code == "setup_state_write_failed":
            monkeypatch.setattr(setup.setup_runtime, "_read_setup_state", lambda path: ({
                "model_groups": ["cleaner-cuda"], "profile": "cuda", "cleaner_cuda_quantization": "4bit",
            }, None))
            monkeypatch.setattr(model_storage_service, "_stage_runtime_models", lambda *args, **kwargs: nullcontext({}))
            monkeypatch.setattr(setup.setup_runtime, "write_setup_state", Mock(side_effect=OSError("private path")))
    response = client.request(method, path, **({"json": payload} if method == "post" else {}))
    assert response.status_code == status
    expected = {"code": code, "error": code, "message": message, **extras}
    assert response.json()["detail"] == expected


@pytest.mark.parametrize("method,path,payload,operation", [
    ("patch", "/api/library/1", {"title": "Book"}, "update_book_metadata"),
    ("post", "/api/conversions/repair/1", {}, "repair_conversion"),
])
def test_replaced_library_preserves_conflict_details(error_client, method, path, payload, operation):
    client, controller = error_client
    setattr(controller, operation, Mock(side_effect=LibraryReplacedError()))
    response = client.request(method, path, json=payload)
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "library_replaced",
        "message": "The library was replaced. Reload the selected book and try again.",
    }


@pytest.mark.parametrize("error,status", [
    (TTSInstallationError("Repair the runtime.", model_name="private model path"), 409),
    (ValueError("Invalid selection."), 400),
    (RuntimeError("setup_in_progress: arbitrary runtime text"), 409),
])
def test_maintenance_errors_keep_plain_string_details(error_client, error, status):
    client, controller = error_client
    controller.restore_database_backup_report = Mock(side_effect=error)
    response = client.post("/api/maintenance/backups/example/restore")
    assert response.status_code == status
    assert response.json() == {"detail": str(error)}


def test_conversion_cleanup_refusal_has_an_explicit_retryable_code(error_client, monkeypatch, tmp_path):
    client, controller = error_client
    controller.start_conversion = Mock(side_effect=ConversionCleanupPendingError())
    monkeypatch.setattr(conversions, "resolve_source_reference", lambda *args, **kwargs: tmp_path / "source.md")
    response = client.post("/api/conversions", json={
        "source_path": "upload:source.md",
        "runtime_options": {"tts_engine": "kokoro", "voice": "af_heart", "speed": 1.0},
    })
    assert response.status_code == 409
    assert response.json() == {"detail": {
        "code": "conversion_cleanup_pending",
        "message": "The previous conversion is still finishing cleanup.",
    }}


@pytest.mark.parametrize("error,status", [(ValueError("Invalid text."), 400), (RuntimeError("Upload failed."), 500)])
def test_upload_errors_keep_plain_string_details(error_client, error, status):
    client, controller = error_client
    controller.upload_text_source = Mock(side_effect=error)
    response = client.post("/api/uploads/text", json={"text": "Book"})
    assert response.status_code == status
    assert response.json() == {"detail": str(error)}


@pytest.mark.parametrize("method,path,payload", [
    ("post", "/api/setup/adopt", {"profile": "base"}),
    ("delete", "/api/setup/models/cleaner-cuda", None),
    ("delete", "/api/setup/runtime", None),
])
def test_setup_lock_conflicts_keep_the_exception_message(error_client, monkeypatch, method, path, payload):
    client, _ = error_client
    message = "Another setup operation holds the lock."
    monkeypatch.setattr(setup.setup_runtime, "SetupLock", Mock(side_effect=setup.setup_runtime.SetupExecutionError(message)))
    response = client.request(method, path, **({"json": payload} if payload else {}))
    assert response.status_code == 409
    assert response.json() == {"detail": {
        "code": "setup_in_progress", "error": "setup_in_progress", "message": message,
    }}


def test_runtime_deletion_keeps_the_conversion_guard_message(error_client):
    client, controller = error_client
    message = "Finish the current conversion first."
    controller._conversion_service = SimpleNamespace(
        begin_storage_cleanup=Mock(side_effect=RuntimeError(message)),
        storage_cleanup_blocker=lambda: "conversion_in_progress",
    )
    response = client.delete("/api/setup/runtime")
    assert response.status_code == 409
    assert response.json() == {"detail": {
        "code": "conversion_in_progress", "error": "conversion_in_progress", "message": message,
    }}


def test_tts_installation_metadata_excludes_internal_model_details():
    from modules.contracts.public_errors import public_error_detail

    error = TTSInstallationError("Repair the runtime.", model_name="private model path").public_error
    assert error.status == 409
    assert public_error_detail(error) == {"code": "tts_installation_error", "message": "Repair the runtime."}


def test_public_error_identity_takes_precedence_over_extra_details():
    from modules.contracts.public_errors import PublicError, public_error_detail

    details = {"code": "other", "message": "other", "error": "other", "packages": {"ready": False}}
    error = PublicError("packages_not_ready", "Repair the runtime.", 409, details=details)
    assert public_error_detail(error, legacy_error=True) == {
        "code": "packages_not_ready", "error": "packages_not_ready", "message": "Repair the runtime.",
        "packages": {"ready": False},
    }
    assert details["code"] == "other"
