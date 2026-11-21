"""Queued upload ownership, durable copies and cleanup."""
from __future__ import annotations

import os
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from modules.app.config import AppConfig
from modules.app.controller import AppController, ConversionCallbacks, ConversionJob, JobStatus
from modules.pipeline.models import ChapterResult, ConversionResult
from modules.storage.maintenance import backup_database
from modules.storage.managed_roots import STORAGE_OWNER_NAME
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
    sources_dir = Path(controller.config.db_path).parent / "sources"
    assert not (sources_dir / source.name).exists()


def test_durable_copy_failure_never_removes_an_existing_source(controller, monkeypatch):
    uploaded = controller.upload_text_source(text="Keep these words.", title="Book", max_bytes=1024)
    source = controller.config.temp_dir / "web_uploads" / uploaded["source_handle"].removeprefix("upload:")
    sources_dir = Path(controller.config.db_path).parent / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    other_book_source = sources_dir / source.name
    other_book_source.write_bytes(b"another book's durable source")
    real_open = Path.open

    def refuse_exclusive_create(path, mode="r", *args, **kwargs):  # noqa: ANN001, ANN202
        if mode == "xb" and path == other_book_source:
            # Windows reports a name held by a pending deletion as access denied.
            raise PermissionError("access denied")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse_exclusive_create)
    with pytest.raises(PermissionError):
        controller._conversion_service._durable_source_path_for_conversion(source)
    assert other_book_source.read_bytes() == b"another book's durable source"


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


@pytest.mark.parametrize("outcome", ["completed", "cancelled", "failed"])
@pytest.mark.parametrize("source_kind", ["upload", "paste"])
def test_reconversion_resolves_removed_upload_to_registered_source(controller, monkeypatch, outcome, source_kind):
    content = b"Words to convert."
    if source_kind == "upload":
        uploaded = controller.upload_source(filename="book.txt", file_obj=BytesIO(content), max_bytes=1024)
    else:
        uploaded = controller.upload_text_source(text=content.decode(), title="Book", max_bytes=1024)
    handle = uploaded["source_handle"]
    source = controller.config.temp_dir / "web_uploads" / handle.removeprefix("upload:")
    content = source.read_bytes()
    document = SimpleNamespace(title="Book", author=None, chapters=[SimpleNamespace(number=1, title="One", content=content.decode())])
    service = controller._conversion_service
    monkeypatch.setattr(service, "_validate_requested_tts_runtime", lambda **kwargs: None)
    monkeypatch.setattr("modules.pipeline.pipeline_stages.PipelineStages.ingest", lambda *args: document)
    registered = []

    def dispatch(context, source_chapters, output_dir):
        book = controller.repository.get_book(service._active_job.book_id)
        registered.append(Path(book.source_path))
        assert registered[-1].read_bytes() == content
        assert not source.exists()
        if len(registered) == 1:
            if outcome == "failed":
                raise RuntimeError("Synthesis failed")
            if outcome == "cancelled":
                service._active_job.pipeline.cancel()
                return []
        audio = output_dir / "01_One.mp3"
        audio.write_bytes(b"audio")
        return [ChapterResult(1, "One", mp3_path=audio, duration_ms=1000)]

    def package(self, **kwargs):
        output = kwargs["output_dir"] / "book.m4b"
        output.write_bytes(b"packaged")
        return output

    monkeypatch.setattr("modules.pipeline.orchestrator.dispatch_chapters", dispatch)
    monkeypatch.setattr("modules.pipeline.pipeline_stages.PipelineStages.package", package)
    client = TestClient(create_app(controller=controller))
    first = client.post("/api/conversions", json={"source_path": handle, "cleaner_backend": "basic"})
    assert first.status_code == 200
    first_job = service._active_job
    first_job._thread.join(timeout=5)
    assert first_job.status.value == outcome, first_job.error
    assert not source.exists()

    second = client.post("/api/conversions", json={"source_path": handle, "cleaner_backend": "basic", "title": "Edited title"})
    assert second.status_code == 200, second.json()
    second_job = service._active_job
    second_job._thread.join(timeout=5)
    assert second_job.status == JobStatus.COMPLETED, second_job.error
    assert second_job.book_id != first_job.book_id
    assert len(registered) == 2
    assert registered[0].read_bytes() == content
    assert controller.delete_book(first_job.book_id)
    assert client.post("/api/conversions", json={"source_path": handle, "cleaner_backend": "basic"}).status_code == 400


