from __future__ import annotations

import threading
import time
import sqlite3
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from modules.app.config import AppConfig
from modules.app.controller import AppController, ConversionCallbacks, ConversionJob, JobStatus
from modules.app.events import EventType, JobState
from modules.app.services.conversion_service import ConversionRuntimeOptions, ConversionService
from modules.pipeline.orchestrator import (
    ChapterResult,
    ConversionPipeline,
    ConversionResult as PipelineConversionResult,
    RepairedChapterUpdate,
    RepairPlan,
    RepairResult,
)
from modules.pipeline.book_workspace import prepare_book_workspace
from modules.pipeline.pool_manager import SharedWorkerPoolManager
from modules.storage.models import Book, Chapter, ConversionMode, PlaybackStatus, SourceType
from modules.storage.sqlite_repo import SQLiteRepository


class _JobStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class _RepositoryStub:
    def __init__(self) -> None:
        self.create_calls = 0
        self.created_book = None
        self.created_chapters = None

    def create_book_with_chapters(self, book, chapters):  # noqa: ANN001, ANN201
        self.create_calls += 1
        self.created_book = book
        self.created_chapters = chapters
        return SimpleNamespace(id=123), []


class _LimiterStub:
    semaphore = object()

    @staticmethod
    def snapshot() -> dict[str, int]:
        return {"wait_count": 0, "wait_ms_total": 0}


class _PipelineStub:
    def __init__(self, result: PipelineConversionResult, **kwargs) -> None:  # noqa: ANN003
        _ = kwargs
        self._result = result

    def convert(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
        _ = kwargs
        return self._result


def _delegate_repair_to_pipeline_module(pipeline, **kwargs):  # noqa: ANN001, ANN003
    from modules.pipeline.repair import repair_conversion

    if not hasattr(pipeline, "stages"):
        pipeline.stages = SimpleNamespace(
            ingest=getattr(pipeline, "stage_ingest", None),
            warmup_models=getattr(pipeline, "stage_warmup_models", lambda: None),
            cleanup_models=getattr(
                pipeline,
                "stage_cleanup_models",
                lambda *, force=False: None,
            ),
            process_chapter=getattr(pipeline, "stage_process_chapter", None),
            process_chapters_parallel=getattr(
                pipeline,
                "stage_process_chapters_parallel",
                None,
            ),
            process_chapters_sequential=getattr(
                pipeline,
                "stage_process_chapters_sequential",
                None,
            ),
            package=getattr(pipeline, "stage_package", None),
        )
    return repair_conversion(pipeline, **kwargs)


def _apply_fake_repair_result(
    repository,
    book_id,
    *,
    repaired_chapter_updates,
    playback_path,
    playback_status,
    total_chapters,
):  # noqa: ANN001, ANN201
    for update in repaired_chapter_updates:
        repository.update_chapter(
            update.chapter_id,
            duration_ms=update.duration_ms,
            mp3_path=update.mp3_path,
        )
    return repository.update_book(
        book_id,
        playback_path=playback_path,
        playback_status=playback_status,
        total_chapters=total_chapters,
    )


def _make_service(
    tmp_path,
    pipeline_cls,
    *,
    job_cls=SimpleNamespace,
    status_enum=_JobStatus,
    shared_worker_pool_manager=None,
):
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    return ConversionService(
        config=config,
        repository=_RepositoryStub(),
        job_cls=job_cls,
        status_enum=status_enum,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=shared_worker_pool_manager,
        pipeline_cls=pipeline_cls,
    )


def _make_service_with_validator(tmp_path, runtime_validator):
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    return ConversionService(
        config=config,
        repository=_RepositoryStub(),
        job_cls=SimpleNamespace,
        status_enum=_JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=runtime_validator,
        shared_worker_pool_manager=None,
        pipeline_cls=_PipelineStub,
    )


def _default_runtime_options(**overrides) -> ConversionRuntimeOptions:
    values = {
        "voice": "am_adam",
        "speed": 1.0,
        "tts_engine": None,
        "tts_quantization": None,
        "cleaner_model": None,
        "cleaner_backend": None,
        "cleaner_api_model": None,
        "tts_options": None,
    }
    values.update(overrides)
    return ConversionRuntimeOptions(**values)


def _start_conversion_and_wait(
    service: ConversionService,
    source_path: Path,
    *,
    runtime_options: ConversionRuntimeOptions | None = None,
    callbacks: ConversionCallbacks | None = None,
    conversion_mode: ConversionMode | str = ConversionMode.AUDIOBOOK,
    title: str | None = None,
    author: str | None = None,
    timeout: float = 3.0,
) -> ConversionJob:
    job = service.start_conversion(
        source_path=source_path,
        callbacks=callbacks,
        conversion_mode=conversion_mode,
        title=title,
        author=author,
        runtime_options=runtime_options or _default_runtime_options(),
    )
    assert job.wait(timeout=timeout)
    return job


def _start_repair_and_wait(
    service: ConversionService,
    book_id: int,
    *,
    runtime_options: ConversionRuntimeOptions | None = None,
    callbacks: ConversionCallbacks | None = None,
    timeout: float = 3.0,
) -> ConversionJob:
    job = service.start_repair(
        book_id=book_id,
        callbacks=callbacks,
        runtime_options=runtime_options or _default_runtime_options(),
    )
    assert job.wait(timeout=timeout)
    return job


def test_app_config_defaults_cleaning_artifact_saving_on(tmp_path):
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )

    assert config.save_cleaning_debug is True


def test_app_config_serializes_kokoro_efficiency_settings(tmp_path):
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
        chunk_size=700,
        enable_tts_file_batch_results=False,
        keep_tts_engine_warm=True,
        tts_idle_timeout_seconds=123.0,
    )

    data = config.to_dict()

    assert data["chunk_size"] == 700
    assert data["enable_tts_file_batch_results"] is False
    assert data["keep_tts_engine_warm"] is True
    assert data["tts_idle_timeout_seconds"] == 123.0
    assert AppConfig.from_dict(data).tts_idle_timeout_seconds == 123.0


def test_conversion_service_reuses_hot_kokoro_engine_across_app_conversions(tmp_path):
    source_path = tmp_path / "book.md"
    source_path.write_text("# One\n\nText.", encoding="utf-8")
    output_root = tmp_path / "output"

    class _HotEngine:
        def __init__(self) -> None:
            self.is_loaded = True
            self.unload = MagicMock()

    class _HotPipeline:
        instances = []
        created_engines = []

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            self.config = kwargs["config"]
            self._tts_engine = None
            self._selected_parallel_chapters = 1
            self.cancelled_idle = 0
            self.stages = SimpleNamespace(
                cleanup_models=self.stage_cleanup_models,
                cancel_tts_idle_unload=self.stage_cancel_tts_idle_unload,
            )
            type(self).instances.append(self)

        def convert(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            _ = kwargs
            if self._tts_engine is None:
                self._tts_engine = _HotEngine()
                type(self).created_engines.append(self._tts_engine)
            output_dir = output_root / f"run_{len(type(self).instances)}"
            output_dir.mkdir(parents=True, exist_ok=True)
            chapter_mp3 = output_dir / "chapter.mp3"
            chapter_mp3.write_bytes(b"mp3")
            output_path = output_dir / "book.m4b"
            output_path.write_bytes(b"m4b")
            self.stages.cleanup_models()
            return PipelineConversionResult(
                success=True,
                title="Book",
                author="Author",
                output_path=output_path,
                chapters=[
                    ChapterResult(
                        chapter_number=1,
                        chapter_title="One",
                        mp3_path=chapter_mp3,
                        duration_ms=1000,
                    )
                ],
                total_duration_ms=1000,
            )

        def stage_cleanup_models(self, *, force: bool = False) -> None:
            if force and self._tts_engine is not None:
                self._tts_engine.unload(keep_in_cache=True)
                self._tts_engine = None

        def stage_cancel_tts_idle_unload(self) -> None:
            self.cancelled_idle += 1

    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=output_root,
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
        keep_tts_engine_warm=True,
        tts_idle_timeout_seconds=300.0,
    )
    service = ConversionService(
        config=config,
        repository=_RepositoryStub(),
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=_HotPipeline,
    )

    for _ in range(2):
        job = _start_conversion_and_wait(service, source_path)
        assert job.status == JobStatus.COMPLETED

    assert len(_HotPipeline.instances) == 2
    assert len(_HotPipeline.created_engines) == 1
    assert _HotPipeline.instances[1].cancelled_idle == 1
    service.cleanup()
    _HotPipeline.created_engines[0].unload.assert_called_once_with(keep_in_cache=False)


def test_app_config_defaults_to_four_requested_workers_without_parallel_auto_start(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("AUDIOBOOK_ENABLE_PARALLEL", raising=False)
    monkeypatch.delenv("AUDIOBOOK_MAX_PARALLEL_CHAPTERS", raising=False)

    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )

    assert config.enable_parallel is False
    assert config.max_parallel_chapters == 4


def test_app_config_allows_explicit_parallel_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIOBOOK_ENABLE_PARALLEL", "1")
    monkeypatch.setenv("AUDIOBOOK_MAX_PARALLEL_CHAPTERS", "2")

    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )

    assert config.enable_parallel is True
    assert config.max_parallel_chapters == 2


def test_conversion_service_manual_worker_selection_uses_static_memory_policy(tmp_path):
    service = _make_service(tmp_path, _PipelineStub)

    config = service._create_pipeline_config(  # noqa: SLF001 - direct contract coverage
        runtime_options=ConversionRuntimeOptions(
            voice="am_adam",
            speed=1.0,
            parallel_worker_mode="manual",
            enable_parallel=True,
            max_parallel_chapters=3,
        ),
    )

    assert config.enable_parallel is True
    assert config.max_parallel_chapters == 3
    assert config.memory_policy is not None
    assert config.memory_policy.enable_warmup_calibration is False


def test_conversion_service_auto_worker_selection_enables_warmup_policy(tmp_path):
    service = _make_service(tmp_path, _PipelineStub)

    config = service._create_pipeline_config(  # noqa: SLF001 - direct contract coverage
        runtime_options=ConversionRuntimeOptions(
            voice="am_adam",
            speed=1.0,
            parallel_worker_mode="auto",
            enable_parallel=True,
            max_parallel_chapters=4,
        ),
    )

    assert config.enable_parallel is True
    assert config.max_parallel_chapters == 4
    assert config.memory_policy is not None
    assert config.memory_policy.enable_warmup_calibration is True


def test_conversion_service_worker_limit_one_forces_sequential_without_policy(tmp_path):
    service = _make_service(tmp_path, _PipelineStub)

    config = service._create_pipeline_config(  # noqa: SLF001 - direct contract coverage
        runtime_options=ConversionRuntimeOptions(
            voice="am_adam",
            speed=1.0,
            parallel_worker_mode="auto",
            enable_parallel=True,
            max_parallel_chapters=1,
        ),
    )

    assert config.enable_parallel is False
    assert config.max_parallel_chapters == 1
    assert config.memory_policy is None


def test_conversion_service_rejects_pipeline_success_with_invalid_artifacts(tmp_path):
    repo = _RepositoryStub()
    source_path = tmp_path / "book.pdf"
    source_path.write_text("source", encoding="utf-8")
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    chapter_one_mp3 = tmp_path / "output" / "chapter_01.mp3"
    chapter_one_mp3.parent.mkdir(parents=True, exist_ok=True)
    chapter_one_mp3.write_bytes(b"mp3")
    pipeline_result = PipelineConversionResult(
        success=True,
        title="Broken Book",
        author="Author",
        output_path=tmp_path / "output" / "Broken Book.m4b",
        chapters=[
            ChapterResult(
                chapter_number=1,
                chapter_title="One",
                mp3_path=chapter_one_mp3,
                duration_ms=1000,
            ),
            ChapterResult(
                chapter_number=2,
                chapter_title="Two",
            ),
        ],
        total_duration_ms=1000,
    )
    pipeline_cls = lambda **kwargs: _PipelineStub(result=pipeline_result, **kwargs)  # noqa: E731

    service = ConversionService(
        config=config,
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=pipeline_cls,
    )

    job = _start_conversion_and_wait(
        service,
        source_path=source_path,
        title="Broken Book",
        author="Author",
    )

    assert job.status == JobStatus.FAILED
    assert job.result is not None
    assert job.result.success is False
    assert job.error is not None
    assert "artifact" in job.error.lower() or "missing" in job.error.lower()
    assert repo.create_calls == 0


def test_conversion_service_accepts_packaged_output_without_chapter_mp3_artifacts(tmp_path):
    repo = _RepositoryStub()
    source_path = tmp_path / "book.pdf"
    source_path.write_text("source", encoding="utf-8")
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    packaged_path = tmp_path / "output" / "Packaged Book" / "Packaged Book.m4b"
    packaged_path.parent.mkdir(parents=True, exist_ok=True)
    packaged_path.write_bytes(b"m4b")
    pipeline_result = PipelineConversionResult(
        success=True,
        title="Packaged Book",
        author="Author",
        output_path=packaged_path,
        chapters=[
            ChapterResult(
                chapter_number=1,
                chapter_title="Praise",
                wav_path=tmp_path / "temp" / "chapter_01.wav",
                duration_ms=1000,
            ),
            ChapterResult(
                chapter_number=2,
                chapter_title="Copyright Page",
                wav_path=tmp_path / "temp" / "chapter_02.wav",
                duration_ms=2000,
            ),
        ],
        total_duration_ms=3000,
    )
    pipeline_cls = lambda **kwargs: _PipelineStub(result=pipeline_result, **kwargs)  # noqa: E731
    service = ConversionService(
        config=config,
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=pipeline_cls,
    )

    job = _start_conversion_and_wait(
        service,
        source_path=source_path,
        title="Packaged Book",
        author="Author",
    )

    assert job.status == JobStatus.COMPLETED
    assert job.result is not None
    assert job.result.success is True
    assert job.result.chapters_completed == 2
    assert repo.create_calls == 1
    assert repo.created_book.playback_path == str(packaged_path)
    assert repo.created_book.playback_status == PlaybackStatus.PACKAGED
    assert [chapter.mp3_path for chapter in repo.created_chapters] == [None, None]


def test_conversion_service_rejects_missing_packaged_output_success(tmp_path):
    repo = _RepositoryStub()
    source_path = tmp_path / "book.pdf"
    source_path.write_text("source", encoding="utf-8")
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    missing_packaged_path = tmp_path / "output" / "Missing Book" / "Missing Book.m4b"
    pipeline_result = PipelineConversionResult(
        success=True,
        title="Missing Book",
        author="Author",
        output_path=missing_packaged_path,
        chapters=[
            ChapterResult(
                chapter_number=1,
                chapter_title="One",
                wav_path=tmp_path / "temp" / "chapter_01.wav",
                duration_ms=1000,
            )
        ],
        total_duration_ms=1000,
    )
    pipeline_cls = lambda **kwargs: _PipelineStub(result=pipeline_result, **kwargs)  # noqa: E731
    service = ConversionService(
        config=config,
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=pipeline_cls,
    )

    job = _start_conversion_and_wait(
        service,
        source_path=source_path,
        title="Missing Book",
        author="Author",
    )

    assert job.status == JobStatus.FAILED
    assert job.book_id is None
    assert job.result is not None
    assert job.result.success is False
    assert job.error is not None
    assert str(missing_packaged_path) in job.error
    assert repo.create_calls == 0
    assert repo.created_book is None


