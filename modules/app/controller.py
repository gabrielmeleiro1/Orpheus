"""
Application Controller
======================
Central façade that delegates business logic to focused app services.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from modules.app.config import AppConfig
from modules.app.services import (
    ConversionRuntimeOptions,
    ConversionService,
    LibraryService,
    RuntimeCapabilityService,
    VoiceService,
)
from modules.app.services.conversion_result_adapter import pipeline_to_controller_result
from modules.pipeline.orchestrator import ConversionPipeline
from modules.pipeline.pool_manager import SharedWorkerPoolManager
from modules.pipeline.worker_recommendation import (
    build_cpu_core_worker_recommendation,
    build_parallel_worker_recommendation,
)
from modules.storage.maintenance import (
    audit_storage_state,
    backup_database as backup_database_file,
    prune_stale_records_and_files,
    workspace_root_for_db,
)
from modules.storage.models import BookSummary, ConversionResult
from modules.storage.repository import IBookRepository
from modules.storage.sqlite_repo import SQLiteRepository
from modules.tts.api_rate_limit import GlobalAPILimiter
from modules.tts.cleaner_config import list_cleaner_backends
from modules.tts.factory import TTSEngineFactory


class JobStatus(Enum):
    """Conversion job status."""

    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ConversionJob:
    """Handle for an active conversion job."""

    id: str
    book_id: Optional[int]
    status: JobStatus
    pipeline: Optional[Any] = None
    result: Optional[ConversionResult] = None
    error: Optional[str] = None
    cancel_requested: bool = False
    _thread: Optional[threading.Thread] = field(default=None, repr=False)

    def cancel(self) -> bool:
        if self.status not in {JobStatus.IDLE, JobStatus.RUNNING}:
            return False

        self.cancel_requested = True
        if self.pipeline:
            self.pipeline.cancel()
        return True

    def is_active(self) -> bool:
        return self.status in {JobStatus.IDLE, JobStatus.RUNNING}

    def wait(self, timeout: Optional[float] = None) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()


@dataclass
class ConversionCallbacks:
    """Callbacks for conversion progress updates."""

    on_progress: Optional[Callable[[str, float, str], None]] = None
    on_stage_change: Optional[Callable[[str], None]] = None
    on_chapter_complete: Optional[Callable[[int, int], None]] = None
    on_log: Optional[Callable[[str, str], None]] = None
    on_complete: Optional[Callable[[ConversionResult], None]] = None
    on_error: Optional[Callable[[str], None]] = None
    on_event: Optional[Callable[[Any], None]] = None


class AppController:
    """Central application controller with compatibility-preserving API."""

    _CLEANUP_CONVERSION_CANCEL_TIMEOUT_SECONDS = 2.0
    _CLEANUP_WORKER_SHUTDOWN_TIMEOUT_SECONDS = 2.0
    _CLEANUP_POST_WORKER_CANCEL_TIMEOUT_SECONDS = 0.5

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        repository: Optional[IBookRepository] = None,
    ):
        self.config = config or AppConfig()
        self._owns_repository = repository is None
        self.repository = repository or SQLiteRepository(
            self.config.db_path,
            output_dir=self.config.output_dir,
            temp_dir=self.config.temp_dir,
        )

        self._cleaner_api_limiter = GlobalAPILimiter(self.config.cleaner_api_global_concurrency)
        self._shared_worker_pool_manager: Optional[SharedWorkerPoolManager] = None
        if self.config.worker_pool_reuse_enabled:
            self._shared_worker_pool_manager = SharedWorkerPoolManager(
                max_workers=self.config.max_parallel_chapters,
            )

        self._library_service = LibraryService(
            self.repository,
            output_dir=self.config.output_dir,
            temp_dir=self.config.temp_dir,
            storage_root=workspace_root_for_db(self.config.db_path),
        )
        self._voice_service = VoiceService(
            self.config,
            tts_factory=TTSEngineFactory,
        )
        self._runtime_capability_service = RuntimeCapabilityService()
        self._conversion_service = ConversionService(
            config=self.config,
            repository=self.repository,
            job_cls=ConversionJob,
            status_enum=JobStatus,
            cleaner_api_limiter=self._cleaner_api_limiter,
            runtime_validator=self._voice_service.validate_tts_runtime,
            shared_worker_pool_manager=self._shared_worker_pool_manager,
        )

    # ==================== Library ====================

    def get_library_books(
        self,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        conversion_mode=None,
        favorite: Optional[bool] = None,
    ) -> list[BookSummary]:
        return self._library_service.get_library_books(
            search=search,
            limit=limit,
            offset=offset,
            conversion_mode=conversion_mode,
            favorite=favorite,
        )

    def get_book(self, book_id: int) -> Optional[dict]:
        return self._library_service.get_book(book_id)

    def upload_source(self, *, filename: str, file_obj, max_bytes: int) -> dict:
        return self._library_service.upload_source(
            filename=filename,
            file_obj=file_obj,
            max_bytes=max_bytes,
        )

    def upload_text_source(self, *, text: str, title: str | None, max_bytes: int) -> dict:
        return self._library_service.upload_text_source(
            text=text,
            title=title,
            max_bytes=max_bytes,
        )

    def preflight_source(self, source_path: Path | str) -> dict:
        return self._library_service.preflight_source(source_path)

    def get_book_playback_file(self, book_id: int) -> Optional[Path]:
        return self._library_service.get_book_playback_file(book_id)

    def update_book_playback_position(self, book_id: int, position_sec: float) -> Optional[float]:
        return self._library_service.update_book_playback_position(book_id, position_sec)

    def update_book_metadata(
        self,
        book_id: int,
        *,
        title: Optional[str] = None,
        author: Optional[str] = None,
        tags: Optional[str] = None,
        favorite: Optional[bool] = None,
    ) -> Optional[dict]:
        return self._library_service.update_book_metadata(
            book_id,
            title=title,
            author=author,
            tags=tags,
            favorite=favorite,
        )

    def get_chapter_cleaning_preview(self, book_id: int, chapter_number: int) -> Optional[dict]:
        return self._library_service.get_chapter_cleaning_preview(book_id, chapter_number)

    def delete_book(self, book_id: int) -> bool:
        return self._library_service.delete_book(book_id)

    def open_book_folder(self, book_id: int) -> Optional[Path]:
        return self._library_service.open_book_folder(book_id)

    def get_library_stats(self) -> dict:
        return self._library_service.get_library_stats()

    def get_storage_diagnostics(self) -> dict:
        db_path = Path(self.config.db_path)
        audit = audit_storage_state(
            db_path=db_path,
            output_dir=self.config.output_dir,
            temp_dir=self.config.temp_dir,
        )
        return {
            "database_path": str(db_path.resolve()),
            "output_path": str(self.config.output_dir.resolve()),
            "temp_path": str(self.config.temp_dir.resolve()),
            "app_data_path": str(workspace_root_for_db(db_path)),
            "audit": audit,
        }

    def backup_database(self) -> Path:
        return backup_database_file(self.config.db_path)

    def cleanup_storage(self, *, confirm: str) -> dict:
        if str(confirm).strip().lower() != "cleanup":
            raise ValueError("Cleanup requires confirm='cleanup'")
        return prune_stale_records_and_files(
            db_path=self.config.db_path,
            output_dir=self.config.output_dir,
            temp_dir=self.config.temp_dir,
            aggressive=True,
        )

    # ==================== Conversion ====================

    def start_conversion(
        self,
        source_path: Path,
        callbacks: Optional[ConversionCallbacks] = None,
        conversion_mode: str = "audiobook",
        title: Optional[str] = None,
        author: Optional[str] = None,
        runtime_options: Optional[ConversionRuntimeOptions] = None,
    ) -> ConversionJob:
        return self._conversion_service.start_conversion(
            source_path=source_path,
            callbacks=callbacks,
            conversion_mode=conversion_mode,
            title=title,
            author=author,
            runtime_options=runtime_options,
        )

    def get_active_job(self) -> Optional[ConversionJob]:
        return self._conversion_service.get_active_job()

    def cancel_conversion(self) -> bool:
        return self._conversion_service.cancel_conversion()

    def repair_conversion(
        self,
        book_id: int,
        callbacks: Optional[ConversionCallbacks] = None,
        runtime_options: Optional[ConversionRuntimeOptions] = None,
    ) -> ConversionJob:
        return self._conversion_service.start_repair(
            book_id=book_id,
            callbacks=callbacks,
            runtime_options=runtime_options,
        )

    # Compatibility wrappers retained for tests/patching internals.
    def _validate_tts_runtime(
        self,
        tts_engine: str,
        tts_quantization: Optional[str] = None,
        *,
        tts_provider: Optional[str] = None,
        tts_device: Optional[str] = None,
        tts_options: Optional[dict[str, Any]] = None,
        runtime_spec: Optional[Any] = None,
    ) -> None:
        self._voice_service.validate_tts_runtime(
            tts_engine,
            tts_quantization,
            tts_provider=tts_provider,
            tts_device=tts_device,
            tts_options=tts_options,
            runtime_spec=runtime_spec,
        )

    def _create_pipeline_config(
        self,
        conversion_mode: str = "audiobook",
        runtime_options: Optional[ConversionRuntimeOptions] = None,
    ):
        return self._conversion_service._create_pipeline_config(  # noqa: SLF001
            conversion_mode=conversion_mode,
            runtime_options=runtime_options,
        )

    def _create_pipeline_callbacks(self, callbacks: Optional[ConversionCallbacks]):
        return self._conversion_service._create_pipeline_callbacks(callbacks)  # noqa: SLF001

    @staticmethod
    def _to_controller_result(pipeline_result) -> ConversionResult:
        return pipeline_to_controller_result(pipeline_result)

    def _persist_conversion_library_records(
        self,
        source_path: Path,
        pipeline_result,
        conversion_mode: str = "audiobook",
    ) -> int:
        return self._conversion_service._persist_conversion_library_records(  # noqa: SLF001
            source_path,
            pipeline_result,
            conversion_mode=conversion_mode,
        )

    def _handle_conversion_success(
        self,
        job: ConversionJob,
        source_path: Path,
        pipeline_result,
        callbacks: Optional[ConversionCallbacks],
    ) -> None:
        self._conversion_service._handle_conversion_success(  # noqa: SLF001
            job=job,
            source_path=source_path,
            pipeline_result=pipeline_result,
            callbacks=callbacks,
        )

    def _handle_conversion_failure(
        self,
        job: ConversionJob,
        error_message: str,
        callbacks: Optional[ConversionCallbacks],
    ) -> None:
        self._conversion_service._handle_conversion_failure(  # noqa: SLF001
            job=job,
            error_message=error_message,
            callbacks=callbacks,
        )

    def _run_conversion(
        self,
        job: ConversionJob,
        source_path: Path,
        callbacks: Optional[ConversionCallbacks],
        conversion_mode: str = "audiobook",
        title: Optional[str] = None,
        author: Optional[str] = None,
        runtime_options: Optional[ConversionRuntimeOptions] = None,
    ) -> None:
        self._conversion_service._run_conversion(  # noqa: SLF001
            job=job,
            source_path=source_path,
            callbacks=callbacks,
            conversion_mode=conversion_mode,
            title=title,
            author=author,
            runtime_options=runtime_options,
        )

    # ==================== Voice ====================

    def generate_voice_preview(
        self,
        voice: str,
        speed: float = 1.0,
        text: Optional[str] = None,
        engine_name: Optional[str] = None,
        tts_quantization: Optional[str] = None,
        tts_provider: Optional[str] = None,
        tts_device: Optional[str] = None,
        tts_options: Optional[dict[str, Any]] = None,
        runtime_spec: Optional[Any] = None,
    ) -> Path:
        return self._voice_service.generate_voice_preview(
            voice=voice,
            speed=speed,
            text=text,
            engine_name=engine_name,
            tts_quantization=tts_quantization,
            tts_provider=tts_provider,
            tts_device=tts_device,
            tts_options=tts_options,
            runtime_spec=runtime_spec,
        )

    def get_available_voices(self, engine_name: Optional[str] = None) -> list[dict]:
        return self._voice_service.get_available_voices(engine_name=engine_name)

    def suggest_voice_for_book(
        self,
        source_path: Path,
        engine_name: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._voice_service.suggest_voice_for_book(
            source_path=source_path, engine_name=engine_name
        )

    def get_available_tts_engines(self) -> list[dict]:
        return self._voice_service.get_available_tts_engines()

    def get_available_cleaner_models(self) -> list[str]:
        return self._voice_service.get_available_cleaner_models()

    def get_available_api_cleaner_models(self) -> list[str]:
        return self._voice_service.get_available_api_cleaner_models()

    def get_system_capabilities(self) -> dict[str, Any]:
        runtime_probe = self._runtime_capability_service.get_system_runtime_snapshot()
        mlx_runtime_available = bool(runtime_probe["tts"]["available"])
        mlx_cleaner_runtime_available = bool(runtime_probe["cleaner"]["available"])

        limiter_snapshot = self._cleaner_api_limiter.snapshot()
        pool_snapshot: dict[str, Any] = {}
        if self._shared_worker_pool_manager is not None:
            pool_snapshot = self._shared_worker_pool_manager.snapshot()

        metrics = self._voice_service.metrics_snapshot()
        metrics.update(
            {
                "worker_pool_reuse_hits": pool_snapshot.get("reuse_hits", 0),
                "worker_pool_restarts": pool_snapshot.get("restarts", 0),
                "global_api_limiter_wait_count": limiter_snapshot.get("wait_count", 0),
                "global_api_limiter_wait_ms": limiter_snapshot.get("wait_ms_total", 0.0),
            }
        )

        return {
            "primary_runtime": "mlx",
            "mlx_runtime_available": mlx_runtime_available,
            "mlx_cleaner_runtime_available": mlx_cleaner_runtime_available,
            "default_tts_engine": self.config.tts_engine,
            "default_tts_quantization": self.config.tts_quantization,
            "available_tts_engines": TTSEngineFactory.available_conversion_engines(),
            "cleaner_backends": list_cleaner_backends(),
            "parallel_processing_enabled": bool(self.config.enable_parallel),
            "runtime_validation_cache_size": self._voice_service.runtime_validation_cache_size,
            "runtime_probe": runtime_probe,
            "global_api_limiter": limiter_snapshot,
            "worker_pool": pool_snapshot,
            "parallel_worker_recommendation": build_parallel_worker_recommendation(
                requested_max=max(
                    4,
                    int(getattr(self.config, "max_parallel_chapters", 1) or 1),
                ),
            ),
            "metrics": metrics,
        }

    def warmup_parallel_worker_recommendation(
        self,
        *,
        voice: str,
        speed: float,
        tts_engine: Optional[str] = None,
        tts_quantization: Optional[str] = None,
        cleaner_model: Optional[str] = None,
        cleaner_backend: Optional[str] = None,
        cleaner_api_model: Optional[str] = None,
        cleaner_api_key: Optional[str] = None,
        max_parallel_chapters: int = 4,
    ) -> dict[str, Any]:
        pipeline_config = self._conversion_service._create_pipeline_config(  # noqa: SLF001
            runtime_options=ConversionRuntimeOptions(
                voice=voice,
                speed=speed,
                tts_engine=tts_engine,
                tts_quantization=tts_quantization,
                cleaner_model=cleaner_model,
                cleaner_backend=cleaner_backend,
                cleaner_api_model=cleaner_api_model,
                cleaner_api_key=cleaner_api_key,
                parallel_worker_mode="auto",
                enable_parallel=True,
                max_parallel_chapters=max_parallel_chapters,
            ),
        )
        pipeline = ConversionPipeline(config=pipeline_config)
        try:
            if str(
                getattr(pipeline.config, "tts_worker_auto_mode", "") or ""
            ).strip().lower() == "cpu_cores" and bool(
                getattr(pipeline.config, "tts_process_safe", False)
            ):
                return build_cpu_core_worker_recommendation(
                    requested_max=pipeline.config.max_parallel_chapters,
                )
            pipeline.stages.warmup_models()
            policy = pipeline.config.memory_policy
            return build_parallel_worker_recommendation(
                requested_max=max_parallel_chapters,
                estimated_worker_bytes=getattr(
                    policy,
                    "estimated_per_worker_memory_bytes",
                    None,
                ),
                warmup_delta_bytes=getattr(
                    policy,
                    "measured_warmup_delta_bytes",
                    None,
                ),
                selected_worker_count_reason=getattr(
                    policy,
                    "selected_worker_count_reason",
                    None,
                ),
            )
        finally:
            pipeline.stages.cleanup_models(force=True)

    # ==================== Cleanup ====================

    def _cancel_active_conversion_for_cleanup(self, timeout_seconds: float) -> bool:
        cancel_active_job = getattr(self._conversion_service, "cancel_active_job", None)
        if callable(cancel_active_job):
            return bool(cancel_active_job(timeout_seconds=timeout_seconds))
        return not bool(self.cancel_conversion())

    def _shutdown_shared_worker_pool_for_cleanup(self) -> bool:
        if self._shared_worker_pool_manager is None:
            return True

        try:
            return bool(
                self._shared_worker_pool_manager.shutdown(
                    timeout_seconds=self._CLEANUP_WORKER_SHUTDOWN_TIMEOUT_SECONDS,
                )
            )
        except TypeError:
            self._shared_worker_pool_manager.shutdown()
            return True

    def cleanup(self) -> None:
        self._cancel_active_conversion_for_cleanup(
            self._CLEANUP_CONVERSION_CANCEL_TIMEOUT_SECONDS,
        )
        self._shutdown_shared_worker_pool_for_cleanup()
        cleanup_conversion_service = getattr(self._conversion_service, "cleanup", None)
        if callable(cleanup_conversion_service):
            cleanup_conversion_service()
        self._cancel_active_conversion_for_cleanup(
            self._CLEANUP_POST_WORKER_CANCEL_TIMEOUT_SECONDS,
        )
        self._voice_service.cleanup()
        self._cleaner_api_limiter.shutdown()
        if self._owns_repository:
            close = getattr(self.repository, "close", None)
            if callable(close):
                close()