@pytest.mark.parametrize("source_kind", ["upload", "paste", "external"])
def test_recovery_registration_and_book_deletion_leave_no_upload_copies(controller, monkeypatch, source_kind):
    content = b"Words to convert."
    if source_kind == "external":
        source = controller.config.data_dir.parent / "picked" / "book.txt"
        source.parent.mkdir()
        source.write_bytes(content)
    else:
        if source_kind == "upload":
            uploaded = controller.upload_source(filename="book.txt", file_obj=BytesIO(content), max_bytes=1024)
        else:
            uploaded = controller.upload_text_source(text=content.decode(), title="Book", max_bytes=1024)
        source = controller.config.temp_dir / "web_uploads" / uploaded["source_handle"].removeprefix("upload:")
        content = source.read_bytes()

    chapters = [SimpleNamespace(number=1, title="One", content=content.decode())]
    document = SimpleNamespace(title="Book", author=None, chapters=chapters)
    service = controller._conversion_service
    monkeypatch.setattr(service, "_validate_requested_tts_runtime", lambda **kwargs: None)
    monkeypatch.setattr("modules.pipeline.pipeline_stages.PipelineStages.ingest", lambda *args: document)
    registered = []

    def dispatch(context, source_chapters, output_dir):
        book = controller.repository.get_book(service._active_job.book_id)
        recovery_source = Path(book.source_path)
        assert recovery_source.parent.name == ".orpheus-source"
        assert recovery_source.read_bytes() == content
        assert {path.name for path in (controller.config.data_dir / "sources").iterdir()} == {STORAGE_OWNER_NAME}
        if source_kind == "external":
            assert source.read_bytes() == content
        registered.append(recovery_source)
        audio = output_dir / "01_One.mp3"
        audio.write_bytes(b"audio")
        return [ChapterResult(1, "One", mp3_path=audio, duration_ms=1000)]

    def package(self, **kwargs):
        output = kwargs["output_dir"] / "book.m4b"
        output.write_bytes(b"packaged")
        return output

    monkeypatch.setattr("modules.pipeline.orchestrator.dispatch_chapters", dispatch)
    monkeypatch.setattr("modules.pipeline.pipeline_stages.PipelineStages.package", package)
    job = service.start_conversion(source)
    job._thread.join(timeout=5)
    assert job.status == JobStatus.COMPLETED, job.error
    assert registered
    assert controller.delete_book(job.book_id) is True
    assert not registered[0].exists()
    assert {path.name for path in (controller.config.data_dir / "sources").iterdir()} == {STORAGE_OWNER_NAME}
    if source_kind == "external":
        assert source.read_bytes() == content
    else:
        assert not source.exists()


@pytest.mark.parametrize("replacement", ["missing_source", "source_link", "source_contents", "record_source", "upload_link", "upload_directory", "unknown_handle", "invalid_handle"])
def test_recovered_upload_handles_reject_missing_or_replaced_sources(controller, monkeypatch, replacement):
    uploaded = controller.upload_text_source(text="Original words.", title="Book", max_bytes=1024)
    handle = uploaded["source_handle"]
    source = controller.config.temp_dir / "web_uploads" / handle.removeprefix("upload:")
    service = controller._conversion_service
    durable = service._durable_source_path_for_conversion(source)
    recovery = controller.config.output_dir / "Book" / ".orpheus-source" / source.name
    recovery.parent.mkdir(parents=True)
    recovery.write_bytes(source.read_bytes())
    job = ConversionJob(id="job", book_id=None, status=JobStatus.RUNNING)
    job.upload_source_path = source.resolve()
    command = SimpleNamespace(job=job, source_path=durable, conversion_mode="audiobook", runtime_options=None)
    service._checkpoint_conversion(command, recovery, ConversionResult(success=False, title="Book", author=None))
    assert not source.exists()
    outside = controller.config.data_dir / "private.txt"
    outside.write_text("Private words.")
    if replacement == "missing_source":
        recovery.unlink()
    elif replacement == "source_link":
        recovery.unlink()
        recovery.symlink_to(outside)
    elif replacement == "source_contents":
        recovery.write_text("Different words.")
    elif replacement == "record_source":
        controller.repository.update_book(job.book_id, source_path=str(outside))
    elif replacement == "upload_link":
        source.symlink_to(outside)
    elif replacement == "upload_directory":
        source.mkdir()
    elif replacement == "unknown_handle":
        handle = "upload:private.txt"
    else:
        handle = "upload:../private.txt"
    start = Mock(return_value=ConversionJob(id="unexpected", book_id=None, status=JobStatus.IDLE))
    monkeypatch.setattr(controller, "start_conversion", start)
    response = TestClient(create_app(controller=controller)).post("/api/conversions", json={"source_path": handle, "cleaner_backend": "basic"})
    assert response.status_code == 400
    start.assert_not_called()
    assert outside.read_text() == "Private words."