def test_conversion_service_worker_crash_failure_does_not_persist_library_records(tmp_path):
    repo = _RepositoryStub()
    source_path = tmp_path / "book.pdf"
    source_path.write_text("source", encoding="utf-8")
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )

    class _WorkerCrashPipeline:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            _ = kwargs

        def convert(self, **kwargs):  # noqa: ANN003, ANN201
            _ = kwargs
            raise RuntimeError("worker process crashed during fallback synthesis")

    service = ConversionService(
        config=config,
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=_WorkerCrashPipeline,
    )

    job = _start_conversion_and_wait(
        service,
        source_path=source_path,
        title="Worker Crash Book",
        author="Author",
    )

    assert job.status == JobStatus.FAILED
    assert job.book_id is None
    assert job.result is not None
    assert job.result.success is False
    error_text = str(job.error).lower()
    assert "worker" in error_text
    assert "crash" in error_text or "infrastructure" in error_text or "fallback" in error_text
    assert repo.create_calls == 0
    assert repo.created_book is None
    assert repo.created_chapters is None


def test_conversion_service_broken_parallel_worker_results_fallback_failure_does_not_persist_library_records(
    tmp_path,
):
    source_path = tmp_path / "broken-pool-source.txt"
    source_path.write_text("synthetic source", encoding="utf-8")
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    repo = SQLiteRepository(
        config.db_path,
        output_dir=config.output_dir,
        temp_dir=config.temp_dir,
    )

    class _PermissiveMemoryPolicy:
        enable_warmup_calibration = False
        safe_mode_reason = None

        def allowed_workers(self, requested_max: int) -> int:
            return requested_max

    class _BrokenPoolResultManager:
        def run_parallel(self, **kwargs):  # noqa: ANN003, ANN201
            chapters = kwargs["chapters"]
            return [
                ChapterResult(
                    chapter_number=chapter.number,
                    chapter_title=chapter.title,
                    error=(
                        "BrokenProcessPool: a process in the process pool was terminated abruptly"
                    ),
                )
                for chapter in chapters
            ]

    class _BrokenPoolFallbackPipeline(ConversionPipeline):
        package_calls = 0
        fallback_calls = 0

        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            super().__init__(*args, **kwargs)
            self.config.enable_pipelining = False
            self.stages.ingest = self.stage_ingest
            self.stages.warmup_models = self.stage_warmup_models
            self.stages.process_chapter = self.stage_process_chapter
            self.stages._chapter_processor.process_chapter = self.stage_process_chapter  # noqa: SLF001
            self.stages.package = self.stage_package

        def stage_ingest(self, source_path):  # noqa: ANN001, ANN201
            _ = source_path
            chapter_text = "alpha " * 12000
            return SimpleNamespace(
                title="Broken Pool Book",
                author="Author",
                chapters=[
                    SimpleNamespace(number=1, title="One", content=chapter_text),
                    SimpleNamespace(number=2, title="Two", content=chapter_text),
                ],
            )

        def stage_warmup_models(self) -> None:
            return None

        def stage_process_chapter(
            self, chapter, chapter_idx: int, total_chapters: int, output_dir: Path
        ):  # noqa: ANN001, ANN201
            _ = chapter, chapter_idx, total_chapters, output_dir
            type(self).fallback_calls += 1
            raise RuntimeError("sequential fallback failed without real TTS")

        def stage_package(self, chapter_results, title, author, output_dir):  # noqa: ANN001, ANN201
            _ = chapter_results, title, author, output_dir
            type(self).package_calls += 1
            package_path = output_dir / "should-not-exist.m4b"
            package_path.write_bytes(b"unexpected")
            return package_path

    state_events = []

    def _on_event(event) -> None:
        if getattr(event, "event_type", None) == EventType.STATE:
            state_events.append(event.state)

    service = ConversionService(
        config=config,
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=_BrokenPoolResultManager(),
        pipeline_cls=_BrokenPoolFallbackPipeline,
    )

    try:
        job = _start_conversion_and_wait(
            service,
            source_path=source_path,
            runtime_options=_default_runtime_options(
                parallel_worker_mode="manual",
                enable_parallel=True,
                max_parallel_chapters=2,
            ),
            callbacks=ConversionCallbacks(on_event=_on_event),
            title="Broken Pool Book",
            author="Author",
        )

        assert _BrokenPoolFallbackPipeline.fallback_calls >= 1
        assert _BrokenPoolFallbackPipeline.package_calls == 0
        assert not any(config.output_dir.rglob("*.m4b"))
        assert job.status == JobStatus.FAILED
        assert job.book_id is None
        assert job.result is not None
        assert job.result.success is False
        error_text = str(job.error).lower()
        assert "brokenprocesspool" in error_text
        assert "process pool was terminated abruptly" in error_text
        assert "fallback" in error_text

        with sqlite3.connect(config.db_path) as conn:
            book_count = conn.execute(
                "SELECT COUNT(*) FROM books WHERE title = ? OR source_path = ?",
                ("Broken Pool Book", str(source_path)),
            ).fetchone()[0]
            chapter_count = conn.execute("SELECT COUNT(*) FROM chapters").fetchone()[0]

        assert book_count == 0
        assert chapter_count == 0
        assert state_events[-1] == JobState.FAILED
        assert JobState.COMPLETED not in state_events
        assert JobState.CANCELLED not in state_events
    finally:
        repo.close()


def test_conversion_service_accepts_cancel_before_pipeline_is_attached(tmp_path):
    source_path = tmp_path / "book.epub"
    source_path.write_text("dummy")

    class _BlockingInitPipeline:
        init_started = threading.Event()
        release_init = threading.Event()
        convert_called = threading.Event()
        cancel_seen = threading.Event()

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            _ = kwargs
            type(self).init_started.set()
            assert type(self).release_init.wait(timeout=3), "pipeline init release timed out"
            self._cancelled = False

        def cancel(self) -> None:
            self._cancelled = True
            type(self).cancel_seen.set()

        def convert(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            _ = kwargs
            type(self).convert_called.set()
            if self._cancelled:
                return PipelineConversionResult(
                    success=False, title="Book", author="Author", error="Cancelled"
                )
            return PipelineConversionResult(
                success=False, title="Book", author="Author", error="not cancelled"
            )

    state_events = []

    def _on_event(event) -> None:
        if getattr(event, "event_type", None) == EventType.STATE:
            state_events.append(event.state)

    service = _make_service(
        tmp_path, _BlockingInitPipeline, job_cls=ConversionJob, status_enum=JobStatus
    )
    job = service.start_conversion(
        source_path=source_path,
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
        ),
        callbacks=ConversionCallbacks(on_event=_on_event),
    )

    assert _BlockingInitPipeline.init_started.wait(timeout=1), "pipeline init did not start"
    try:
        assert service.cancel_conversion() is True
    finally:
        _BlockingInitPipeline.release_init.set()
        job.wait(timeout=3)

    assert _BlockingInitPipeline.cancel_seen.is_set()
    assert not _BlockingInitPipeline.convert_called.is_set()
    assert job.status == JobStatus.CANCELLED
    assert state_events[-1] == JobState.CANCELLED


def test_conversion_service_accepts_cancel_for_pending_active_job(tmp_path, monkeypatch):
    source_path = tmp_path / "book.md"
    source_path.write_text("text", encoding="utf-8")

    class _PendingJob(ConversionJob):
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            super().__init__(**kwargs)
            self.pipeline = MagicMock()

        def wait(self, timeout=None) -> bool:  # noqa: ANN001
            _ = timeout
            return True

    monkeypatch.setattr(threading.Thread, "start", lambda thread: None)
    service = _make_service(tmp_path, _PipelineStub, job_cls=_PendingJob, status_enum=JobStatus)
    pending_job = service.start_conversion(
        source_path=source_path,
        runtime_options=ConversionRuntimeOptions(voice="af_bella", speed=1.0),
    )
    pending_job.status = JobStatus.IDLE

    assert service.cancel_conversion() is True
    assert pending_job.cancel_requested is True
    pending_job.pipeline.cancel.assert_called_once()

    pending_job.cancel_requested = False
    pending_job.pipeline.reset_mock()

    assert service.cancel_active_job(timeout_seconds=0.0) is True
    assert pending_job.cancel_requested is True
    pending_job.pipeline.cancel.assert_called_once()


def test_conversion_service_copies_temp_web_upload_before_worker_starts(tmp_path, monkeypatch):
    service = _make_service(tmp_path, _PipelineStub)
    upload_dir = service._config.temp_dir / "web_uploads"  # noqa: SLF001 - upload boundary coverage
    upload_dir.mkdir(parents=True)
    source_path = upload_dir / "book.epub"
    source_path.write_text("uploaded source", encoding="utf-8")
    durable_path = Path(service._config.db_path).parent / "sources" / source_path.name  # noqa: SLF001
    started_with = {}

    def _start_noop(thread):  # noqa: ANN001, ANN202
        queued_source_path = thread._kwargs["source_path"]  # noqa: SLF001 - assert queued worker input
        started_with["source_path"] = queued_source_path
        assert durable_path.is_file()
        assert durable_path.read_text(encoding="utf-8") == "uploaded source"
        assert queued_source_path == durable_path

    monkeypatch.setattr(threading.Thread, "start", _start_noop)

    service.start_conversion(
        source_path=source_path,
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
        ),
    )

    assert started_with["source_path"] == durable_path
    assert source_path.is_file()
    assert durable_path.is_file()


def test_conversion_service_does_not_copy_temp_web_upload_when_active_job_rejects(tmp_path, monkeypatch):
    service = _make_service(tmp_path, _PipelineStub)
    active_source_path = tmp_path / "active.md"
    active_source_path.write_text("active source", encoding="utf-8")
    monkeypatch.setattr(threading.Thread, "start", lambda thread: None)
    service.start_conversion(
        source_path=active_source_path,
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
        ),
    )
    upload_dir = service._config.temp_dir / "web_uploads"  # noqa: SLF001 - upload boundary coverage
    upload_dir.mkdir(parents=True)
    source_path = upload_dir / "book.epub"
    source_path.write_text("uploaded source", encoding="utf-8")
    durable_path = Path(service._config.db_path).parent / "sources" / source_path.name  # noqa: SLF001

    with pytest.raises(RuntimeError, match="A conversion is already in progress"):
        service.start_conversion(
            source_path=source_path,
            runtime_options=ConversionRuntimeOptions(
                voice="af_bella",
                speed=1.0,
            ),
        )

    assert source_path.is_file()
    assert not durable_path.exists()


def test_conversion_service_keeps_external_source_path_when_worker_starts(tmp_path, monkeypatch):
    service = _make_service(tmp_path, _PipelineStub)
    source_path = tmp_path / "external" / "book.epub"
    source_path.parent.mkdir()
    source_path.write_text("external source", encoding="utf-8")
    durable_path = Path(service._config.db_path).parent / "sources" / source_path.name  # noqa: SLF001
    started_with = {}

    def _start_noop(thread):  # noqa: ANN001, ANN202
        queued_source_path = thread._kwargs["source_path"]  # noqa: SLF001 - assert queued worker input
        started_with["source_path"] = queued_source_path
        assert queued_source_path == source_path

    monkeypatch.setattr(threading.Thread, "start", _start_noop)

    service.start_conversion(
        source_path=source_path,
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
        ),
    )

    assert started_with["source_path"] == source_path
    assert not durable_path.exists()


def test_conversion_service_persists_durable_source_path_for_temp_web_upload(tmp_path, monkeypatch):
    upload_source = tmp_path / "temp" / "web_uploads" / "book.epub"
    upload_source.parent.mkdir(parents=True)
    upload_source.write_text("uploaded source", encoding="utf-8")
    durable_path = tmp_path / "data" / "sources" / upload_source.name
    packaged_path = tmp_path / "output" / "Uploaded Book" / "Uploaded Book.m4b"
    repo = _RepositoryStub()

    class _SuccessfulPipeline:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            _ = kwargs

        def convert(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            assert kwargs["source_path"] == durable_path
            packaged_path.parent.mkdir(parents=True, exist_ok=True)
            packaged_path.write_bytes(b"m4b")
            return PipelineConversionResult(
                success=True,
                title="Uploaded Book",
                author="Author",
                output_path=packaged_path,
                chapters=[
                    ChapterResult(
                        chapter_number=1,
                        chapter_title="One",
                        wav_path=tmp_path / "temp" / "chapter_01.wav",
                        duration_ms=1000,
                    )
                ],
                total_duration_ms=1000,
            )

    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    service = ConversionService(
        config=config,
        repository=repo,
        job_cls=SimpleNamespace,
        status_enum=_JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=_SuccessfulPipeline,
    )
    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda thread: thread._target(*thread._args, **thread._kwargs),  # noqa: SLF001
    )

    job = service.start_conversion(
        source_path=upload_source,
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
        ),
    )

    assert job.status == _JobStatus.COMPLETED
    assert repo.created_book.source_path == str(durable_path)
    assert upload_source.is_file()
    assert durable_path.is_file()


def test_conversion_service_uses_unique_durable_paths_for_duplicate_upload_names(
    tmp_path, monkeypatch
):
    upload_source = tmp_path / "temp" / "web_uploads" / "book.epub"
    upload_source.parent.mkdir(parents=True)
    packaged_root = tmp_path / "output"
    created_books = []

    class _RecordingRepo:
        def create_book_with_chapters(self, book, chapters):  # noqa: ANN001, ANN201
            _ = chapters
            created_books.append(book)
            return SimpleNamespace(id=len(created_books)), []

    class _SuccessfulPipeline:
        calls = 0

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            _ = kwargs

        def convert(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            _ = kwargs
            type(self).calls += 1
            packaged_path = packaged_root / f"run_{type(self).calls}" / "book.m4b"
            packaged_path.parent.mkdir(parents=True, exist_ok=True)
            packaged_path.write_bytes(b"m4b")
            return PipelineConversionResult(
                success=True,
                title=f"Uploaded Book {type(self).calls}",
                author="Author",
                output_path=packaged_path,
                chapters=[
                    ChapterResult(
                        chapter_number=1,
                        chapter_title="One",
                        wav_path=tmp_path / "temp" / f"chapter_{type(self).calls:02d}.wav",
                        duration_ms=1000,
                    )
                ],
                total_duration_ms=1000,
            )

    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=packaged_root,
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    service = ConversionService(
        config=config,
        repository=_RecordingRepo(),
        job_cls=SimpleNamespace,
        status_enum=_JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=_SuccessfulPipeline,
    )
    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda thread: thread._target(*thread._args, **thread._kwargs),  # noqa: SLF001
    )

    upload_source.write_text("first upload", encoding="utf-8")
    first_job = service.start_conversion(
        source_path=upload_source,
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
        ),
    )
    first_durable_path = Path(created_books[0].source_path)

    upload_source.write_text("second upload", encoding="utf-8")
    second_job = service.start_conversion(
        source_path=upload_source,
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
        ),
    )
    second_durable_path = Path(created_books[1].source_path)

    assert first_job.status == _JobStatus.COMPLETED
    assert second_job.status == _JobStatus.COMPLETED
    assert first_durable_path != second_durable_path
    assert first_durable_path.name == "book.epub"
    assert second_durable_path.name == "book_1.epub"
    assert first_durable_path.is_file()
    assert second_durable_path.is_file()
    assert first_durable_path.read_text(encoding="utf-8") == "first upload"
    assert second_durable_path.read_text(encoding="utf-8") == "second upload"


