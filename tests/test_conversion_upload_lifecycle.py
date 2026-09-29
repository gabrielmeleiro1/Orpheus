"""Queued upload ownership, durable copies and cleanup."""
from __future__ import annotations

import os
import time
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from modules.app.config import AppConfig
from modules.app.controller import AppController, ConversionJob, JobStatus
from modules.storage.models import BookCreate, SourceType
from modules.web.server import create_app


@pytest.fixture
def controller(tmp_path):
    config = AppConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "output",
                       temp_dir=tmp_path / "temp", db_path=tmp_path / "data" / "library.db",
                       worker_pool_reuse_enabled=False)
    app = AppController(config)
    yield app
    app._voice_service.cleanup()
    app._conversion_service.cleanup()
    app.repository.close()


def test_uploaded_source_kept_until_recoverable_record_exists(controller, monkeypatch):
    uploaded = controller.upload_text_source(text="Keep these words.", title="Book", max_bytes=1024)
    source = controller.config.temp_dir / "web_uploads" / uploaded["source_handle"].removeprefix("upload:")
    original = source.read_bytes()
    durable = controller._conversion_service._durable_source_path_for_conversion(source)
    assert durable.read_bytes() == original
    assert source.exists()
    # Explicit queue deletion remains available before conversion.
    assert controller.delete_upload(uploaded["source_handle"]) == {"deleted": True}


def test_copy_failure_keeps_upload(controller, monkeypatch):
    uploaded = controller.upload_text_source(text="Keep these words.", title="Book", max_bytes=1024)
    source = controller.config.temp_dir / "web_uploads" / uploaded["source_handle"].removeprefix("upload:")
    monkeypatch.setattr("modules.app.services.conversion_service.shutil.copyfileobj", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        controller._conversion_service._durable_source_path_for_conversion(source)
    assert source.is_file()


def test_delete_upload_refuses_active_and_library_references(controller):
    uploaded = controller.upload_text_source(text="Keep these words.", title="Book", max_bytes=1024)
    handle = uploaded["source_handle"]
    source = controller.config.temp_dir / "web_uploads" / handle.removeprefix("upload:")
    job = ConversionJob(id="job", book_id=None, status=JobStatus.RUNNING)
    job.upload_source_path = source.resolve()
    controller._conversion_service._active_job = job
    client = TestClient(create_app(controller=controller))
    assert client.delete(f"/api/uploads/{handle}").status_code == 409
    other = controller.upload_text_source(text="Another source.", title="Other", max_bytes=1024)
    assert client.delete(f"/api/uploads/{other['source_handle']}").status_code == 200
    job.status = JobStatus.COMPLETED
    controller.repository.create_book(BookCreate(title="Book", source_path=str(source), source_type=SourceType.TXT, total_chapters=1))
    assert client.delete(f"/api/uploads/{handle}").status_code == 409
    assert source.is_file()
    assert client.delete("/api/uploads/upload:..").status_code == 400


def test_sweep_preserves_week_old_queue_and_active_upload(controller):
    uploaded = controller.upload_text_source(text="Keep these words.", title="Book", max_bytes=1024)
    source = controller.config.temp_dir / "web_uploads" / uploaded["source_handle"].removeprefix("upload:")
    age = time.time() - 6 * 24 * 3600
    os.utime(source, (age, age))
    assert controller.sweep_stale_uploads() == 0
    age -= 2 * 24 * 3600
    os.utime(source, (age, age))
    controller._conversion_service._active_job = ConversionJob(id="job", book_id=None, status=JobStatus.IDLE)
    assert controller.sweep_stale_uploads() == 0
    controller._conversion_service._active_job = None
    assert controller.sweep_stale_uploads() == 1


def test_missing_queued_upload_has_actionable_start_error(controller):
    response = TestClient(create_app(controller=controller)).post("/api/conversions", json={"source_path": "upload:missing.txt", "cleaner_backend": "basic"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Source file is no longer available \u2014 add it again"