@pytest.mark.parametrize("same_source_path", [False, True])
def test_backup_restore_expires_recovered_upload_handles(controller, monkeypatch, same_source_path):
    service = controller._conversion_service
    empty_backup = backup_database(controller.config.db_path)
    recovery = controller.config.output_dir / "Book" / ".orpheus-source" / "book.txt"
    other_source = controller.config.data_dir / "other.txt"
    other_source.write_text("Different book.")
    old_book = controller.repository.create_book(BookCreate(
        title="Other", source_path=str(recovery if same_source_path else other_source),
        source_type=SourceType.TXT, total_chapters=1,
    ))
    backup = backup_database(controller.config.db_path)
    controller.restore_database_backup(empty_backup.name)
    uploaded = controller.upload_text_source(text="Original words.", title="Book", max_bytes=1024)
    handle = uploaded["source_handle"]
    source = controller.config.temp_dir / "web_uploads" / handle.removeprefix("upload:")
    durable = service._durable_source_path_for_conversion(source)
    recovery.parent.mkdir(parents=True)
    recovery.write_bytes(source.read_bytes())
    job = ConversionJob(id="job", book_id=None, status=JobStatus.RUNNING)
    job.upload_source_path = source.resolve()
    command = SimpleNamespace(job=job, source_path=durable, conversion_mode="audiobook", runtime_options=None)
    service._checkpoint_conversion(command, recovery, ConversionResult(success=False, title="Book", author=None))
    assert job.book_id == old_book.id
    assert service.resolve_recovered_upload(source) == recovery
    controller.restore_database_backup(backup.name)
    assert controller.repository.get_book(old_book.id).title == "Other"
    start = Mock(return_value=ConversionJob(id="unexpected", book_id=None, status=JobStatus.IDLE))
    monkeypatch.setattr(controller, "start_conversion", start)
    response = TestClient(create_app(controller=controller)).post(
        "/api/conversions", json={"source_path": handle, "cleaner_backend": "basic"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Source file is no longer available — add it again"
    start.assert_not_called()


def test_failed_recovery_registration_keeps_upload_and_intermediate_copy(controller, monkeypatch):
    uploaded = controller.upload_text_source(text="Keep these words.", title="Book", max_bytes=1024)
    source = controller.config.temp_dir / "web_uploads" / uploaded["source_handle"].removeprefix("upload:")
    original = source.read_bytes()
    service = controller._conversion_service
    durable = service._durable_source_path_for_conversion(source)
    job = ConversionJob(id="job", book_id=None, status=JobStatus.RUNNING)
    job.upload_source_path = source.resolve()
    command = SimpleNamespace(job=job, source_path=durable, conversion_mode="audiobook", runtime_options=None)
    recovery_source = controller.config.output_dir / "Book" / ".orpheus-source" / source.name
    recovery_source.parent.mkdir(parents=True)
    recovery_source.write_bytes(original)
    monkeypatch.setattr(service, "_persist_conversion_library_records", Mock(side_effect=RuntimeError("Database unavailable")))

    with pytest.raises(RuntimeError, match="Database unavailable"):
        service._checkpoint_conversion(command, recovery_source, Mock())

    assert source.read_bytes() == original
    assert durable.read_bytes() == original
    assert job.book_id is None
    assert controller.repository.count_books() == 0


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "start_failed"])
def test_unregistered_conversion_leaves_no_durable_upload_copy(controller, monkeypatch, outcome):
    uploaded = controller.upload_text_source(text="Keep these words.", title="Book", max_bytes=1024)
    source = controller.config.temp_dir / "web_uploads" / uploaded["source_handle"].removeprefix("upload:")
    original = source.read_bytes()
    service = controller._conversion_service
    monkeypatch.setattr(service, "_validate_requested_tts_runtime", lambda **kwargs: None)

    def ingest(*args):
        if outcome == "cancelled":
            service._active_job.pipeline.cancel()
            return SimpleNamespace(title="Book", author=None, chapters=[])
        raise RuntimeError("Unreadable source")

    def refuse_start(event):
        raise RuntimeError("Observer unavailable")

    monkeypatch.setattr("modules.pipeline.pipeline_stages.PipelineStages.ingest", ingest)
    callbacks = ConversionCallbacks(on_event=refuse_start) if outcome == "start_failed" else None
    for _attempt in range(2):
        if outcome == "start_failed":
            with pytest.raises(RuntimeError, match="Observer unavailable"):
                service.start_conversion(source, callbacks)
            job = service._active_job
        else:
            job = service.start_conversion(source, callbacks)
            job._thread.join(timeout=5)
        assert job.status == (JobStatus.CANCELLED if outcome == "cancelled" else JobStatus.FAILED), job.error
        assert job.book_id is None

    assert {path.name for path in (controller.config.data_dir / "sources").iterdir()} == {STORAGE_OWNER_NAME}
    assert source.read_bytes() == original
    assert controller.repository.count_books() == 0


def test_repair_does_not_block_deleting_an_unrelated_upload(controller):
    uploaded = controller.upload_text_source(text="Keep these words.", title="Book", max_bytes=1024)
    handle = uploaded["source_handle"]
    source = controller.config.temp_dir / "web_uploads" / handle.removeprefix("upload:")
    book = controller.repository.create_book(BookCreate(title="Other", source_path="other.txt", source_type=SourceType.TXT, total_chapters=1))
    with controller._conversion_service.repair_reservation(book.id):
        assert controller.delete_upload(handle) == {"deleted": True}
    assert not source.exists()