def test_conversion_service_retries_durable_path_when_exclusive_create_loses_race(
    tmp_path, monkeypatch
):
    service = _make_service(tmp_path, _PipelineStub)
    upload_source = service._config.temp_dir / "web_uploads" / "book.epub"  # noqa: SLF001
    upload_source.parent.mkdir(parents=True)
    upload_source.write_text("raced upload", encoding="utf-8")
    base_durable_path = Path(service._config.db_path).parent / "sources" / "book.epub"  # noqa: SLF001
    retry_durable_path = Path(service._config.db_path).parent / "sources" / "book_1.epub"  # noqa: SLF001
    started_with = {}
    original_open = Path.open

    def _racing_open(path, *args, **kwargs):  # noqa: ANN001, ANN202
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == base_durable_path and "x" in mode:
            raise FileExistsError("simulated durable source allocation race")
        return original_open(path, *args, **kwargs)

    def _start_noop(thread):  # noqa: ANN001, ANN202
        started_with["source_path"] = thread._kwargs["source_path"]  # noqa: SLF001

    monkeypatch.setattr(Path, "open", _racing_open)
    monkeypatch.setattr(threading.Thread, "start", _start_noop)

    service.start_conversion(
        source_path=upload_source,
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
        ),
    )

    assert started_with["source_path"] == retry_durable_path
    assert not base_durable_path.exists()
    assert retry_durable_path.read_text(encoding="utf-8") == "raced upload"


def test_conversion_service_finishes_user_cancel_as_cancelled(tmp_path):
    source_path = tmp_path / "book.epub"
    source_path.write_text("dummy")

    class _CancellablePipeline:
        convert_started = threading.Event()

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            _ = kwargs
            self._cancelled = False

        def cancel(self) -> None:
            self._cancelled = True

        def convert(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            _ = kwargs
            type(self).convert_started.set()
            deadline = time.time() + 3
            while time.time() < deadline and not self._cancelled:
                time.sleep(0.01)
            assert self._cancelled, "cancel was not forwarded to the pipeline"
            return PipelineConversionResult(
                success=False, title="Book", author="Author", error="Cancelled"
            )

    state_events = []

    def _on_event(event) -> None:
        if getattr(event, "event_type", None) == EventType.STATE:
            state_events.append(event.state)

    service = _make_service(
        tmp_path, _CancellablePipeline, job_cls=ConversionJob, status_enum=JobStatus
    )
    job = service.start_conversion(
        source_path=source_path,
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
        ),
        callbacks=ConversionCallbacks(on_event=_on_event),
    )

    assert _CancellablePipeline.convert_started.wait(timeout=1), "pipeline convert did not start"
    assert service.cancel_conversion() is True
    assert job.wait(timeout=3) is True
    assert job.status == JobStatus.CANCELLED
    assert state_events[-1] == JobState.CANCELLED


def test_conversion_service_late_cancel_after_packaging_persists_completed_output(tmp_path):
    source_path = tmp_path / "book.epub"
    source_path.write_text("dummy")
    packaged_path = tmp_path / "output" / "Late Cancel Book" / "Late Cancel Book.m4b"
    repo = _RepositoryStub()

    class _PackagedThenCancellablePipeline:
        packaged_ready = threading.Event()
        cancel_seen = threading.Event()

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            _ = kwargs

        def cancel(self) -> None:
            type(self).cancel_seen.set()

        def convert(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            _ = kwargs
            packaged_path.parent.mkdir(parents=True, exist_ok=True)
            packaged_path.write_bytes(b"m4b")
            assert packaged_path.is_file()
            type(self).packaged_ready.set()
            assert type(self).cancel_seen.wait(timeout=3), "cancel was not issued after packaging"
            return PipelineConversionResult(
                success=True,
                title="Late Cancel Book",
                author="Author",
                output_path=packaged_path,
                chapters=[
                    ChapterResult(
                        chapter_number=1,
                        chapter_title="One",
                        wav_path=tmp_path / "temp" / "chapter_01.wav",
                        duration_ms=1000,
                    )
                ],
                total_duration_ms=1000,
            )

    state_events = []

    def _on_event(event) -> None:
        if getattr(event, "event_type", None) == EventType.STATE:
            state_events.append(event.state)

    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    service = ConversionService(
        config=config,
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=_PackagedThenCancellablePipeline,
    )

    job = service.start_conversion(
        source_path=source_path,
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
        ),
        callbacks=ConversionCallbacks(on_event=_on_event),
        title="Late Cancel Book",
        author="Author",
    )

    assert _PackagedThenCancellablePipeline.packaged_ready.wait(timeout=1), (
        "packaged output was not created"
    )
    assert packaged_path.is_file()
    assert service.cancel_conversion() is True
    assert job.wait(timeout=3) is True
    assert job.status == JobStatus.COMPLETED
    assert job.book_id is not None
    assert job.result is not None
    assert job.result.success is True
    assert repo.create_calls == 1
    assert repo.created_book.playback_path == str(packaged_path)
    assert repo.created_book.playback_status == PlaybackStatus.PACKAGED
    assert state_events[-1] == JobState.COMPLETED
    assert JobState.CANCELLED not in state_events


def test_conversion_service_generates_unique_job_ids_within_same_second(monkeypatch, tmp_path):
    source_path = tmp_path / "book.epub"
    source_path.write_text("dummy")
    monkeypatch.setattr(time, "time", lambda: 1234.0)

    class _FastFailPipeline:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            _ = kwargs

        def convert(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            _ = kwargs
            return PipelineConversionResult(
                success=False, title="Book", author="Author", error="done"
            )

    service = _make_service(
        tmp_path, _FastFailPipeline, job_cls=ConversionJob, status_enum=JobStatus
    )

    first = service.start_conversion(
        source_path=source_path,
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
        ),
    )
    assert first.wait(timeout=3)
    second = service.start_conversion(
        source_path=source_path,
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
        ),
    )
    assert second.wait(timeout=3)

    assert first.id.startswith("conv_")
    assert second.id.startswith("conv_")
    assert first.id != second.id


def test_conversion_service_rejects_second_start_while_job_is_active(tmp_path):
    source_path = tmp_path / "book.epub"
    source_path.write_text("dummy")

    class _BlockingPipeline:
        convert_started = threading.Event()
        release_convert = threading.Event()

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            _ = kwargs

        def convert(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            _ = kwargs
            type(self).convert_started.set()
            type(self).release_convert.wait(timeout=3)
            return PipelineConversionResult(
                success=False,
                title="Book",
                author="Author",
                error="released",
            )

    service = _make_service(
        tmp_path, _BlockingPipeline, job_cls=ConversionJob, status_enum=JobStatus
    )
    job = service.start_conversion(
        source_path=source_path,
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
        ),
    )
    assert _BlockingPipeline.convert_started.wait(timeout=1), "pipeline convert did not start"

    try:
        with pytest.raises(RuntimeError, match="A conversion is already in progress"):
            service.start_conversion(
                source_path=source_path,
                runtime_options=ConversionRuntimeOptions(
                    voice="af_bella",
                    speed=1.0,
                ),
            )
    finally:
        _BlockingPipeline.release_convert.set()
        job.wait(timeout=3)


def test_conversion_service_generates_unique_repair_ids_within_same_second(monkeypatch, tmp_path):
    monkeypatch.setattr(time, "time", lambda: 1234.0)

    service = _make_service(tmp_path, _PipelineStub, job_cls=ConversionJob, status_enum=JobStatus)

    first = service.start_repair(
        book_id=77,
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
        ),
    )
    assert first.wait(timeout=3)
    second = service.start_repair(
        book_id=77,
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
        ),
    )
    assert second.wait(timeout=3)

    assert first.id.startswith("repair_77_")
    assert second.id.startswith("repair_77_")
    assert first.id != second.id


def test_conversion_service_cancel_active_job_wait_is_bounded(tmp_path):
    source_path = tmp_path / "book.epub"
    source_path.write_text("dummy")

    class _BlockingPipeline:
        convert_started = threading.Event()
        cancel_seen = threading.Event()
        release_convert = threading.Event()

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            _ = kwargs

        def cancel(self) -> None:
            type(self).cancel_seen.set()

        def convert(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            _ = kwargs
            type(self).convert_started.set()
            assert type(self).release_convert.wait(timeout=3), "convert release timed out"
            return PipelineConversionResult(
                success=False, title="Book", author="Author", error="Cancelled"
            )

    service = _make_service(
        tmp_path, _BlockingPipeline, job_cls=ConversionJob, status_enum=JobStatus
    )
    job = service.start_conversion(
        source_path=source_path,
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
        ),
    )

    assert _BlockingPipeline.convert_started.wait(timeout=1), "pipeline convert did not start"
    started_at = time.perf_counter()
    try:
        finished = service.cancel_active_job(timeout_seconds=0.05)
    finally:
        elapsed = time.perf_counter() - started_at
        _BlockingPipeline.release_convert.set()
        job.wait(timeout=3)

    assert finished is False
    assert elapsed < 0.5
    assert _BlockingPipeline.cancel_seen.is_set()


def test_conversion_service_cancel_shuts_down_shared_worker_pool(tmp_path):
    source_path = tmp_path / "book.epub"
    source_path.write_text("dummy")

    class _BlockingPipeline:
        convert_started = threading.Event()
        release_convert = threading.Event()

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            _ = kwargs

        def cancel(self) -> None:
            return None

        def convert(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            _ = kwargs
            type(self).convert_started.set()
            assert type(self).release_convert.wait(timeout=3), "convert release timed out"
            return PipelineConversionResult(
                success=False, title="Book", author="Author", error="Cancelled"
            )

    class _SharedWorkerPoolManager:
        def __init__(self) -> None:
            self.shutdown_calls = []

        def shutdown(self, *, timeout_seconds=None, cancel_futures=True):  # noqa: ANN001
            self.shutdown_calls.append((timeout_seconds, cancel_futures))
            return True

        @staticmethod
        def snapshot() -> dict[str, int]:
            return {}

    shared_worker_pool_manager = _SharedWorkerPoolManager()
    service = _make_service(
        tmp_path,
        _BlockingPipeline,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        shared_worker_pool_manager=shared_worker_pool_manager,
    )
    job = service.start_conversion(
        source_path=source_path,
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
        ),
    )

    assert _BlockingPipeline.convert_started.wait(timeout=1), "pipeline convert did not start"
    try:
        assert service.cancel_conversion() is True
        assert shared_worker_pool_manager.shutdown_calls == [(0.0, True)]
    finally:
        _BlockingPipeline.release_convert.set()
        job.wait(timeout=3)


