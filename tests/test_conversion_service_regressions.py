"""Regression coverage for conversion service."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from modules.app.services.conversion_runtime import ConversionRuntimeOptions
from modules.storage.models import BookCreate, SourceType
from scripts import setup_runtime

from tests import test_conversion_upload_lifecycle as _upload_lifecycle
from tests._factories_group2_conversion import SOURCE, TITLES, _probe
from tests.test_conversion_service import _PipelineStub, _make_service


controller = _upload_lifecycle.controller


@pytest.mark.parametrize("endpoint", ["/api/sources/preflight", "/api/conversions"])
@pytest.mark.parametrize("suffix", [".txt", ".md", ".pdf", ".epub"])
def test_source_directory_is_rejected_before_runtime_work(controller, monkeypatch, tmp_path, endpoint, suffix):
    from fastapi.testclient import TestClient
    from modules.web.server import create_app

    source = tmp_path / f"folder{suffix}"
    source.mkdir()
    validate = Mock(side_effect=AssertionError("Directory reached runtime validation"))
    monkeypatch.setattr(controller._conversion_service, "_validate_requested_tts_runtime", validate)

    with TestClient(create_app(controller=controller)) as client:
        response = client.post(endpoint, json={"source_path": str(source)})

    assert response.status_code == 400
    assert response.json()["detail"] == "Choose a source file, not a folder or another non-file item."
    validate.assert_not_called()
    assert controller.get_active_job() is None


@pytest.mark.parametrize("outcome", ["completed", "cancelled", "failed"])
def test_upload_handoff_commits_recovery_before_synthesis_and_survives_reopening(conversion, tmp_path, monkeypatch, outcome):
    from io import BytesIO

    from modules.app.config import AppConfig
    from modules.app.controller import ConversionJob, JobStatus
    from modules.app.services.conversion_runtime import ConversionRuntimeOptions
    from modules.app.services.conversion_service import ConversionService
    from modules.app.services.library_service import LibraryService
    from modules.storage.models import PlaybackStatus
    from modules.storage.sqlite_repo import SQLiteRepository
    from modules.tts.api_rate_limit import GlobalAPILimiter

    _, engine, _ = conversion
    monkeypatch.chdir(tmp_path)
    for name in (
        "CLEANER_BACKEND", "AUDIOBOOK_ENABLE_PARALLEL", "AUDIOBOOK_MAX_PARALLEL_CHAPTERS",
        "AUDIOBOOK_ENABLE_CHAPTER_AAC_PACKAGING", "AUDIOBOOK_EMIT_CHAPTER_MP3_ARTIFACTS",
    ):
        monkeypatch.delenv(name, raising=False)
    config = AppConfig(
        data_dir=tmp_path / "data", output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp", db_path=tmp_path / "data" / "library.db",
        cleaner_backend="basic", enable_parallel=False, max_parallel_chapters=1,
        keep_tts_engine_warm=False, normalize_volume=False,
        enable_tts_file_batch_results=True,
    )
    repo = SQLiteRepository(config.db_path)
    library = LibraryService(repo, output_dir=config.output_dir, temp_dir=config.temp_dir)
    service = ConversionService(
        config=config, repository=repo, job_cls=ConversionJob, status_enum=JobStatus,
        cleaner_api_limiter=GlobalAPILimiter(1), runtime_validator=lambda *a, **k: None,
    )
    service.upload_reference_checker = library._is_source_referenced_by_books
    uploaded = library.upload_source(filename=SOURCE.name, file_obj=BytesIO(SOURCE.read_bytes()), max_bytes=4096)
    upload = config.temp_dir / "web_uploads" / uploaded["source_handle"].removeprefix("upload:")
    intermediate = service._durable_source_path_for_conversion(upload)
    assert upload.is_file() and intermediate.is_file()
    job = ConversionJob(id="tiny-conversion", book_id=None, status=JobStatus.RUNNING)
    job.upload_source_path = upload.resolve()
    service._active_job = job
    observed = []

    def inspect_handoff():
        book, chapters = repo.get_book_with_chapters(job.book_id)
        durable = Path(book.source_path)
        observed.append(durable)
        assert durable.read_bytes() == SOURCE.read_bytes()
        assert durable.parent.name == ".orpheus-source"
        assert not upload.exists() and not intermediate.exists()
        assert service.resolve_recovered_upload(upload) == durable
        assert book.playback_status == PlaybackStatus.MISSING
        assert [chapter.title for chapter in chapters] == TITLES
        assert all(chapter.mp3_path is None for chapter in chapters)
        if outcome == "cancelled":
            job.pipeline.cancel()
        elif outcome == "failed":
            raise RuntimeError("Speech synthesis unavailable")

    engine.before_batch = inspect_handoff
    try:
        service._run_conversion(
            job, intermediate, None, title="Tiny Book",
            runtime_options=ConversionRuntimeOptions(cleaner_backend="basic"),
        )
        assert observed
        assert job.status.value == outcome, job.error
        if outcome == "failed":
            assert "Speech synthesis unavailable" in job.error
        assert repo.count_books() == 1
        with closing(SQLiteRepository(config.db_path)) as reopened:
            book, chapters = reopened.get_book_with_chapters(job.book_id)
            assert Path(book.source_path).read_bytes() == SOURCE.read_bytes()
            assert [chapter.title for chapter in chapters] == TITLES
            if outcome == "completed":
                assert book.playback_status == PlaybackStatus.PACKAGED
                assert all(Path(chapter.mp3_path).is_file() for chapter in chapters)
                assert [chapter.packaged_start_ms for chapter in chapters] == [0, 3200]
                assert _probe(Path(book.playback_path))["chapters"][1]["tags"]["title"] == TITLES[1]
            else:
                assert book.playback_status == PlaybackStatus.MISSING
                assert all(chapter.mp3_path is None for chapter in chapters)
        assert service.resolve_recovered_upload(upload) == observed[0]
    finally:
        service.cleanup()
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


def test_conversion_keeps_one_preference_snapshot_after_setup_changes(tmp_path, monkeypatch):
    service = _make_service(tmp_path, _PipelineStub)
    path = tmp_path / "setup.json"
    monkeypatch.setattr("modules.runtime_setup.state.default_state_path", lambda: path)
    setup_runtime.write_setup_state(path, profile_id="cuda", model_groups=("tts-cpu-cuda", "cleaner-cuda"), accepted_risks=())
    service._config.apply_setup_preferences()
    source = tmp_path / "book.txt"
    source.write_text("source")
    captured = {}
    def start(**kwargs):
        captured.update(kwargs["kwargs"])
        return kwargs["job"]
    monkeypatch.setattr(service._job_coordinator, "_start_pipeline_job", start)
    service.start_conversion(source)
    setup_runtime.write_setup_state(path, profile_id="cuda", model_groups=("tts-cpu-cuda", "cleaner-cuda-large"), accepted_risks=(), cleaner_cuda_quantization="fp16")
    service._config.apply_setup_preferences()
    options = service._normalize_runtime_options(captured["runtime_options"])
    assert (options.cleaner_backend, options.cleaner_model, options.cleaner_cuda_quantization) == (
        "cuda", "Qwen/Qwen2.5-1.5B-Instruct", "4bit",
    )
    assert service._config.cleaner_model_name == "Qwen/Qwen2.5-3B-Instruct"
    assert service._config.cleaner_cuda_quantization == "fp16"
