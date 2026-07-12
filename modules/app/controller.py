"""
Application Controller
======================
Central façade that delegates business logic to focused app services.
"""

from __future__ import annotations

import json
import os
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
from modules.tts.cleaner_api import fetch_openai_model_ids
from modules.tts.cleaner_backends.metadata import list_cleaner_backend_options
from modules.tts.cleaner_backends.registry import (
    available_cleaner_backends,
    requires_api_key_cleaner_backend,
)
from modules.tts.cleaner_config import (
    DEFAULT_API_BASE_URL,
    list_cleaner_backends,
    list_cuda_cleaner_models,
)
from modules.tts.device_memory import get_device_memory_policy
from modules.tts.factory import TTSEngineFactory
from modules.tts.runtime_spec import TTSRuntimeSpec


class JobStatus(Enum):
    """Conversion job status."""

    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ConversionJob:
    """Handle for an active conversion job.

    All status/pipeline/result fields are mutated from the worker thread while
    the request thread reads snapshots (/active, SSE) and requests cancellation.
    A single per-job lock guards every compound transition and multi-field read
    so callers never observe a torn state. The lock is never held across
    ``pipeline.cancel()`` or other long operations: the pipeline reference is
    copied out under the lock and the call happens after release.
    """

    id: str
    book_id: Optional[int]
    status: JobStatus
    pipeline: Optional[Any] = None
    result: Optional[ConversionResult] = None
    error: Optional[str] = None
    cancel_requested: bool = False
    _thread: Optional[threading.Thread] = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def cancel(self) -> bool:
        with self._lock:
            if self.status not in {JobStatus.IDLE, JobStatus.RUNNING}:
                return False
            self.cancel_requested = True
            pipeline = self.pipeline
        if pipeline is not None:
            pipeline.cancel()
        return True

    def is_active(self) -> bool:
        with self._lock:
            return self.status in {JobStatus.IDLE, JobStatus.RUNNING}

    def wait(self, timeout: Optional[float] = None) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    # ---- Synchronized transitions (called by the worker thread) ----

    def begin_running(self, thread: Optional[threading.Thread] = None) -> None:
        with self._lock:
            if thread is not None:
                self._thread = thread
            self.status = JobStatus.RUNNING

    def attach_pipeline(self, pipeline: Any) -> bool:
        """Attach the running pipeline; return True if cancel was already asked."""
        with self._lock:
            self.pipeline = pipeline
            return self.cancel_requested

    def detach_pipeline(self) -> None:
        with self._lock:
            self.pipeline = None

    def set_result(self, result: Optional[ConversionResult]) -> None:
        with self._lock:
            self.result = result

    def complete(self, book_id: Optional[int]) -> None:
        with self._lock:
            self.status = JobStatus.COMPLETED
            self.book_id = book_id

    def fail(self, error_message: str, failure_result: ConversionResult) -> None:
        with self._lock:
            self.status = JobStatus.FAILED
            self.error = error_message
            if self.result is None or bool(getattr(self.result, "success", False)):
                self.result = failure_result

    def mark_cancelled(self, message: str) -> None:
        with self._lock:
            self.status = JobStatus.CANCELLED
            self.error = message

    def read_state(self) -> tuple[str, Optional[int], JobStatus, Optional[str]]:
        """Return a consistent (id, book_id, status, error) snapshot."""
        with self._lock:
            return (self.id, self.book_id, self.status, self.error)


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
    # Desktop supervisor should allow at least this budget after SIGTERM so cleanup
    # can cancel active conversions, shut down workers, and close SQLite safely.
    DESKTOP_SHUTDOWN_CLEANUP_BUDGET_SECONDS = 5.0
    DESKTOP_SHUTDOWN_ACK_EVENT = "shutdown"

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

    def sweep_stale_uploads(self) -> int:
        return self._library_service.sweep_stale_uploads()

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
        active_job = self._conversion_service.get_active_job()
        if (
            active_job is not None
            and getattr(active_job, "book_id", None) == book_id
            and self._conversion_service.has_active_job()
        ):
            raise RuntimeError(
                "Cannot delete a book while it is being converted or repaired"
            )
        return self._library_service.delete_book(book_id)

    def open_book_folder(self, book_id: int) -> Optional[Path]:
        return self._library_service.open_book_folder(book_id)

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
        # begin_storage_cleanup atomically rejects an active job and blocks new
        # conversion starts until cleanup finishes (no check-then-prune race).
        self._conversion_service.begin_storage_cleanup()
        try:
            return prune_stale_records_and_files(
                db_path=self.config.db_path,
                output_dir=self.config.output_dir,
                temp_dir=self.config.temp_dir,
                aggressive=True,
            )
        finally:
            self._conversion_service.end_storage_cleanup()

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
        runtime_fields_set: Optional[set[str] | frozenset[str]] = None,
    ) -> ConversionJob:
        return self._conversion_service.start_repair(
            book_id=book_id,
            callbacks=callbacks,
            runtime_options=runtime_options,
            runtime_fields_set=runtime_fields_set,
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

    def get_cuda_cleaner_models(self) -> list[str]:
        return list_cuda_cleaner_models()

    def get_cleaner_backend_options(self) -> list[dict[str, Any]]:
        return list_cleaner_backend_options()

    def list_openai_models(self, api_key: str) -> dict[str, Any]:
        models = fetch_openai_model_ids(api_key=api_key, base_url=DEFAULT_API_BASE_URL)
        return {
            "provider": "openai",
            "base_url": DEFAULT_API_BASE_URL,
            "models": models,
        }

    def get_system_capabilities(self) -> dict[str, Any]:
        runtime_probe = self._runtime_capability_service.get_system_runtime_snapshot()
        mlx_runtime_available = bool(runtime_probe["tts"]["available"])
        cleaner_runtime_availability = (
            self._runtime_capability_service.get_cached_cleaner_backend_availability()
        )
        for backend_name in available_cleaner_backends():
            if requires_api_key_cleaner_backend(backend_name):
                cleaner_runtime_availability[backend_name] = True
        mlx_cleaner_runtime_available = cleaner_runtime_availability.get(
            "mlx",
            bool(runtime_probe["cleaner"]["available"]),
        )

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

        default_tts_runtime = TTSRuntimeSpec.from_legacy(
            self.config.tts_engine,
            self.config.tts_quantization,
        )
        primary_runtime = default_tts_runtime.provider

        return {
            "primary_runtime": primary_runtime,
            "mlx_runtime_available": mlx_runtime_available,
            "cleaner_runtime_availability": cleaner_runtime_availability,
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
                device_memory_policy=get_device_memory_policy(default_tts_runtime),
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
        cleaner_cuda_device: Optional[str] = None,
        cleaner_cuda_quantization: Optional[str] = None,
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
                cleaner_cuda_device=cleaner_cuda_device,
                cleaner_cuda_quantization=cleaner_cuda_quantization,
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
                device_memory_policy=pipeline.config.device_memory_policy,
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
        self._emit_desktop_shutdown_ack()

    def _emit_desktop_shutdown_ack(self) -> None:
        if not os.environ.get("AUDIOBOOK_DESKTOP_READY_FILE", "").strip():
            return
        print(
            json.dumps(
                {"event": self.DESKTOP_SHUTDOWN_ACK_EVENT},
                separators=(",", ":"),
            ),
            flush=True,
        )