def test_conversion_service_cancel_terminates_workers_when_executor_clears_process_map(tmp_path):
    source_path = tmp_path / "book.epub"
    source_path.write_text("dummy")

    class _FakeWorkerProcess:
        def __init__(self) -> None:
            self.terminated = False
            self.killed = False
            self.join_calls = 0

        def is_alive(self) -> bool:
            return not self.terminated and not self.killed

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def join(self, timeout: float | None = None) -> None:
            _ = timeout
            self.join_calls += 1

    class _ClearingShutdownExecutor:
        instances: list["_ClearingShutdownExecutor"] = []

        def __init__(self, *args, **kwargs):  # noqa: ANN003
            _ = args, kwargs
            self.worker = _FakeWorkerProcess()
            self._processes = {1: self.worker}
            type(self).instances.append(self)

        def shutdown(self, wait: bool = True, cancel_futures: bool = True):  # noqa: ARG002
            self._processes = None

    class _BlockingPipeline:
        convert_started = threading.Event()
        release_convert = threading.Event()
        cancel_seen = threading.Event()

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            _ = kwargs

        def cancel(self) -> None:
            type(self).cancel_seen.set()

        def convert(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            _ = kwargs
            type(self).convert_started.set()
            assert type(self).release_convert.wait(timeout=3), "convert release timed out"
            return PipelineConversionResult(
                success=False, title="Book", author="Author", error="Cancelled"
            )

    with patch("modules.pipeline.pool_manager.ProcessPoolExecutor", _ClearingShutdownExecutor):
        shared_worker_pool_manager = SharedWorkerPoolManager(max_workers=2)
        shared_worker_pool_manager.ensure_started(
            "cancel-clearing-shutdown",
            worker_init_config={"voice": "af_bella"},
        )
        worker = _ClearingShutdownExecutor.instances[0].worker
        service = _make_service(
            tmp_path,
            _BlockingPipeline,
            job_cls=ConversionJob,
            status_enum=JobStatus,
            shared_worker_pool_manager=shared_worker_pool_manager,
        )
        job = service.start_conversion(
            source_path=source_path,
            runtime_options=ConversionRuntimeOptions(
                voice="af_bella",
                speed=1.0,
            ),
        )

        assert _BlockingPipeline.convert_started.wait(timeout=1), "pipeline convert did not start"
        try:
            assert service.cancel_conversion() is True
            assert _BlockingPipeline.cancel_seen.is_set()
            assert worker.terminated is True
            assert worker.join_calls >= 1
        finally:
            _BlockingPipeline.release_convert.set()
            job.wait(timeout=3)

    assert job.status == JobStatus.CANCELLED


def test_conversion_service_throttles_progress_callbacks_to_stage_boundaries_and_interval(
    tmp_path,
):
    service = _make_service(tmp_path, _PipelineStub)
    progress_calls = []
    stage_changes = []
    progress_events = []
    callbacks = ConversionCallbacks(
        on_progress=lambda stage, progress, message: progress_calls.append(
            (stage, progress, message)
        ),
        on_stage_change=stage_changes.append,
        on_event=lambda event: progress_events.append(event),
    )
    on_progress, _ = service._create_pipeline_callbacks(callbacks)

    with patch(
        "modules.app.services.conversion_service.time.monotonic",
        side_effect=[0.0, 0.05, 0.10, 0.35, 0.36],
    ):
        on_progress(SimpleNamespace(value="cleaning"), 1, 4, 1, 4, "clean start", None)
        on_progress(SimpleNamespace(value="cleaning"), 1, 4, 2, 4, "clean mid", None)
        on_progress(SimpleNamespace(value="synthesizing"), 2, 4, 1, 4, "synth start", None)
        on_progress(SimpleNamespace(value="synthesizing"), 2, 4, 2, 4, "synth mid", None)
        on_progress(SimpleNamespace(value="complete"), 4, 4, 4, 4, "done", None)

    assert [call[0] for call in progress_calls] == [
        "cleaning",
        "synthesizing",
        "synthesizing",
        "complete",
    ]
    assert [call[2] for call in progress_calls] == [
        "clean start",
        "synth start",
        "synth mid",
        "done",
    ]
    assert stage_changes == ["cleaning", "synthesizing", "complete"]
    assert [event.event_type for event in progress_events] == [EventType.PROGRESS] * 4
    assert [event.message for event in progress_events] == [
        "clean start",
        "synth start",
        "synth mid",
        "done",
    ]


def test_conversion_service_progress_events_include_explicit_overall_and_stage_progress(
    tmp_path,
):
    service = _make_service(tmp_path, _PipelineStub)
    progress_events = []
    callbacks = ConversionCallbacks(
        on_event=lambda event: progress_events.append(event),
    )
    on_progress, _ = service._create_pipeline_callbacks(callbacks)

    with patch(
        "modules.app.services.conversion_service.time.monotonic",
        side_effect=[0.0],
    ):
        on_progress(SimpleNamespace(value="cleaning"), 0, 1, 0, 1, "clean start", 12.5)

    event = progress_events[-1]
    assert event.event_type == EventType.PROGRESS
    assert event.stage == "cleaning"
    assert event.message == "clean start"
    assert event.progress == event.overall_progress
    assert 0.0 < event.overall_progress < 1.0
    assert 0.0 < event.stage_progress <= 1.0
    assert event.eta is None


def test_conversion_service_progress_events_include_subtask_counters(tmp_path):
    service = _make_service(tmp_path, _PipelineStub)
    progress_events = []
    callbacks = ConversionCallbacks(
        on_event=lambda event: progress_events.append(event),
    )
    on_progress, _ = service._create_pipeline_callbacks(callbacks)

    with patch(
        "modules.app.services.conversion_service.time.monotonic",
        side_effect=[0.0],
    ):
        on_progress(SimpleNamespace(value="synthesizing"), 1, 4, 2, 8, "synth chunk", None)

    event = progress_events[-1]
    assert event.chapter_current == 2
    assert event.chapter_total == 4
    assert event.chunk_current == 3
    assert event.chunk_total == 8


def test_conversion_service_progress_events_forward_performance_payload(tmp_path):
    service = _make_service(tmp_path, _PipelineStub)
    progress_events = []
    callbacks = ConversionCallbacks(
        on_event=lambda event: progress_events.append(event),
    )
    on_progress, _ = service._create_pipeline_callbacks(callbacks)

    with patch(
        "modules.app.services.conversion_service.time.monotonic",
        side_effect=[0.0],
    ):
        on_progress(
            SimpleNamespace(value="synthesizing"),
            1,
            4,
            2,
            8,
            "synth chunk",
            None,
            {
                "pipeline_mode": "parallel",
                "active_workers": 2,
                "max_workers": 2,
                "queued_chapters": 4,
                "running_chapters": 2,
                "completed_chapters": 1,
                "failed_chapters": 0,
                "active_chapter_titles": ["One", "Two"],
                "cleaner_chunks_per_second": 8.5,
                "tts_chars_per_second": 1200.0,
                "encoder_chapters_per_second": 0.4,
                "elapsed_seconds": 10.0,
            },
        )

    event = progress_events[-1]
    assert event.performance["pipeline_mode"] == "parallel"
    assert event.performance["active_workers"] == 2
    assert event.performance["queued_chapters"] == 4
    assert event.performance["active_chapter_titles"] == ["One", "Two"]
    assert event.performance["tts_chars_per_second"] == 1200.0


def test_conversion_service_progress_events_use_stage_weighted_eta(tmp_path):
    service = _make_service(tmp_path, _PipelineStub)
    progress_events = []
    callbacks = ConversionCallbacks(
        on_event=lambda event: progress_events.append(event),
    )
    on_progress, _ = service._create_pipeline_callbacks(callbacks)

    with patch(
        "modules.app.services.conversion_service.time.monotonic",
        side_effect=[10.0, 20.0],
    ):
        on_progress(SimpleNamespace(value="ingesting"), 0, 1, 0, 0, "parse start", 999.0)
        on_progress(SimpleNamespace(value="cleaning"), 0, 1, 0, 1, "clean start", 999.0)

    event = progress_events[-1]
    assert event.event_type == EventType.PROGRESS
    assert event.stage == "cleaning"
    assert event.eta == pytest.approx((10.0 * (1.0 - 0.30)) / 0.30)
    assert event.eta != 999.0


def test_conversion_service_single_chapter_progress_moves_before_packaging(tmp_path):
    service = _make_service(tmp_path, _PipelineStub)
    progress_calls = []
    callbacks = ConversionCallbacks(
        on_progress=lambda stage, progress, message: progress_calls.append(
            (stage, progress, message)
        ),
    )
    on_progress, _ = service._create_pipeline_callbacks(callbacks)

    with patch(
        "modules.app.services.conversion_service.time.monotonic",
        side_effect=[0.0, 0.3, 0.6],
    ):
        on_progress(SimpleNamespace(value="cleaning"), 0, 1, 0, 1, "clean start", None)
        on_progress(SimpleNamespace(value="synthesizing"), 0, 1, 0, 1, "synth start", None)
        on_progress(SimpleNamespace(value="encoding"), 0, 1, 0, 0, "encode start", None)

    assert [call[0] for call in progress_calls] == ["cleaning", "synthesizing", "encoding"]
    assert all(0.0 < progress < 1.0 for _, progress, _ in progress_calls)
    assert [call[1] for call in progress_calls] == sorted(call[1] for call in progress_calls)


def test_conversion_service_validates_quantization_from_tts_options(tmp_path):
    source_path = tmp_path / "book.epub"
    source_path.write_text("dummy")
    calls = []

    def _validator(engine: str, quantization: str | None, *, runtime_spec=None) -> None:
        calls.append((engine, quantization))
        raise RuntimeError(f"validated {engine}/{quantization}")

    service = _make_service_with_validator(tmp_path, _validator)

    with pytest.raises(RuntimeError, match="validated kokoro/bf16"):
        service.start_conversion(
            source_path=source_path,
            runtime_options=ConversionRuntimeOptions(
                voice="am_adam",
                speed=1.0,
                tts_engine="kokoro",
                tts_quantization=None,
                tts_options={"quantization": "bf16"},
            ),
        )

    assert calls == [("kokoro", "bf16")]


def test_conversion_service_passes_canonical_runtime_spec_to_validation_and_config(tmp_path):
    runtime_specs = []

    def _validator(engine: str, quantization: str | None, *, runtime_spec=None) -> None:
        runtime_specs.append((engine, quantization, runtime_spec))

    service = _make_service_with_validator(tmp_path, _validator)

    pipeline_config = service._create_pipeline_config(  # noqa: SLF001
        runtime_options=ConversionRuntimeOptions(
            tts_engine="kokoro",
            tts_quantization="bf16",
            tts_options={
                "dtype": "fp16",
                "model_name": "Kokoro-82M",
            },
        )
    )
    service._validate_requested_tts_runtime(  # noqa: SLF001
        runtime_options=ConversionRuntimeOptions(
            tts_engine="kokoro",
            tts_quantization="bf16",
            tts_options={
                "dtype": "fp16",
                "model_name": "Kokoro-82M",
            },
        )
    )

    assert pipeline_config.tts_runtime is not None
    assert pipeline_config.tts_runtime.provider == "mlx"
    assert pipeline_config.tts_runtime.device is None
    assert pipeline_config.tts_runtime.options == (("dtype", "fp16"),)
    assert runtime_specs == [
        ("kokoro", "bf16", pipeline_config.tts_runtime),
    ]


def test_conversion_service_rejects_unsupported_kokoro_cuda_before_runtime_load(tmp_path):
    source_path = tmp_path / "book.epub"
    source_path.write_text("dummy")
    runtime_specs = []
    pipeline_inits = []

    def _validator(engine: str, quantization: str | None, *, runtime_spec=None) -> None:
        runtime_specs.append((engine, quantization, runtime_spec))

    class _RecordingPipeline(_PipelineStub):
        def __init__(self, result: PipelineConversionResult, **kwargs) -> None:  # noqa: ANN003
            pipeline_inits.append(kwargs)
            super().__init__(result, **kwargs)

    service = _make_service(
        tmp_path,
        _RecordingPipeline,
    )
    service._runtime_options_resolver._runtime_validator = _validator  # noqa: SLF001

    with pytest.raises(ValueError, match="Kokoro provider 'cuda' is not supported"):
        service.start_conversion(
            source_path=source_path,
            runtime_options=ConversionRuntimeOptions(
                tts_engine="kokoro",
                tts_quantization="bf16",
                tts_provider="cuda",
                tts_device="cuda:0",
                tts_options={"model_name": "Kokoro-82M"},
            ),
        )

    assert runtime_specs == []
    assert pipeline_inits == []


def test_conversion_service_validation_distinguishes_supported_provider_and_device(tmp_path):
    runtime_specs = []

    def _validator(engine: str, quantization: str | None, *, runtime_spec=None) -> None:
        runtime_specs.append((engine, quantization, runtime_spec))

    service = _make_service_with_validator(tmp_path, _validator)
    mlx_options = ConversionRuntimeOptions(
        tts_engine="kokoro",
        tts_quantization="bf16",
        tts_provider="mlx",
        tts_options={"model_name": "Kokoro-82M"},
    )
    explicit_mlx_device_options = ConversionRuntimeOptions(
        tts_engine="kokoro",
        tts_quantization="bf16",
        tts_provider="mlx",
        tts_device="gpu:0",
        tts_options={"model_name": "Kokoro-82M"},
    )

    service._validate_requested_tts_runtime(runtime_options=mlx_options)  # noqa: SLF001
    service._validate_requested_tts_runtime(runtime_options=explicit_mlx_device_options)  # noqa: SLF001

    assert len(runtime_specs) == 2
    first_spec = runtime_specs[0][2]
    second_spec = runtime_specs[1][2]
    assert first_spec.provider == "mlx"
    assert first_spec.device is None
    assert second_spec.provider == "mlx"
    assert second_spec.device == "gpu:0"
    assert first_spec.cache_key() != second_spec.cache_key()


@pytest.mark.parametrize("quantization", ["fp16", "fp32"])
def test_conversion_service_rejects_cuda_precision_quantization_by_provider_before_quantization(
    tmp_path,
    quantization,
):
    runtime_specs = []

    def _validator(engine: str, quantization: str | None, *, runtime_spec=None) -> None:
        runtime_specs.append((engine, quantization, runtime_spec))

    service = _make_service_with_validator(tmp_path, _validator)

    with pytest.raises(ValueError, match="Kokoro provider 'cuda' is not supported"):
        service._validate_requested_tts_runtime(  # noqa: SLF001
            runtime_options=ConversionRuntimeOptions(
                tts_engine="kokoro",
                tts_quantization=quantization,
                tts_provider="cuda",
                tts_device="cuda:0",
                tts_options={"model_name": "Kokoro-82M"},
            )
        )

    assert runtime_specs == []


def test_app_controller_generate_voice_preview_forwards_runtime_spec(tmp_path):
    runtime_spec = object()
    preview_path = tmp_path / "preview.wav"
    controller = AppController.__new__(AppController)
    controller._voice_service = MagicMock()
    controller._voice_service.generate_voice_preview.return_value = preview_path

    result = controller.generate_voice_preview(
        voice="am_adam",
        runtime_spec=runtime_spec,
    )

    assert result == preview_path
    controller._voice_service.generate_voice_preview.assert_called_once_with(
        voice="am_adam",
        speed=1.0,
        text=None,
        engine_name=None,
        tts_quantization=None,
        tts_provider=None,
        tts_device=None,
        tts_options=None,
        runtime_spec=runtime_spec,
    )


def test_conversion_service_validates_quantization_from_runtime_options(tmp_path):
    source_path = tmp_path / "book.epub"
    source_path.write_text("dummy")
    calls = []

    def _validator(engine: str, quantization: str | None, *, runtime_spec=None) -> None:
        calls.append((engine, quantization))
        raise RuntimeError(f"validated {engine}/{quantization}")

    service = _make_service_with_validator(tmp_path, _validator)

    with pytest.raises(RuntimeError, match="validated kokoro/bf16"):
        service.start_conversion(
            source_path=source_path,
            runtime_options=ConversionRuntimeOptions(
                tts_engine="kokoro",
                tts_options={"quantization": "bf16"},
            ),
        )

    assert calls == [("kokoro", "bf16")]


def test_conversion_service_rejects_removed_conversion_engine(tmp_path):
    source_path = tmp_path / "book.epub"
    source_path.write_text("dummy")
    validator_calls = []

    def _validator(engine: str, quantization: str | None, *, runtime_spec=None) -> None:
        validator_calls.append((engine, quantization))

    service = _make_service_with_validator(tmp_path, _validator)

    with pytest.raises(ValueError, match="not available for MLX conversion"):
        service.start_conversion(
            source_path=source_path,
            runtime_options=ConversionRuntimeOptions(
                voice="am_adam",
                speed=1.0,
                tts_engine="removed_engine",
                tts_quantization=None,
                tts_options=None,
            ),
        )

    assert validator_calls == []


def test_conversion_service_rejects_unknown_tts_quantization_before_runtime_validation(tmp_path):
    source_path = tmp_path / "book.epub"
    source_path.write_text("dummy")
    validator_calls = []

    def _validator(engine: str, quantization: str | None, *, runtime_spec=None) -> None:
        validator_calls.append((engine, quantization))

    service = _make_service_with_validator(tmp_path, _validator)

    with pytest.raises(ValueError, match="Unsupported TTS runtime quantization"):
        service.start_conversion(
            source_path=source_path,
            runtime_options=ConversionRuntimeOptions(
                voice="am_adam",
                speed=1.0,
                tts_engine="kokoro",
                tts_quantization=None,
                tts_options={"quantization": "not-real"},
            ),
        )

    assert validator_calls == []


def test_repair_validates_quantization_from_tts_options(tmp_path):
    calls = []

    def _validator(engine: str, quantization: str | None, *, runtime_spec=None) -> None:
        calls.append((engine, quantization))
        raise RuntimeError(f"validated {engine}/{quantization}")

    service = _make_service_with_validator(tmp_path, _validator)

    with pytest.raises(RuntimeError, match="validated kokoro/bf16"):
        service.start_repair(
            book_id=123,
            runtime_options=ConversionRuntimeOptions(
                voice="am_adam",
                speed=1.0,
                tts_engine="kokoro",
                tts_quantization=None,
                tts_options={"quantization": "bf16"},
            ),
        )

    assert calls == [("kokoro", "bf16")]


def test_conversion_service_pipeline_config_accepts_tts_model_option(tmp_path):
    service = _make_service(tmp_path, _PipelineStub)

    pipeline_config = service._create_pipeline_config(  # noqa: SLF001 - direct contract coverage
        runtime_options=ConversionRuntimeOptions(
            voice="am_adam",
            speed=1.0,
            tts_engine="kokoro",
            tts_quantization=None,
            cleaner_model=None,
            cleaner_backend=None,
            cleaner_api_model=None,
            tts_options={"model_name": "mlx-community/Kokoro-82M-test"},
        ),
        conversion_mode="audiobook",
    )

    assert pipeline_config.tts_engine == "kokoro"
    assert pipeline_config.tts_model_name == "mlx-community/Kokoro-82M-test"


def test_conversion_service_pipeline_config_includes_conversion_mode(tmp_path):
    service = _make_service(tmp_path, _PipelineStub)

    pipeline_config = service._create_pipeline_config(  # noqa: SLF001 - direct contract coverage
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
            tts_engine="kokoro",
            tts_quantization=None,
            cleaner_model=None,
            cleaner_backend=None,
            cleaner_api_model=None,
            tts_options=None,
        ),
        conversion_mode="document_audio",
    )

    assert pipeline_config.conversion_mode == "document_audio"


def test_conversion_service_pipeline_config_defaults_cleaning_artifacts_on(tmp_path):
    service = _make_service(tmp_path, _PipelineStub)

    pipeline_config = service._create_pipeline_config(  # noqa: SLF001 - direct contract coverage
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
            tts_engine="kokoro",
            tts_quantization=None,
            cleaner_model=None,
            cleaner_backend=None,
            cleaner_api_model=None,
            tts_options=None,
        ),
        conversion_mode="audiobook",
    )

    assert pipeline_config.save_cleaning_debug is True


def test_conversion_service_pipeline_config_accepts_direct_cleaner_api_key(tmp_path):
    service = _make_service(tmp_path, _PipelineStub)

    pipeline_config = service._create_pipeline_config(  # noqa: SLF001 - direct contract coverage
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.0,
            tts_engine="kokoro",
            tts_quantization=None,
            cleaner_model=None,
            cleaner_backend="api",
            cleaner_api_model="gpt-5-nano",
            cleaner_api_key="pasted-key",
            tts_options=None,
        ),
        conversion_mode="audiobook",
    )

    assert pipeline_config.cleaner_api_key == "pasted-key"


def test_conversion_service_pipeline_config_preserves_frontend_selection_values(tmp_path):
    service = _make_service(tmp_path, _PipelineStub)

    pipeline_config = service._create_pipeline_config(  # noqa: SLF001 - direct contract coverage
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.35,
            tts_engine="kokoro",
            tts_quantization="bf16",
            cleaner_model=None,
            cleaner_backend="api",
            cleaner_api_model="gpt-5-nano",
            cleaner_api_key="sk-contract",
            tts_options={"quantization": "bf16"},
            parallel_worker_mode="manual",
            enable_parallel=True,
            max_parallel_chapters=3,
        ),
        conversion_mode="document_audio",
    )

    assert pipeline_config.conversion_mode == "document_audio"
    assert pipeline_config.tts_engine == "kokoro"
    assert pipeline_config.tts_quantization == "bf16"
    assert pipeline_config.voice == "af_bella"
    assert pipeline_config.speed == 1.35
    assert pipeline_config.cleaner_backend == "api"
    assert pipeline_config.cleaner_api_model == "gpt-5-nano"
    assert pipeline_config.cleaner_api_key == "sk-contract"
    assert pipeline_config.parallel_worker_mode == "manual"
    assert pipeline_config.enable_parallel is True
    assert pipeline_config.max_parallel_chapters == 3
    assert pipeline_config.requested_parallel_chapters == 3
    assert pipeline_config.memory_policy is not None
    assert pipeline_config.memory_policy.enable_warmup_calibration is False


def test_app_controller_pipeline_config_wrapper_preserves_worker_selection(tmp_path):
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    controller = AppController(config=config, repository=_RepositoryStub())

    pipeline_config = controller._create_pipeline_config(  # noqa: SLF001 - compatibility wrapper contract
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.35,
            tts_engine="kokoro",
            tts_quantization="bf16",
            cleaner_backend="api",
            cleaner_api_model="gpt-5-nano",
            cleaner_api_key="sk-contract",
            tts_options={"quantization": "bf16"},
            parallel_worker_mode="manual",
            enable_parallel=True,
            max_parallel_chapters=3,
        ),
        conversion_mode="document_audio",
    )

    assert pipeline_config.conversion_mode == "document_audio"
    assert pipeline_config.parallel_worker_mode == "manual"
    assert pipeline_config.enable_parallel is True
    assert pipeline_config.max_parallel_chapters == 3
    assert pipeline_config.requested_parallel_chapters == 3


def test_app_controller_start_conversion_preserves_frontend_selection_kwargs(tmp_path):
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    controller = AppController(config=config, repository=_RepositoryStub())
    captured_kwargs = {}

    controller._conversion_service = SimpleNamespace(  # noqa: SLF001 - injected app service
        start_conversion=lambda **kwargs: captured_kwargs.update(kwargs)
    )

    controller.start_conversion(
        source_path=tmp_path / "source.md",
        runtime_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.35,
            tts_engine="kokoro",
            tts_quantization="bf16",
            cleaner_model=None,
            cleaner_backend="api",
            cleaner_api_model="gpt-5-nano",
            cleaner_api_key="sk-contract",
            tts_options={"quantization": "bf16"},
            parallel_worker_mode="manual",
            enable_parallel=True,
            max_parallel_chapters=3,
        ),
        callbacks=None,
        conversion_mode="document_audio",
        title="Frontend Contract Book",
        author="Contract Author",
    )

    assert captured_kwargs["conversion_mode"] == "document_audio"
    assert captured_kwargs["runtime_options"] == ConversionRuntimeOptions(
        voice="af_bella",
        speed=1.35,
        tts_engine="kokoro",
        tts_quantization="bf16",
        cleaner_model=None,
        cleaner_backend="api",
        cleaner_api_model="gpt-5-nano",
        cleaner_api_key="sk-contract",
        tts_options={"quantization": "bf16"},
        parallel_worker_mode="manual",
        enable_parallel=True,
        max_parallel_chapters=3,
    )


def test_conversion_service_persists_packaged_playback_metadata_on_success(tmp_path):
    repo = _RepositoryStub()
    source_path = tmp_path / "book.pdf"
    source_path.write_text("source", encoding="utf-8")
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    chapter_one_mp3 = tmp_path / "output" / "chapter_01.mp3"
    chapter_one_mp3.parent.mkdir(parents=True, exist_ok=True)
    chapter_one_mp3.write_bytes(b"mp3")
    packaged_path = tmp_path / "output" / "Book" / "Book.m4b"
    packaged_path.parent.mkdir(parents=True, exist_ok=True)
    packaged_path.write_bytes(b"m4b")
    pipeline_result = PipelineConversionResult(
        success=True,
        title="Book",
        author="Author",
        output_path=packaged_path,
        chapters=[
            ChapterResult(
                chapter_number=1,
                chapter_title="One",
                mp3_path=chapter_one_mp3,
                duration_ms=1000,
            )
        ],
        total_duration_ms=1000,
    )
    pipeline_cls = lambda **kwargs: _PipelineStub(result=pipeline_result, **kwargs)  # noqa: E731
    service = ConversionService(
        config=config,
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=pipeline_cls,
    )

    job = _start_conversion_and_wait(
        service,
        source_path=source_path,
        title="Book",
        author="Author",
    )

    assert job.status == JobStatus.COMPLETED
    assert repo.created_book is not None
    assert repo.created_book.playback_path == str(packaged_path)
    assert repo.created_book.playback_status == PlaybackStatus.PACKAGED


def test_conversion_service_success_handler_failure_replaces_success_result(tmp_path):
    class _FailingRepository(_RepositoryStub):
        def create_book_with_chapters(self, book, chapters):  # noqa: ANN001, ANN201
            _ = book, chapters
            raise RuntimeError("database write failed")

    repo = _FailingRepository()
    source_path = tmp_path / "book.pdf"
    source_path.write_text("source", encoding="utf-8")
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    packaged_path = tmp_path / "output" / "Book" / "Book.m4b"
    packaged_path.parent.mkdir(parents=True, exist_ok=True)
    packaged_path.write_bytes(b"m4b")
    pipeline_result = PipelineConversionResult(
        success=True,
        title="Book",
        author="Author",
        output_path=packaged_path,
        chapters=[
            ChapterResult(
                chapter_number=1,
                chapter_title="One",
                duration_ms=1000,
            )
        ],
        total_duration_ms=1000,
    )
    pipeline_cls = lambda **kwargs: _PipelineStub(result=pipeline_result, **kwargs)  # noqa: E731
    service = ConversionService(
        config=config,
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=pipeline_cls,
    )

    job = _start_conversion_and_wait(
        service,
        source_path=source_path,
        title="Book",
        author="Author",
    )

    assert job.status == JobStatus.FAILED
    assert job.result is not None
    assert job.result.success is False
    assert job.result.error_message == "database write failed"


def test_conversion_service_rejects_success_without_packaged_playback_metadata(tmp_path):
    repo = _RepositoryStub()
    source_path = tmp_path / "book.pdf"
    source_path.write_text("source", encoding="utf-8")
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    chapter_one_mp3 = tmp_path / "output" / "chapter_01.mp3"
    chapter_one_mp3.parent.mkdir(parents=True, exist_ok=True)
    chapter_one_mp3.write_bytes(b"mp3")
    pipeline_result = PipelineConversionResult(
        success=True,
        title="Book",
        author="Author",
        output_path=None,
        chapters=[
            ChapterResult(
                chapter_number=1,
                chapter_title="One",
                mp3_path=chapter_one_mp3,
                duration_ms=1000,
            )
        ],
        total_duration_ms=1000,
    )
    pipeline_cls = lambda **kwargs: _PipelineStub(result=pipeline_result, **kwargs)  # noqa: E731
    service = ConversionService(
        config=config,
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=pipeline_cls,
    )

    job = _start_conversion_and_wait(
        service,
        source_path=source_path,
        title="Book",
        author="Author",
    )

    assert job.status == JobStatus.FAILED
    assert job.result is not None
    assert job.result.success is False
    assert job.result.chapters_completed == 1
    assert "missing packaged output" in str(job.error).lower()
    assert repo.create_calls == 0
    assert repo.created_book is None


def test_conversion_service_repeated_same_title_runs_persist_isolated_output_paths_and_playback_metadata(
    tmp_path,
):
    source_path = tmp_path / "source.md"
    source_path.write_text("# Same Title\n\nShort text.", encoding="utf-8")
    title = "Same Title"
    source_bytes = b"source-copy-v1"
    first_chapter_bytes = b"chapter-one-v1"
    first_package_bytes = b"package-one-v1"

    class _RepeatedTitlePipeline:
        run_count = 0
        temp_paths_by_run = []

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            self.config = kwargs["config"]

        def convert(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            source = Path(kwargs["source_path"])
            book_title = kwargs["title"]
            type(self).run_count += 1
            run_number = type(self).run_count
            workspace = prepare_book_workspace(self.config.output_dir, book_title)

            temp_artifact = self.config.temp_dir / f"synthetic-run-{run_number}.tmp"
            temp_artifact.parent.mkdir(parents=True, exist_ok=True)
            temp_artifact.write_bytes(f"temp-{run_number}".encode("utf-8"))
            type(self).temp_paths_by_run.append(temp_artifact)

            copied_source = workspace.output_dir / source.name
            copied_source.write_bytes(source_bytes if run_number == 1 else b"source-copy-v2")
            chapter_mp3 = workspace.chapters_dir / "01_same_title.mp3"
            chapter_mp3.write_bytes(first_chapter_bytes if run_number == 1 else b"chapter-one-v2")
            package_path = workspace.output_dir / f"{workspace.output_dir.name}.m4b"
            package_path.write_bytes(first_package_bytes if run_number == 1 else b"package-one-v2")
            temp_artifact.unlink()

            return PipelineConversionResult(
                success=True,
                title=book_title,
                author="Author",
                output_path=package_path,
                chapters=[
                    ChapterResult(
                        chapter_number=1,
                        chapter_title="One",
                        mp3_path=chapter_mp3,
                        duration_ms=1000,
                    )
                ],
                total_duration_ms=1000,
            )

    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    repo = SQLiteRepository(
        config.db_path,
        output_dir=config.output_dir,
        temp_dir=config.temp_dir,
    )
    service = ConversionService(
        config=config,
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=_RepeatedTitlePipeline,
    )
    jobs = []

    try:
        for index in range(1, 3):
            job = _start_conversion_and_wait(
                service,
                source_path=source_path,
                title=title,
                author="Author",
            )
            jobs.append(job)
            assert job.status == JobStatus.COMPLETED
            assert job.book_id is not None
            assert _RepeatedTitlePipeline.temp_paths_by_run[index - 1].exists() is False

        assert jobs[0].book_id != jobs[1].book_id

        with sqlite3.connect(config.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, title, source_path, playback_path, playback_status
                FROM books
                WHERE title = ? AND source_path = ?
                ORDER BY id
                """,
                (title, str(source_path)),
            ).fetchall()
        assert len(rows) == 2

        books = [repo.get_book(job.book_id) for job in jobs]
        chapters = [repo.get_book_with_chapters(job.book_id)[1][0] for job in jobs]

        assert all(book is not None for book in books)
        assert [book.playback_status for book in books] == [
            PlaybackStatus.PACKAGED,
            PlaybackStatus.PACKAGED,
        ]

        playback_paths = [Path(book.playback_path) for book in books]
        assert all(path.exists() for path in playback_paths)
        assert playback_paths[0] != playback_paths[1]

        first_output_dir = config.output_dir / title
        second_output_dir = config.output_dir / f"{title} (2)"
        assert playback_paths[0].parent == first_output_dir
        assert playback_paths[1].parent == second_output_dir

        chapter_paths = [Path(chapter.mp3_path) for chapter in chapters]
        assert all(path.exists() for path in chapter_paths)
        assert chapter_paths[0] != chapter_paths[1]
        assert chapter_paths[0].parent == first_output_dir / "chapters"
        assert chapter_paths[1].parent == second_output_dir / "chapters"

        first_source_copy = first_output_dir / source_path.name
        first_chapter = chapter_paths[0]
        first_package = playback_paths[0]
        assert first_source_copy.read_bytes() == source_bytes
        assert first_chapter.read_bytes() == first_chapter_bytes
        assert first_package.read_bytes() == first_package_bytes
        assert not list(config.temp_dir.glob("synthetic-run-*.tmp"))
    finally:
        repo.close()
        service.cleanup()


def test_conversion_service_persists_document_audio_mode_for_txt_source(tmp_path):
    repo = _RepositoryStub()
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    service = ConversionService(
        config=config,
        repository=repo,
        job_cls=SimpleNamespace,
        status_enum=_JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=_PipelineStub,
    )
    chapter_one_mp3 = tmp_path / "output" / "chapter_01.mp3"
    chapter_one_mp3.parent.mkdir(parents=True, exist_ok=True)
    chapter_one_mp3.write_bytes(b"mp3")
    pipeline_result = PipelineConversionResult(
        success=True,
        title="Notes",
        author="Author",
        output_path=None,
        chapters=[
            ChapterResult(
                chapter_number=1,
                chapter_title="Full Document",
                mp3_path=chapter_one_mp3,
                duration_ms=1000,
            )
        ],
        total_duration_ms=1000,
    )

    service._persist_conversion_library_records(  # noqa: SLF001 - direct contract coverage
        source_path=Path("/tmp/notes.txt"),
        pipeline_result=pipeline_result,
        conversion_mode=ConversionMode.DOCUMENT_AUDIO,
    )

    assert repo.created_book is not None
    assert repo.created_book.source_type == SourceType.TXT
    assert repo.created_book.conversion_mode == ConversionMode.DOCUMENT_AUDIO


def test_conversion_service_repair_delegates_to_pipeline_repair_entrypoint(tmp_path):
    source_path = tmp_path / "source.md"
    source_path.write_text("# One\n\nText one.", encoding="utf-8")

    class _RepairRepository:
        def __init__(self) -> None:
            self.chapter_updates = []
            self.book_updates = []
            self.book = Book(
                id=77,
                title="Repair Book",
                author="Tester",
                source_path=str(source_path),
                source_type=SourceType.MD,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path=None,
                playback_status=PlaybackStatus.MISSING,
                playback_position_sec=0.0,
                created_at=__import__("datetime").datetime(2026, 4, 1, 9, 0, 0),
                updated_at=__import__("datetime").datetime(2026, 4, 1, 9, 5, 0),
            )
            self.chapters = [Chapter(1, 77, 1, "One", None, None)]

        def get_book_with_chapters(self, book_id):
            if book_id != 77:
                return None
            return self.book, list(self.chapters)

        def update_chapter(self, chapter_id, duration_ms=None, mp3_path=None):
            _ = (chapter_id, duration_ms, mp3_path)
            raise AssertionError("repair success should use apply_repair_result")

        def update_book(self, book_id, **updates):
            _ = (book_id, updates)
            raise AssertionError("repair success should use apply_repair_result")

        def apply_repair_result(
            self,
            book_id,
            *,
            repaired_chapter_updates,
            playback_path,
            playback_status,
            total_chapters,
        ):
            self.book_updates.append(
                (
                    book_id,
                    {
                        "repaired_chapter_updates": list(repaired_chapter_updates),
                        "playback_path": playback_path,
                        "playback_status": playback_status,
                        "total_chapters": total_chapters,
                    },
                )
            )
            return self.book

    class _RepairEntryPipeline:
        repair_calls = []

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            self.config = kwargs["config"]
            self._tts_engine = None
            self._selected_parallel_chapters = 1

        def repair(self, **kwargs) -> RepairResult:  # noqa: ANN003
            type(self).repair_calls.append(kwargs)
            plan = kwargs["plan"]
            output_dir = plan.output_dir
            final = output_dir / "Repair Book.m4b"
            final.parent.mkdir(parents=True, exist_ok=True)
            final.write_bytes(b"m4b")
            return RepairResult(
                success=True,
                title="Repair Book",
                author="Tester",
                output_path=final,
                chapters=[
                    ChapterResult(
                        chapter_number=1,
                        chapter_title="One",
                        duration_ms=1000,
                    )
                ],
                total_duration_ms=1000,
            )

        def stage_ingest(self, source_path):  # noqa: ANN001
            raise AssertionError("service must not orchestrate repair ingestion")

        def stage_cleanup_models(self, *, force: bool = False) -> None:
            _ = force

    repo = _RepairRepository()
    service = ConversionService(
        config=AppConfig(
            data_dir=tmp_path / "data",
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            db_path=tmp_path / "data" / "app.db",
        ),
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=_RepairEntryPipeline,
    )

    job = _start_repair_and_wait(service, book_id=77)

    assert job.status == JobStatus.COMPLETED
    assert len(_RepairEntryPipeline.repair_calls) == 1
    assert _RepairEntryPipeline.repair_calls[0]["plan"].source_path == source_path
    assert repo.book_updates[-1][1]["playback_status"] == PlaybackStatus.PACKAGED


def test_conversion_service_repair_late_cancel_after_packaging_persists_completed_output(tmp_path):
    source_path = tmp_path / "source.md"
    source_path.write_text("# One\n\nText one.", encoding="utf-8")
    final_output = tmp_path / "output" / "Repair Book" / "Repair Book.m4b"
    repaired_mp3 = tmp_path / "output" / "Repair Book" / "chapter_01.mp3"

    class _RepairRepository:
        def __init__(self) -> None:
            self.chapter_updates = []
            self.book_updates = []
            self.book = Book(
                id=77,
                title="Repair Book",
                author="Tester",
                source_path=str(source_path),
                source_type=SourceType.MD,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path=None,
                playback_status=PlaybackStatus.CHAPTER_AUDIO,
                playback_position_sec=0.0,
                created_at=__import__("datetime").datetime(2026, 4, 1, 9, 0, 0),
                updated_at=__import__("datetime").datetime(2026, 4, 1, 9, 5, 0),
            )
            self.chapters = [Chapter(1, 77, 1, "One", None, None)]

        def get_book_with_chapters(self, book_id):
            if book_id != 77:
                return None
            return self.book, list(self.chapters)

        def update_chapter(self, chapter_id, duration_ms=None, mp3_path=None):
            self.chapter_updates.append((chapter_id, duration_ms, mp3_path))
            return self.chapters[0]

        def update_book(self, book_id, **updates):
            self.book_updates.append((book_id, updates))
            return self.book

        def apply_repair_result(
            self,
            book_id,
            *,
            repaired_chapter_updates,
            playback_path,
            playback_status,
            total_chapters,
        ):
            return _apply_fake_repair_result(
                self,
                book_id,
                repaired_chapter_updates=repaired_chapter_updates,
                playback_path=playback_path,
                playback_status=playback_status,
                total_chapters=total_chapters,
            )

    class _LateCancelRepairPipeline:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            self.config = kwargs["config"]
            self._tts_engine = None
            self._selected_parallel_chapters = 1

        def repair(self, **kwargs) -> RepairResult:  # noqa: ANN003
            _ = kwargs
            assert job_ready.wait(timeout=3)
            final_output.parent.mkdir(parents=True, exist_ok=True)
            final_output.write_bytes(b"m4b")
            repaired_mp3.write_bytes(b"mp3")
            job_holder["job"].cancel_requested = True
            return RepairResult(
                success=True,
                title="Repair Book",
                author="Tester",
                output_path=final_output,
                chapters=[
                    ChapterResult(
                        chapter_number=1,
                        chapter_title="One",
                        mp3_path=repaired_mp3,
                        duration_ms=1000,
                    )
                ],
                total_duration_ms=1000,
                repaired_chapter_updates=[RepairedChapterUpdate(1, 1000, str(repaired_mp3))],
            )

        def stage_cleanup_models(self, *, force: bool = False) -> None:
            _ = force

    state_events = []

    def _on_event(event) -> None:
        if getattr(event, "event_type", None) == EventType.STATE:
            state_events.append(event.state)

    repo = _RepairRepository()
    service = ConversionService(
        config=AppConfig(
            data_dir=tmp_path / "data",
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            db_path=tmp_path / "data" / "app.db",
        ),
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=_LateCancelRepairPipeline,
    )
    job_holder = {}
    job_ready = threading.Event()

    job = service.start_repair(
        book_id=77,
        callbacks=ConversionCallbacks(on_event=_on_event),
        runtime_options=_default_runtime_options(),
    )
    job_holder["job"] = job
    job_ready.set()
    assert job.wait(timeout=3)

    assert job.status == JobStatus.COMPLETED
    assert job.result is not None
    assert job.result.success is True
    assert job.result.output_path == str(final_output)
    assert repo.chapter_updates == [(1, 1000, str(repaired_mp3))]
    assert repo.book_updates[-1][1]["playback_path"] == str(final_output)
    assert repo.book_updates[-1][1]["playback_status"] == PlaybackStatus.PACKAGED
    assert state_events[-1] == JobState.COMPLETED
    assert JobState.CANCELLED not in state_events
    service.cleanup()


def test_conversion_service_repair_rejects_delegated_missing_packaged_output(tmp_path):
    source_path = tmp_path / "source.md"
    source_path.write_text("# One\n\nText one.", encoding="utf-8")

    class _RepairRepository:
        def __init__(self) -> None:
            self.chapter_updates = []
            self.book_updates = []
            self.book = Book(
                id=77,
                title="Repair Book",
                author="Tester",
                source_path=str(source_path),
                source_type=SourceType.MD,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path=None,
                playback_status=PlaybackStatus.MISSING,
                playback_position_sec=0.0,
                created_at=__import__("datetime").datetime(2026, 4, 1, 9, 0, 0),
                updated_at=__import__("datetime").datetime(2026, 4, 1, 9, 5, 0),
            )
            self.chapters = [Chapter(1, 77, 1, "One", None, None)]

        def get_book_with_chapters(self, book_id):
            if book_id != 77:
                return None
            return self.book, list(self.chapters)

        def update_chapter(self, chapter_id, duration_ms=None, mp3_path=None):
            self.chapter_updates.append((chapter_id, duration_ms, mp3_path))
            return self.chapters[0]

        def update_book(self, book_id, **updates):
            self.book_updates.append((book_id, updates))
            return self.book

    class _MissingDelegatedRepairPipeline:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            self.config = kwargs["config"]
            self._tts_engine = None

        def repair(self, **kwargs) -> RepairResult:  # noqa: ANN003
            output_dir = kwargs["plan"].output_dir
            return RepairResult(
                success=True,
                title="Repair Book",
                author="Tester",
                output_path=output_dir / "missing.m4b",
                chapters=[
                    ChapterResult(
                        chapter_number=1,
                        chapter_title="One",
                        duration_ms=1000,
                    )
                ],
                total_duration_ms=1000,
            )

        def stage_cleanup_models(self, *, force: bool = False) -> None:
            _ = force

    repo = _RepairRepository()
    service = ConversionService(
        config=AppConfig(
            data_dir=tmp_path / "data",
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            db_path=tmp_path / "data" / "app.db",
        ),
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=_MissingDelegatedRepairPipeline,
    )

    job = _start_repair_and_wait(service, book_id=77)

    assert job.status == JobStatus.FAILED
    assert "packaged output" in str(job.error).lower()
    assert repo.chapter_updates == []
    assert repo.book_updates == []
    service.cleanup()


def test_pipeline_repair_dispatches_missing_chapters_through_worker_plan(tmp_path):
    from modules.pipeline import repair as repair_module

    source_path = tmp_path / "source.md"
    source_path.write_text("# One\n\nText one.\n\n# Two\n\nText two.", encoding="utf-8")
    output_dir = tmp_path / "output"
    chapters_dir = output_dir / "chapters"
    existing_audio = chapters_dir / "01_one.mp3"
    existing_audio.parent.mkdir(parents=True, exist_ok=True)
    existing_audio.write_bytes(b"one")

    stored_chapters = [
        Chapter(1, 77, 1, "One", 1000, str(existing_audio)),
        Chapter(2, 77, 2, "Two", None, None),
    ]
    dispatched = {}

    class _RepairPipeline:
        def __init__(self) -> None:
            self._stage = None
            self._progress = SimpleNamespace(
                set_total_chars=lambda value: dispatched.setdefault("total_chars", value)
            )
            self._total_chars = 0
            self.config = SimpleNamespace(
                max_parallel_chapters=4,
                requested_parallel_chapters=4,
                parallel_worker_mode="manual",
                tts_engine="other",
                cleaner_backend="api",
                memory_policy=None,
            )
            self.dispatch_context = SimpleNamespace(parallel_capacity=lambda fallback: 4)
            self.is_parallel = True
            self._parallel_execution_active = False
            self.stages = SimpleNamespace(
                ingest=self.stage_ingest,
                warmup_models=self.stage_warmup_models,
                process_chapter=self.stage_process_chapter,
                process_chapters_parallel=self.stage_process_chapters_parallel,
                process_chapters_sequential=self.stage_process_chapters_sequential,
                package=self.stage_package,
            )

        def begin_run(self) -> None:
            dispatched["began"] = True

        def finish_run(self) -> None:
            dispatched["finished"] = True

        def check_cancelled(self) -> bool:
            return False

        def set_stage(self, stage) -> None:  # noqa: ANN001
            self._stage = stage

        @property
        def stage(self):
            return self._stage

        def set_total_chars(self, total_chars: int) -> None:
            self._total_chars = total_chars
            self._progress.set_total_chars(total_chars)

        def stage_ingest(self, path):  # noqa: ANN001
            assert path == source_path
            return SimpleNamespace(
                chapters=[
                    SimpleNamespace(number=1, title="One", content="Text one."),
                    SimpleNamespace(number=2, title="Two", content="Text two."),
                ]
            )

        def stage_process_chapter(self, **kwargs):  # noqa: ANN003
            raise AssertionError(
                "repair should dispatch missing chapters, not call _process_chapter directly"
            )

        def _log_verbose(self, message, level="info"):  # noqa: ANN001
            _ = message, level

        def stage_warmup_models(self) -> None:
            pass

        def stage_process_chapters_parallel(self, chapters, output_path):  # noqa: ANN001
            _ = chapters, output_path
            raise AssertionError("patched dispatcher should own repair dispatch")

        def stage_process_chapters_sequential(self, chapters, output_path):  # noqa: ANN001
            _ = chapters, output_path
            raise AssertionError("patched dispatcher should own repair dispatch")

        def stage_package(self, chapter_results, title, author, output_dir):  # noqa: ANN001
            _ = chapter_results, title, author
            final = output_dir / "Repair Book.m4b"
            final.write_bytes(b"m4b")
            return final

    def _dispatch(dispatch_context, chapters, output_path):  # noqa: ANN001
        dispatched["max_workers"] = dispatch_context.parallel_capacity(1)
        dispatched["chapters"] = [chapter.number for chapter in chapters]
        dispatched["output_dir"] = output_path
        repaired = chapters_dir / "02_two.mp3"
        repaired.write_bytes(b"two")
        return [
            ChapterResult(
                chapter_number=2,
                chapter_title="Two",
                mp3_path=repaired,
                duration_ms=2000,
            )
        ]

    pipeline = _RepairPipeline()
    with patch.object(repair_module, "dispatch_chapters", _dispatch):
        result = repair_module.repair_conversion(
            pipeline,
            source_path=source_path,
            stored_chapters=stored_chapters,
            title="Repair Book",
            author="Tester",
            output_dir=output_dir,
            chapters_dir=chapters_dir,
            resolve_chapter_audio=lambda chapter: (
                Path(chapter.mp3_path) if chapter.mp3_path else None
            ),
        )

    assert result.success is True
    assert pipeline.stage.value == "complete"
    assert dispatched["chapters"] == [2]
    assert dispatched["max_workers"] == 4
    assert dispatched["output_dir"] == chapters_dir
    assert result.repaired_chapter_updates == [
        RepairedChapterUpdate(2, 2000, str(chapters_dir / "02_two.mp3"))
    ]


def test_conversion_pipeline_repair_entrypoint_uses_explicit_runtime_plan(tmp_path):
    import modules.pipeline.orchestrator as orchestrator_module

    source_path = tmp_path / "source.md"
    source_path.write_text("# One\n\nText one.\n\n# Two\n\nText two.", encoding="utf-8")
    output_dir = tmp_path / "output"
    chapters_dir = output_dir / "chapters"
    existing_audio = chapters_dir / "01_one.mp3"
    existing_audio.parent.mkdir(parents=True, exist_ok=True)
    existing_audio.write_bytes(b"one")
    stored_chapters = [
        Chapter(1, 77, 1, "One", 1000, str(existing_audio)),
        Chapter(2, 77, 2, "Two", None, None),
    ]
    observed = {}
    pipeline = ConversionPipeline()

    def _ingest(path):
        assert path == source_path
        return SimpleNamespace(
            chapters=[
                SimpleNamespace(number=1, title="One", content="Text one."),
                SimpleNamespace(number=2, title="Two", content="Text two."),
            ]
        )

    stage_patches = (
        patch.object(pipeline.stages, "ingest", _ingest),
        patch.object(
            pipeline.stages,
            "cleanup_models",
            lambda *, force=False: observed.setdefault("cleaned", True),
        ),
    )

    def _package(*, chapter_results, title, author, output_dir):  # noqa: ANN001
        observed["packaged"] = [result.chapter_number for result in chapter_results]
        observed["title"] = title
        observed["author"] = author
        final = output_dir / f"{title}.m4b"
        final.write_bytes(b"m4b")
        return final

    def _dispatch(dispatch_context, chapters, output_path):  # noqa: ANN001
        assert dispatch_context is pipeline.dispatch_context
        observed["missing"] = [chapter.number for chapter in chapters]
        observed["output_dir"] = output_path
        repaired = chapters_dir / "02_two.mp3"
        repaired.write_bytes(b"two")
        return [
            ChapterResult(
                chapter_number=2,
                chapter_title="Two",
                mp3_path=repaired,
                duration_ms=2000,
            )
        ]

    with (
        stage_patches[0],
        stage_patches[1],
        patch.object(pipeline.stages, "package", _package),
        patch.object(orchestrator_module, "dispatch_chapters", _dispatch),
    ):
        result = pipeline.repair(
            plan=RepairPlan(
                source_path=source_path,
                stored_chapters=stored_chapters,
                title="Repair Book",
                author="Tester",
                output_dir=output_dir,
                chapters_dir=chapters_dir,
                resolve_chapter_audio=lambda chapter: (
                    Path(chapter.mp3_path) if chapter.mp3_path else None
                ),
            )
        )

    assert result.success is True
    assert pipeline.stage.value == "complete"
    assert observed == {
        "missing": [2],
        "output_dir": chapters_dir,
        "packaged": [1, 2],
        "title": "Repair Book",
        "author": "Tester",
        "cleaned": True,
    }
    assert result.repaired_chapter_updates == [
        RepairedChapterUpdate(2, 2000, str(chapters_dir / "02_two.mp3"))
    ]


def test_repair_conversion_wrapper_delegates_real_pipeline_to_public_repair(tmp_path):
    from modules.pipeline import repair as repair_module

    source_path = tmp_path / "source.md"
    source_path.write_text("# One\n\nText one.", encoding="utf-8")
    pipeline = ConversionPipeline()
    pipeline.cancel()

    result = repair_module.repair_conversion(
        pipeline,
        plan=RepairPlan(
            source_path=source_path,
            stored_chapters=[Chapter(1, 77, 1, "One", None, None)],
            title="Repair Book",
            author="Tester",
            output_dir=tmp_path / "output",
            chapters_dir=tmp_path / "output" / "chapters",
            resolve_chapter_audio=lambda chapter: None,
        ),
    )

    assert result.success is False
    assert result.error == "Cancelled"
    assert pipeline.stage.value == "cancelled"


def test_pipeline_repair_marks_stage_error_on_structure_mismatch(tmp_path):
    from modules.pipeline import repair as repair_module

    source_path = tmp_path / "source.md"
    source_path.write_text("# One\n\nText one.", encoding="utf-8")
    output_dir = tmp_path / "output"
    chapters_dir = output_dir / "chapters"

    class _RepairPipeline:
        def __init__(self) -> None:
            self._stage = None
            self._progress = SimpleNamespace(set_total_chars=lambda value: None)
            self.config = SimpleNamespace(output_dir=output_dir)
            self.stages = SimpleNamespace(ingest=self.stage_ingest)

        def set_stage(self, stage) -> None:  # noqa: ANN001
            self._stage = stage

        @property
        def stage(self):
            return self._stage

        def stage_ingest(self, path):  # noqa: ANN001
            assert path == source_path
            return SimpleNamespace(
                chapters=[SimpleNamespace(number=1, title="One", content="Text one.")]
            )

    pipeline = _RepairPipeline()

    result = repair_module.repair_conversion(
        pipeline,
        source_path=source_path,
        stored_chapters=[
            Chapter(1, 77, 2, "Two", None, None),
        ],
        title="Repair Book",
        author="Tester",
        output_dir=output_dir,
        chapters_dir=chapters_dir,
        resolve_chapter_audio=lambda chapter: None,
    )

    assert result.success is False
    assert "structure mismatch" in str(result.error).lower()
    assert pipeline.stage.value == "error"


def test_conversion_service_repair_processes_only_missing_chapters_and_repackages(tmp_path):
    source_path = tmp_path / "source.md"
    source_path.write_text("# One\n\nText one.\n\n# Two\n\nText two.", encoding="utf-8")
    output_dir = tmp_path / "output" / "Repair Book"
    chapters_dir = output_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    existing_audio = chapters_dir / "01_one.mp3"
    existing_audio.write_bytes(b"mp3")

    class _RepairRepository:
        def __init__(self) -> None:
            self.chapter_updates = []
            self.book_updates = []
            self.book = Book(
                id=77,
                title="Repair Book",
                author="Tester",
                source_path=str(source_path),
                source_type=SourceType.MD,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=2,
                playback_path=None,
                playback_status=PlaybackStatus.CHAPTER_AUDIO,
                playback_position_sec=0.0,
                created_at=__import__("datetime").datetime(2026, 4, 1, 9, 0, 0),
                updated_at=__import__("datetime").datetime(2026, 4, 1, 9, 5, 0),
            )
            self.chapters = [
                Chapter(1, 77, 1, "One", 1000, str(existing_audio)),
                Chapter(2, 77, 2, "Two", None, None),
            ]

        def get_book_with_chapters(self, book_id):
            if book_id != 77:
                return None
            return self.book, list(self.chapters)

        def update_chapter(self, chapter_id, duration_ms=None, mp3_path=None):
            self.chapter_updates.append((chapter_id, duration_ms, mp3_path))
            chapter = next(chapter for chapter in self.chapters if chapter.id == chapter_id)
            chapter.duration_ms = duration_ms
            chapter.mp3_path = mp3_path
            return chapter

        def update_book(self, book_id, **updates):
            self.book_updates.append((book_id, updates))
            self.book.playback_path = updates.get("playback_path", self.book.playback_path)
            self.book.playback_status = updates.get("playback_status", self.book.playback_status)
            return self.book

        def apply_repair_result(
            self,
            book_id,
            *,
            repaired_chapter_updates,
            playback_path,
            playback_status,
            total_chapters,
        ):
            return _apply_fake_repair_result(
                self,
                book_id,
                repaired_chapter_updates=repaired_chapter_updates,
                playback_path=playback_path,
                playback_status=playback_status,
                total_chapters=total_chapters,
            )

    class _RepairPipeline:
        processed_numbers = []
        cleanup_calls = []

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            self.config = kwargs["config"]
            self._tts_engine = None
            self._selected_parallel_chapters = 1

        def repair(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            return _delegate_repair_to_pipeline_module(self, **kwargs)

        def stage_ingest(self, source_path):  # noqa: ANN001
            _ = source_path
            return SimpleNamespace(
                chapters=[
                    SimpleNamespace(number=1, title="One", content="Text one."),
                    SimpleNamespace(number=2, title="Two", content="Text two."),
                ]
            )

        def stage_process_chapter(self, *, chapter, chapter_idx, total_chapters, output_dir):  # noqa: ANN001
            _ = (chapter_idx, total_chapters)
            type(self).processed_numbers.append(chapter.number)
            self._tts_engine = MagicMock()
            repaired = output_dir / "02_two.mp3"
            repaired.write_bytes(b"mp3")
            return ChapterResult(
                chapter_number=chapter.number,
                chapter_title=chapter.title,
                mp3_path=repaired,
                duration_ms=2000,
            )

        def stage_package(self, chapter_results, title, author, output_dir):  # noqa: ANN001
            assert [result.chapter_number for result in chapter_results] == [1, 2]
            assert title == "Repair Book"
            assert author == "Tester"
            final = output_dir / "Repair Book.m4b"
            final.write_bytes(b"m4b")
            return final

        def stage_cleanup_models(self, *, force: bool = False) -> None:
            type(self).cleanup_calls.append(force)

        def stage_cancel_tts_idle_unload(self) -> None:
            pass

    repo = _RepairRepository()
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    service = ConversionService(
        config=config,
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=_RepairPipeline,
    )
    job = _start_repair_and_wait(service, book_id=77)

    assert _RepairPipeline.processed_numbers == [2]
    assert _RepairPipeline.cleanup_calls == [False]
    assert repo.chapter_updates == [(2, 2000, str(chapters_dir / "02_two.mp3"))]
    assert repo.book_updates[-1][1]["playback_status"] == PlaybackStatus.PACKAGED
    assert job.status == JobStatus.COMPLETED
    service.cleanup()


def test_conversion_service_repair_packages_wav_only_chapter_results(tmp_path):
    source_path = tmp_path / "source.md"
    source_path.write_text("# One\n\nText one.\n\n# Two\n\nText two.", encoding="utf-8")
    output_dir = tmp_path / "output" / "Repair Book"

    class _WavOnlyRepairRepository:
        def __init__(self) -> None:
            self.chapter_updates = []
            self.book_updates = []
            self.book = Book(
                id=77,
                title="Repair Book",
                author="Tester",
                source_path=str(source_path),
                source_type=SourceType.MD,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=2,
                playback_path=None,
                playback_status=PlaybackStatus.MISSING,
                playback_position_sec=0.0,
                created_at=__import__("datetime").datetime(2026, 4, 1, 9, 0, 0),
                updated_at=__import__("datetime").datetime(2026, 4, 1, 9, 5, 0),
            )
            self.chapters = [
                Chapter(1, 77, 1, "One", None, None),
                Chapter(2, 77, 2, "Two", None, None),
            ]

        def get_book_with_chapters(self, book_id):
            if book_id != 77:
                return None
            return self.book, list(self.chapters)

        def update_chapter(self, chapter_id, duration_ms=None, mp3_path=None):
            self.chapter_updates.append((chapter_id, duration_ms, mp3_path))
            chapter = next(chapter for chapter in self.chapters if chapter.id == chapter_id)
            chapter.duration_ms = duration_ms
            chapter.mp3_path = mp3_path
            return chapter

        def update_book(self, book_id, **updates):
            self.book_updates.append((book_id, updates))
            self.book.playback_path = updates.get("playback_path", self.book.playback_path)
            self.book.playback_status = updates.get("playback_status", self.book.playback_status)
            self.book.total_chapters = updates.get("total_chapters", self.book.total_chapters)
            return self.book

        def apply_repair_result(
            self,
            book_id,
            *,
            repaired_chapter_updates,
            playback_path,
            playback_status,
            total_chapters,
        ):
            return _apply_fake_repair_result(
                self,
                book_id,
                repaired_chapter_updates=repaired_chapter_updates,
                playback_path=playback_path,
                playback_status=playback_status,
                total_chapters=total_chapters,
            )

    class _WavOnlyRepairPipeline:
        package_inputs = []

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            _ = kwargs

        def repair(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            return _delegate_repair_to_pipeline_module(self, **kwargs)

        def stage_ingest(self, source_path):  # noqa: ANN001
            _ = source_path
            return SimpleNamespace(
                chapters=[
                    SimpleNamespace(number=1, title="One", content="Text one."),
                    SimpleNamespace(number=2, title="Two", content="Text two."),
                ]
            )

        def stage_process_chapter(self, *, chapter, chapter_idx, total_chapters, output_dir):  # noqa: ANN001
            _ = (chapter_idx, total_chapters)
            wav_path = output_dir / f"{chapter.number:02d}_{chapter.title.lower()}.wav"
            wav_path.write_bytes(b"wav")
            return ChapterResult(
                chapter_number=chapter.number,
                chapter_title=chapter.title,
                wav_path=wav_path,
                duration_ms=chapter.number * 1000,
            )

        def stage_package(self, chapter_results, title, author, output_dir):  # noqa: ANN001
            assert title == "Repair Book"
            assert author == "Tester"
            type(self).package_inputs.append(chapter_results)
            assert [result.wav_path.is_file() for result in chapter_results] == [True, True]
            assert [result.mp3_path for result in chapter_results] == [None, None]
            final = output_dir / "Repair Book.m4b"
            final.write_bytes(b"m4b")
            return final

    repo = _WavOnlyRepairRepository()
    service = ConversionService(
        config=AppConfig(
            data_dir=tmp_path / "data",
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            db_path=tmp_path / "data" / "app.db",
        ),
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=_WavOnlyRepairPipeline,
    )

    job = _start_repair_and_wait(service, book_id=77)

    assert job.status == JobStatus.COMPLETED
    assert job.result is not None
    assert job.result.success is True
    assert job.result.output_path == str(output_dir / "Repair Book.m4b")
    assert repo.chapter_updates == []
    assert repo.book_updates[-1][1]["playback_path"] == str(output_dir / "Repair Book.m4b")
    assert repo.book_updates[-1][1]["playback_status"] == PlaybackStatus.PACKAGED
    assert len(_WavOnlyRepairPipeline.package_inputs) == 1
    service.cleanup()


def test_conversion_service_repair_rejects_missing_packaged_output_before_library_update(tmp_path):
    source_path = tmp_path / "source.md"
    source_path.write_text("# One\n\nText one.", encoding="utf-8")
    output_dir = tmp_path / "output" / "Repair Book"
    original_playback_path = str(output_dir / "old.m4b")

    class _MissingPackageRepairRepository:
        def __init__(self) -> None:
            self.chapter_updates = []
            self.book_updates = []
            self.book = Book(
                id=77,
                title="Repair Book",
                author="Tester",
                source_path=str(source_path),
                source_type=SourceType.MD,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path=original_playback_path,
                playback_status=PlaybackStatus.CHAPTER_AUDIO,
                playback_position_sec=0.0,
                created_at=__import__("datetime").datetime(2026, 4, 1, 9, 0, 0),
                updated_at=__import__("datetime").datetime(2026, 4, 1, 9, 5, 0),
            )
            self.chapters = [Chapter(1, 77, 1, "One", None, None)]

        def get_book_with_chapters(self, book_id):
            if book_id != 77:
                return None
            return self.book, list(self.chapters)

        def update_chapter(self, chapter_id, duration_ms=None, mp3_path=None):
            self.chapter_updates.append((chapter_id, duration_ms, mp3_path))
            chapter = next(chapter for chapter in self.chapters if chapter.id == chapter_id)
            chapter.duration_ms = duration_ms
            chapter.mp3_path = mp3_path
            return chapter

        def update_book(self, book_id, **updates):
            self.book_updates.append((book_id, updates))
            self.book.playback_path = updates.get("playback_path", self.book.playback_path)
            self.book.playback_status = updates.get("playback_status", self.book.playback_status)
            self.book.total_chapters = updates.get("total_chapters", self.book.total_chapters)
            return self.book

    class _MissingPackageRepairPipeline:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            _ = kwargs

        def repair(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            return _delegate_repair_to_pipeline_module(self, **kwargs)

        def stage_ingest(self, source_path):  # noqa: ANN001
            _ = source_path
            return SimpleNamespace(
                chapters=[SimpleNamespace(number=1, title="One", content="Text one.")]
            )

        def stage_process_chapter(self, *, chapter, chapter_idx, total_chapters, output_dir):  # noqa: ANN001
            _ = (chapter_idx, total_chapters)
            repaired = output_dir / "01_one.mp3"
            repaired.write_bytes(b"mp3")
            return ChapterResult(
                chapter_number=chapter.number,
                chapter_title=chapter.title,
                mp3_path=repaired,
                duration_ms=1000,
            )

        def stage_package(self, chapter_results, title, author, output_dir):  # noqa: ANN001
            _ = (chapter_results, title, author)
            return output_dir / "Repair Book.m4b"

    repo = _MissingPackageRepairRepository()
    service = ConversionService(
        config=AppConfig(
            data_dir=tmp_path / "data",
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            db_path=tmp_path / "data" / "app.db",
        ),
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=_MissingPackageRepairPipeline,
    )

    job = _start_repair_and_wait(service, book_id=77)

    missing_packaged_path = output_dir / "Repair Book.m4b"
    assert job.status == JobStatus.FAILED
    assert job.result is not None
    assert job.result.success is False
    assert "packaged output" in str(job.error).lower()
    assert str(missing_packaged_path) in str(job.error)
    assert repo.chapter_updates == []
    assert repo.book_updates == []
    assert repo.book.playback_path == original_playback_path
    assert repo.book.playback_status == PlaybackStatus.CHAPTER_AUDIO
    assert not missing_packaged_path.exists()
    service.cleanup()


def test_conversion_service_repair_pre_requested_cancel_finishes_cancelled_without_processing(
    tmp_path,
):
    source_path = tmp_path / "source.md"
    source_path.write_text("# One\n\nText one.", encoding="utf-8")

    class _RepairRepository:
        def __init__(self) -> None:
            self.chapter_updates = []
            self.book_updates = []
            self.book = Book(
                id=77,
                title="Repair Book",
                author="Tester",
                source_path=str(source_path),
                source_type=SourceType.MD,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path=None,
                playback_status=PlaybackStatus.CHAPTER_AUDIO,
                playback_position_sec=0.0,
                created_at=__import__("datetime").datetime(2026, 4, 1, 9, 0, 0),
                updated_at=__import__("datetime").datetime(2026, 4, 1, 9, 5, 0),
            )
            self.chapters = [Chapter(1, 77, 1, "One", None, None)]

        def get_book_with_chapters(self, book_id):
            if book_id != 77:
                return None
            return self.book, list(self.chapters)

        def update_chapter(self, chapter_id, duration_ms=None, mp3_path=None):
            self.chapter_updates.append((chapter_id, duration_ms, mp3_path))
            raise AssertionError("cancelled repair should not update chapters")

        def update_book(self, book_id, **updates):
            self.book_updates.append((book_id, updates))
            raise AssertionError("cancelled repair should not update the book")

    class _RepairPipeline:
        cancel_seen = threading.Event()
        init_started = threading.Event()
        release_init = threading.Event()

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            self.config = kwargs["config"]
            self._tts_engine = None
            self._selected_parallel_chapters = 1
            type(self).init_started.set()
            assert type(self).release_init.wait(timeout=3), "repair init release timed out"

        def cancel(self) -> None:
            type(self).cancel_seen.set()

        def stage_ingest(self, source_path):  # noqa: ANN001
            _ = source_path
            return SimpleNamespace(
                chapters=[SimpleNamespace(number=1, title="One", content="Text one.")]
            )

        def stage_process_chapter(self, **kwargs):  # noqa: ANN003
            _ = kwargs
            raise AssertionError("cancelled repair should not process chapters")

        def stage_package(self, **kwargs):  # noqa: ANN003
            _ = kwargs
            raise AssertionError("cancelled repair should not package")

        def stage_cleanup_models(self, *, force: bool = False) -> None:
            _ = force

        def stage_cancel_tts_idle_unload(self) -> None:
            pass

    state_events = []

    def _on_event(event) -> None:
        if getattr(event, "event_type", None) == EventType.STATE:
            state_events.append(event.state)

    repo = _RepairRepository()
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    service = ConversionService(
        config=config,
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=_RepairPipeline,
    )
    job = service.start_repair(
        book_id=77,
        callbacks=ConversionCallbacks(on_event=_on_event),
        runtime_options=_default_runtime_options(),
    )
    assert _RepairPipeline.init_started.wait(timeout=3)
    assert service.cancel_conversion() is True
    _RepairPipeline.release_init.set()
    assert job.wait(timeout=3)

    assert _RepairPipeline.cancel_seen.is_set()
    assert repo.chapter_updates == []
    assert repo.book_updates == []
    assert job.status == JobStatus.CANCELLED
    assert state_events[-1] == JobState.CANCELLED
    service.cleanup()


def test_conversion_service_repair_cancelled_chapter_result_finishes_cancelled(tmp_path):
    source_path = tmp_path / "source.md"
    source_path.write_text("# One\n\nText one.", encoding="utf-8")

    class _RepairRepository:
        def __init__(self) -> None:
            self.chapter_updates = []
            self.book_updates = []
            self.book = Book(
                id=77,
                title="Repair Book",
                author="Tester",
                source_path=str(source_path),
                source_type=SourceType.MD,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path=None,
                playback_status=PlaybackStatus.CHAPTER_AUDIO,
                playback_position_sec=0.0,
                created_at=__import__("datetime").datetime(2026, 4, 1, 9, 0, 0),
                updated_at=__import__("datetime").datetime(2026, 4, 1, 9, 5, 0),
            )
            self.chapters = [Chapter(1, 77, 1, "One", None, None)]

        def get_book_with_chapters(self, book_id):
            if book_id != 77:
                return None
            return self.book, list(self.chapters)

        def update_chapter(self, chapter_id, duration_ms=None, mp3_path=None):
            self.chapter_updates.append((chapter_id, duration_ms, mp3_path))
            raise AssertionError("cancelled repair should not update chapters")

        def update_book(self, book_id, **updates):
            self.book_updates.append((book_id, updates))
            raise AssertionError("cancelled repair should not update the book")

    class _RepairPipeline:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            self.config = kwargs["config"]
            self._tts_engine = None
            self._selected_parallel_chapters = 1

        def repair(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            return _delegate_repair_to_pipeline_module(self, **kwargs)

        def stage_ingest(self, source_path):  # noqa: ANN001
            _ = source_path
            return SimpleNamespace(
                chapters=[SimpleNamespace(number=1, title="One", content="Text one.")]
            )

        def stage_process_chapter(self, **kwargs):  # noqa: ANN003
            _ = kwargs
            return ChapterResult(
                chapter_number=1,
                chapter_title="One",
                error="Cancelled",
            )

        def stage_package(self, **kwargs):  # noqa: ANN003
            _ = kwargs
            raise AssertionError("cancelled repair should not package")

        def stage_cleanup_models(self, *, force: bool = False) -> None:
            _ = force

        def stage_cancel_tts_idle_unload(self) -> None:
            pass

    state_events = []

    def _on_event(event) -> None:
        if getattr(event, "event_type", None) == EventType.STATE:
            state_events.append(event.state)

    repo = _RepairRepository()
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    service = ConversionService(
        config=config,
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=_RepairPipeline,
    )

    job = _start_repair_and_wait(
        service,
        book_id=77,
        callbacks=ConversionCallbacks(on_event=_on_event),
    )

    assert repo.chapter_updates == []
    assert repo.book_updates == []
    assert job.status == JobStatus.CANCELLED
    assert state_events[-1] == JobState.CANCELLED
    service.cleanup()


def test_conversion_service_repair_failure_after_partial_work_does_not_mutate_library_metadata(
    tmp_path,
):
    source_path = tmp_path / "source.md"
    source_path.write_text("# One\n\nText one.\n\n# Two\n\nText two.", encoding="utf-8")
    output_dir = tmp_path / "output" / "Repair Book"
    chapters_dir = output_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    existing_audio = chapters_dir / "01_one.mp3"
    existing_audio.write_bytes(b"original-one")
    original_playback_path = str(output_dir / "Repair Book.m4b")
    original_second_duration_ms = 1500
    original_second_mp3_path = str(chapters_dir / "02_two_original.mp3")

    class _PartialFailureRepairRepository:
        def __init__(self) -> None:
            self.chapter_updates = []
            self.book_updates = []
            self.book = Book(
                id=77,
                title="Repair Book",
                author="Tester",
                source_path=str(source_path),
                source_type=SourceType.MD,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=2,
                playback_path=original_playback_path,
                playback_status=PlaybackStatus.CHAPTER_AUDIO,
                playback_position_sec=0.0,
                created_at=__import__("datetime").datetime(2026, 4, 1, 9, 0, 0),
                updated_at=__import__("datetime").datetime(2026, 4, 1, 9, 5, 0),
            )
            self.chapters = [
                Chapter(1, 77, 1, "One", 1000, str(existing_audio)),
                Chapter(2, 77, 2, "Two", original_second_duration_ms, original_second_mp3_path),
            ]

        def get_book_with_chapters(self, book_id):
            if book_id != 77:
                return None
            return self.book, list(self.chapters)

        def update_chapter(self, chapter_id, duration_ms=None, mp3_path=None):
            self.chapter_updates.append((chapter_id, duration_ms, mp3_path))
            chapter = next(chapter for chapter in self.chapters if chapter.id == chapter_id)
            chapter.duration_ms = duration_ms
            chapter.mp3_path = mp3_path
            return chapter

        def update_book(self, book_id, **updates):
            self.book_updates.append((book_id, updates))
            self.book.playback_path = updates.get("playback_path", self.book.playback_path)
            self.book.playback_status = updates.get("playback_status", self.book.playback_status)
            self.book.total_chapters = updates.get("total_chapters", self.book.total_chapters)
            return self.book

    class _PackageFailureRepairPipeline:
        repaired_artifact = chapters_dir / "02_two_repaired.mp3"

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            _ = kwargs

        def repair(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            return _delegate_repair_to_pipeline_module(self, **kwargs)

        def stage_ingest(self, source_path):  # noqa: ANN001
            _ = source_path
            return SimpleNamespace(
                chapters=[
                    SimpleNamespace(number=1, title="One", content="Text one."),
                    SimpleNamespace(number=2, title="Two", content="Text two."),
                ]
            )

        def stage_process_chapter(self, *, chapter, chapter_idx, total_chapters, output_dir):  # noqa: ANN001
            _ = (chapter_idx, total_chapters)
            repaired = output_dir / f"{chapter.number:02d}_two_repaired.mp3"
            repaired.write_bytes(b"repaired-two")
            type(self).repaired_artifact = repaired
            return ChapterResult(
                chapter_number=chapter.number,
                chapter_title=chapter.title,
                mp3_path=repaired,
                duration_ms=2200,
            )

        def stage_package(self, chapter_results, title, author, output_dir):  # noqa: ANN001
            _ = (chapter_results, title, author, output_dir)
            raise RuntimeError("injected repair package failure")

    repo = _PartialFailureRepairRepository()
    service = ConversionService(
        config=AppConfig(
            data_dir=tmp_path / "data",
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            db_path=tmp_path / "data" / "app.db",
        ),
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=_PackageFailureRepairPipeline,
    )

    job = _start_repair_and_wait(service, book_id=77)

    assert job.status == JobStatus.FAILED
    assert job.book_id == 77
    assert job.result is not None
    assert job.result.success is False
    assert repo.chapter_updates == []
    assert repo.book_updates == []
    assert repo.chapters[0].duration_ms == 1000
    assert repo.chapters[0].mp3_path == str(existing_audio)
    assert repo.chapters[1].duration_ms == original_second_duration_ms
    assert repo.chapters[1].mp3_path == original_second_mp3_path
    assert repo.book.playback_path == original_playback_path
    assert repo.book.playback_status == PlaybackStatus.CHAPTER_AUDIO
    assert repo.book.total_chapters == 2
    assert _PackageFailureRepairPipeline.repaired_artifact.exists()
    stored_paths = {chapter.mp3_path for chapter in repo.chapters}
    assert str(_PackageFailureRepairPipeline.repaired_artifact) not in stored_paths
    assert job.error is not None
    assert "injected repair package failure" in str(job.error).lower()
    assert "injected repair package failure" in job.result.error_message.lower()


def test_conversion_service_repair_fails_when_stored_chapter_rows_are_missing(tmp_path):
    source_path = tmp_path / "source.md"
    source_path.write_text("# One\n\nText one.\n\n# Two\n\nText two.", encoding="utf-8")
    output_dir = tmp_path / "output" / "Repair Book"
    chapters_dir = output_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    existing_audio = chapters_dir / "01_one.mp3"
    existing_audio.write_bytes(b"mp3")

    class _IncompleteRepairRepository:
        def __init__(self) -> None:
            self.chapter_updates = []
            self.book_updates = []
            self.book = Book(
                id=77,
                title="Repair Book",
                author="Tester",
                source_path=str(source_path),
                source_type=SourceType.MD,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=2,
                playback_path=None,
                playback_status=PlaybackStatus.CHAPTER_AUDIO,
                playback_position_sec=0.0,
                created_at=__import__("datetime").datetime(2026, 4, 1, 9, 0, 0),
                updated_at=__import__("datetime").datetime(2026, 4, 1, 9, 5, 0),
            )
            self.chapters = [
                Chapter(1, 77, 1, "One", 1000, str(existing_audio)),
            ]

        def get_book_with_chapters(self, book_id):
            if book_id != 77:
                return None
            return self.book, list(self.chapters)

        def update_chapter(self, chapter_id, duration_ms=None, mp3_path=None):
            self.chapter_updates.append((chapter_id, duration_ms, mp3_path))
            return None

        def update_book(self, book_id, **updates):
            self.book_updates.append((book_id, updates))
            self.book.playback_path = updates.get("playback_path", self.book.playback_path)
            self.book.playback_status = updates.get("playback_status", self.book.playback_status)
            self.book.total_chapters = updates.get("total_chapters", self.book.total_chapters)
            return self.book

    class _IncompleteRepairPipeline:
        package_calls = []

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            _ = kwargs

        def repair(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            return _delegate_repair_to_pipeline_module(self, **kwargs)

        def stage_ingest(self, source_path):  # noqa: ANN001
            _ = source_path
            return SimpleNamespace(
                chapters=[
                    SimpleNamespace(number=1, title="One", content="Text one."),
                    SimpleNamespace(number=2, title="Two", content="Text two."),
                ]
            )

        def stage_process_chapter(self, *, chapter, chapter_idx, total_chapters, output_dir):  # noqa: ANN001
            _ = (chapter, chapter_idx, total_chapters, output_dir)
            raise AssertionError("repair should fail before processing")

        def stage_package(self, chapter_results, title, author, output_dir):  # noqa: ANN001
            type(self).package_calls.append((chapter_results, title, author, output_dir))
            final = output_dir / "Repair Book.m4b"
            final.write_bytes(b"m4b")
            return final

    repo = _IncompleteRepairRepository()
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
    )
    service = ConversionService(
        config=config,
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=_IncompleteRepairPipeline,
    )

    job = _start_repair_and_wait(service, book_id=77)

    assert job.status == JobStatus.FAILED
    assert _IncompleteRepairPipeline.package_calls == []
    assert repo.book_updates == []
    assert repo.chapter_updates == []
    assert repo.book.playback_status == PlaybackStatus.CHAPTER_AUDIO
    assert repo.book.total_chapters == 2
    assert job.error is not None
    assert "missing" in str(job.error).lower()
    assert "chapter" in str(job.error).lower()
    assert "structure" in str(job.error).lower()


def test_conversion_service_repair_fails_when_stored_chapter_title_mismatches_source(tmp_path):
    source_path = tmp_path / "source.md"
    source_path.write_text("# One\n\nText one.\n\n# Two\n\nText two.", encoding="utf-8")
    output_dir = tmp_path / "output" / "Repair Book"
    chapters_dir = output_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    existing_audio = chapters_dir / "01_one.mp3"
    existing_audio.write_bytes(b"mp3")

    class _TitleMismatchRepairRepository:
        def __init__(self) -> None:
            self.chapter_updates = []
            self.book_updates = []
            self.book = Book(
                id=77,
                title="Repair Book",
                author="Tester",
                source_path=str(source_path),
                source_type=SourceType.MD,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=2,
                playback_path=None,
                playback_status=PlaybackStatus.CHAPTER_AUDIO,
                playback_position_sec=0.0,
                created_at=__import__("datetime").datetime(2026, 4, 1, 9, 0, 0),
                updated_at=__import__("datetime").datetime(2026, 4, 1, 9, 5, 0),
            )
            self.chapters = [
                Chapter(1, 77, 1, "One", 1000, str(existing_audio)),
                Chapter(2, 77, 2, "Stored Two", None, None),
            ]

        def get_book_with_chapters(self, book_id):
            if book_id != 77:
                return None
            return self.book, list(self.chapters)

        def update_chapter(self, chapter_id, duration_ms=None, mp3_path=None):
            self.chapter_updates.append((chapter_id, duration_ms, mp3_path))
            return None

        def update_book(self, book_id, **updates):
            self.book_updates.append((book_id, updates))
            return self.book

    class _TitleMismatchRepairPipeline:
        package_calls = []

        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            _ = kwargs

        def repair(self, **kwargs) -> PipelineConversionResult:  # noqa: ANN003
            return _delegate_repair_to_pipeline_module(self, **kwargs)

        def stage_ingest(self, source_path):  # noqa: ANN001
            _ = source_path
            return SimpleNamespace(
                chapters=[
                    SimpleNamespace(number=1, title="One", content="Text one."),
                    SimpleNamespace(number=2, title="Source Two", content="Text two."),
                ]
            )

        def stage_process_chapter(self, *, chapter, chapter_idx, total_chapters, output_dir):  # noqa: ANN001
            _ = (chapter, chapter_idx, total_chapters, output_dir)
            raise AssertionError("repair should fail before processing")

        def stage_package(self, chapter_results, title, author, output_dir):  # noqa: ANN001
            type(self).package_calls.append((chapter_results, title, author, output_dir))
            return output_dir / "Repair Book.m4b"

    repo = _TitleMismatchRepairRepository()
    service = ConversionService(
        config=AppConfig(
            data_dir=tmp_path / "data",
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            db_path=tmp_path / "data" / "app.db",
        ),
        repository=repo,
        job_cls=ConversionJob,
        status_enum=JobStatus,
        cleaner_api_limiter=_LimiterStub(),
        runtime_validator=lambda *args, **kwargs: None,
        shared_worker_pool_manager=None,
        pipeline_cls=_TitleMismatchRepairPipeline,
    )

    job = _start_repair_and_wait(service, book_id=77)

    assert job.status == JobStatus.FAILED
    assert _TitleMismatchRepairPipeline.package_calls == []
    assert repo.book_updates == []
    assert repo.chapter_updates == []
    assert job.error is not None
    assert "title mismatch" in str(job.error).lower()
    assert "chapter 2" in str(job.error).lower()
