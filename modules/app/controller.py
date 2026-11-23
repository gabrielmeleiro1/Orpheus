"""
Application Controller
======================
Central façade that delegates business logic to focused app services.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
from modules.pipeline.pool_manager import SharedWorkerPoolManager
from modules.pipeline.worker_recommendation import build_parallel_worker_recommendation
from modules.storage.maintenance import (
    audit_with_unowned_folders,
    backup_database as backup_database_file,
    list_database_backups,
    prune_stale_records_and_files,
    recover_restore_artifacts,
    restore_database_backup,
    storage_cleanup_summary,
    unowned_output_folders,
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


logger = logging.getLogger(__name__)


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
    title: Optional[str] = None
    author: Optional[str] = None
    pipeline: Optional[Any] = None
    result: Optional[ConversionResult] = None
    error: Optional[str] = None
    cancel_requested: bool = False
    client_request_id: Optional[str] = None
    upload_source_path: Optional[Path] = None
    repair: bool = False
    _thread: Optional[threading.Thread] = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def request_cancel(self) -> tuple[bool, Any]:
        """Mark cancellation and capture its pipeline without calling external code."""
        with self._lock:
            if self.status not in {JobStatus.IDLE, JobStatus.RUNNING}:
                return False, None
            self.cancel_requested = True
            return True, self.pipeline

    def cancel(self) -> bool:
        cancelled, pipeline = self.request_cancel()
        if pipeline is not None:
            pipeline.cancel()
        return cancelled

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
    on_log: Optional[Callable[[str, str], None]] = None
    on_complete: Optional[Callable[[ConversionResult], None]] = None
    on_error: Optional[Callable[[str], None]] = None
    on_event: Optional[Callable[[Any], None]] = None


class AppController:
    """Central application controller with compatibility-preserving API."""

    _CLEANUP_CONVERSION_CANCEL_TIMEOUT_SECONDS = 2.0
    _CLEANUP_WORKER_SHUTDOWN_TIMEOUT_SECONDS = 2.0
    _CLEANUP_POST_WORKER_CANCEL_TIMEOUT_SECONDS = 0.5
    _CLEANUP_VOICE_PREVIEW_TIMEOUT_SECONDS = 0.5
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
        self.config.data_dir = Path(self.config.data_dir).expanduser().resolve()
        self.config.output_dir = Path(self.config.output_dir).expanduser().resolve()
        self.config.temp_dir = Path(self.config.temp_dir).expanduser().resolve()
        self._library_generation_id = uuid.uuid4().hex
        self._library_restore_lock = threading.Lock()
        self._owns_repository = repository is None
        self.repository = repository or SQLiteRepository(
            self.config.db_path,
            output_dir=self.config.output_dir,
            temp_dir=self.config.temp_dir,
        )
        if isinstance(self.repository, SQLiteRepository):
            # Every maintenance service must keep using the path whose process
            # ownership the repository acquired, even if an alias is retargeted.
            self.config.db_path = self.repository.db_path
        recover_restore_artifacts(self.repository)

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
            book_cleanup_paths_resolver=lambda book_id: self._library_service.book_cleanup_paths(book_id),
        )
        if hasattr(self._conversion_service, "upload_cleanup_guard"):
            self._library_service.upload_cleanup_guard = self._conversion_service.upload_cleanup_guard
        if hasattr(self._conversion_service, "upload_reference_checker") and hasattr(
            self._library_service, "_is_source_referenced_by_books"
        ):
            self._conversion_service.upload_reference_checker = self._library_service._is_source_referenced_by_books

    @property
    def runtime_capability_service(self) -> RuntimeCapabilityService:
        """Shared probe cache for capabilities and setup readiness."""
        return self._runtime_capability_service

    # ==================== Library ====================

    def library_snapshot(self, expected_epoch: str | None = None, *, mutation: bool = False):
        return self._library_service.library_snapshot(expected_epoch, mutation=mutation)

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

    def get_library_count(self) -> int:
        return self._library_service.get_library_count()

    def get_book(self, book_id: int) -> Optional[dict]:
        return self._library_service.get_book(book_id)

    def get_book_cover(self, book_id: int):
        return self._library_service.get_book_cover(book_id)

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

    def find_existing_books(self, source_path: Path | str, title: str | None = None) -> list[dict]:
        return self._library_service.find_existing_books(source_path, title)

    def sweep_stale_uploads(self) -> int:
        return self._library_service.sweep_stale_uploads()

    def delete_upload(self, handle: str) -> dict:
        return self._library_service.delete_upload(handle)

    def get_book_playback_file(self, book_id: int, chapter_number: int | None = None) -> Optional[Path]:
        return self._library_service.get_book_playback_file(book_id, chapter_number=chapter_number)

    def update_book_playback_position(self, book_id: int, position_sec: float, expected_epoch: str | None = None) -> Optional[float]:
        return self._library_service.update_book_playback_position(book_id, position_sec, expected_epoch=expected_epoch)

    def update_book_metadata(
        self,
        book_id: int,
        *,
        title: Optional[str] = None,
        author: Optional[str] = None,
        tags: Optional[str] = None,
        favorite: Optional[bool] = None,
        expected_epoch: str | None = None,
    ) -> Optional[dict]:
        return self._library_service.update_book_metadata(
            book_id,
            title=title,
            author=author,
            tags=tags,
            favorite=favorite,
            expected_epoch=expected_epoch,
        )

    def get_chapter_cleaning_preview(self, book_id: int, chapter_number: int) -> Optional[dict]:
        return self._library_service.get_chapter_cleaning_preview(book_id, chapter_number)

    @contextmanager
    def _book_cleanup(self, book_id: int):
        self._conversion_service.begin_book_cleanup(book_id)
        try:
            yield
        finally:
            self._conversion_service.end_book_cleanup(book_id)

    def delete_book(self, book_id: int, expected_epoch: str | None = None) -> bool | dict:
        with self._book_cleanup(book_id):
            return self._library_service.delete_book(book_id, **({"expected_epoch": expected_epoch} if expected_epoch is not None else {}))

    def delete_book_with_identity(self, book_id: int, expected_epoch: str | None = None) -> bool | dict:
        # Reserve before taking the snapshot; upload cleanup takes the locks in
        # this order too. The reservation excludes restore, the only identity
        # change, so the identity read here still describes the deletion while
        # the staged files are removed outside the repository lock.
        with self._book_cleanup(book_id):
            with self.library_snapshot(expected_epoch) as snapshot:
                identity = dict(snapshot)
            deleted = self._library_service.delete_book(book_id, **({"expected_epoch": expected_epoch} if expected_epoch is not None else {}))
            if not deleted:
                return False
            return {"deleted": True, **identity, **(deleted if isinstance(deleted, dict) else {})}

    def open_book_folder(self, book_id: int) -> Optional[Path]:
        return self._library_service.open_book_folder(book_id)

    def get_storage_diagnostics(self) -> dict:
        # Restore is the only operation that replaces library identity, and it
        # takes the restore lock. Holding that lock keeps the identity and the
        # audit consistent; the repository lock is released before the walk,
        # which reads through its own connection and touches every stored path.
        with self._library_restore_lock:
            with self.library_snapshot() as snapshot:
                identity = dict(snapshot)
            db_path = Path(self.config.db_path)
            # One audit pass yields both the graced audit and the unowned folders.
            audit, unowned = audit_with_unowned_folders(
                db_path=db_path,
                output_dir=self.config.output_dir,
                temp_dir=self.config.temp_dir,
            )
            return {
                **identity,
                "database_path": str(db_path.resolve()),
                "output_path": str(self.config.output_dir.resolve()),
                "temp_path": str(self.config.temp_dir.resolve()),
                "app_data_path": str(workspace_root_for_db(db_path)),
                "audit": audit,
                # The same classification cleanup reports, so counts agree.
                "cleanup_preview": storage_cleanup_summary(
                    db_path, self.config.output_dir, self.config.temp_dir, audit, unowned=unowned,
                ),
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "quarantine": getattr(self.repository, "quarantine", None),
                "library_generation": f"{self._library_generation_id}:{getattr(self.repository, 'library_generation', 0)}",
            }

    def list_database_backups(self) -> list[dict]:
        return list_database_backups(self.config.db_path)

    def restore_database_backup(self, backup_id: str) -> Path:
        return self.restore_database_backup_report(backup_id)["safety_copy"]

    def restore_database_backup_report(self, backup_id: str) -> dict[str, Any]:
        """Restore a backup, then report how the restored rows match the disk.

        A backup holds rows, not files: books deleted since it was taken come
        back without their audio, and books converted since then leave folders
        that no row owns. Restore never deletes anything; it reports both.
        """
        self._conversion_service.begin_storage_cleanup()
        try:
            with self._library_restore_lock:
                safety = restore_database_backup(self.repository, backup_id)
                self._conversion_service.invalidate_recovered_uploads()
                self._library_service.sweep_delete_staging()
                files = self._library_service.books_with_missing_files()
                folders = unowned_output_folders(self.config.db_path, self.config.output_dir, self.config.temp_dir)
                return {
                    # resolve() restores the on-disk case of the canonical path.
                    "safety_copy": Path(safety).resolve(),
                    "summary": {
                        "books_restored": files["books"],
                        "missing_audio": files["missing_audio"],
                        "missing_source": files["missing_source"],
                        "unowned_output_folders": folders,
                    },
                }
        finally:
            self._conversion_service.end_storage_cleanup()

    def backup_database(self) -> Path:
        # A backup opens its own SQLite connections outside the repository lock,
        # so an unguarded backup taken during an active chapter/book commit
        # races on the write lock. Reuse the storage-cleanup guard (as
        # cleanup_storage does) so a queued/running conversion yields a clear
        # error instead of a 'database is locked' race.
        self._conversion_service.begin_storage_cleanup()
        try:
            return backup_database_file(self.config.db_path)
        finally:
            self._conversion_service.end_storage_cleanup()

    def cleanup_storage(self, *, confirm: str, remove_unowned_folders: tuple[str, ...] = ()) -> dict:
        """Prune stale storage; remove unowned output folders only when named.

        A folder no book owns may hold a finished audiobook, so cleanup never
        removes one on its own. Each folder named in ``remove_unowned_folders``
        goes through the same staged deletion transaction as a book.
        """
        if str(confirm).strip().lower() != "cleanup":
            raise ValueError("Cleanup requires confirm='cleanup'")
        # begin_storage_cleanup atomically rejects an active job and blocks new
        # conversion starts until cleanup finishes (no check-then-prune race).
        self._conversion_service.begin_storage_cleanup()
        try:
            report = prune_stale_records_and_files(
                db_path=self.config.db_path,
                output_dir=self.config.output_dir,
                temp_dir=self.config.temp_dir,
                aggressive=True,
            )
            if remove_unowned_folders:
                unowned = {
                    folder["name"]: Path(folder["path"])
                    for folder in unowned_output_folders(self.config.db_path, self.config.output_dir, self.config.temp_dir)
                }
                removed, missing = [], []
                for name in dict.fromkeys(remove_unowned_folders):
                    if name not in unowned:
                        missing.append(name)
                        continue
                    self._library_service.remove_unowned_output_folder(unowned[name])
                    removed.append(name)
                report["removed_unowned_folders"] = removed
                report["unknown_unowned_folders"] = missing
                summary = report.get("summary")
                if isinstance(summary, dict):
                    if removed:
                        # What is kept, and why, depends on the folders that
                        # are still there: classify again without the removed ones.
                        from modules.storage.maintenance import audit_with_unowned_folders

                        audit, still_unowned = audit_with_unowned_folders(
                            self.config.db_path, self.config.output_dir, self.config.temp_dir,
                        )
                        report["audit_after"] = audit
                        summary = report["summary"] = {
                            "removed": summary.get("removed", {}),
                            **storage_cleanup_summary(
                                self.config.db_path, self.config.output_dir, self.config.temp_dir, audit,
                                actions=report.get("actions"), changed=True, unowned=still_unowned,
                            ),
                        }
                    summary.setdefault("removed", {})["unowned_folders"] = len(removed)
            return report
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
        *,
        client_request_id: str | None = None,
    ) -> ConversionJob:
        return self._conversion_service.start_conversion(
            source_path=source_path,
            callbacks=callbacks,
            conversion_mode=conversion_mode,
            title=title,
            author=author,
            runtime_options=runtime_options,
            client_request_id=client_request_id,
        )

    def get_active_job(self) -> Optional[ConversionJob]:
        return self._conversion_service.get_active_job()

    def cancel_conversion(self, expected_job_id: str | None = None) -> bool:
        return self._conversion_service.cancel_conversion(expected_job_id=expected_job_id)

    def repair_conversion(
        self,
        book_id: int,
        callbacks: Optional[ConversionCallbacks] = None,
        runtime_options: Optional[ConversionRuntimeOptions] = None,
        runtime_fields_set: Optional[set[str] | frozenset[str]] = None,
        expected_epoch: str | None = None,
    ) -> ConversionJob:
        with self._conversion_service.repair_reservation(book_id) as job:
            # The reservation blocks restore until epoch validation and start
            # finish, without acquiring the job lock inside the snapshot.
            with self.library_snapshot(expected_epoch, mutation=True):
                generation = getattr(self.repository, "library_generation", 0)
            return self._conversion_service.start_repair(
                book_id=book_id,
                callbacks=callbacks,
                runtime_options=runtime_options,
                runtime_fields_set=runtime_fields_set,
                reserved_job=job,
                expected_generation=generation,
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
        cleaner_backend: Optional[str] = None,
        cleaner_api_model: Optional[str] = None,
        cleaner_api_key: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._voice_service.suggest_voice_for_book(
            source_path=source_path, engine_name=engine_name,
            cleaner_backend=cleaner_backend,
            cleaner_api_model=cleaner_api_model,
            cleaner_api_key=cleaner_api_key,
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
        default_tts_runtime = TTSRuntimeSpec.from_legacy(
            self.config.tts_engine,
            self.config.tts_quantization,
        )
        primary_runtime = default_tts_runtime.provider
        runtime_probe = self._runtime_capability_service.get_system_runtime_snapshot(
            primary_runtime=primary_runtime,
        )
        # The probe reports the selected runtime: MLX TTS only on an MLX runtime.
        mlx_runtime_available = primary_runtime == "mlx" and bool(runtime_probe["tts"]["available"])
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

    # ==================== Cleanup ====================

    def _cancel_active_conversion_for_cleanup(self, timeout_seconds: float) -> bool:
        cancel_active_job = getattr(self._conversion_service, "cancel_active_job", None)
        if callable(cancel_active_job):
            return bool(cancel_active_job(timeout_seconds=timeout_seconds))
        return not bool(self.cancel_conversion())

    def _shutdown_shared_worker_pool_for_cleanup(self) -> bool:
        if self._shared_worker_pool_manager is None:
            return True

        return bool(
            self._shared_worker_pool_manager.shutdown(
                timeout_seconds=self._CLEANUP_WORKER_SHUTDOWN_TIMEOUT_SECONDS,
            )
        )

    def release_resident_models(self) -> bool:
        """Unload the warm conversion engine and stop idle pool workers.

        The caller must hold the storage-cleanup reservation, so no conversion
        can attach the engine or start the pool meanwhile. Returns False when
        the workers did not exit within the bounded wait; their model files
        may then still be open.
        """
        self._conversion_service.cleanup()
        return self._shutdown_shared_worker_pool_for_cleanup()

    def cleanup(self) -> bool:
        begin_shutdown = getattr(self._conversion_service, "begin_shutdown", None)
        if callable(begin_shutdown):
            begin_shutdown()
        self._cancel_active_conversion_for_cleanup(
            self._CLEANUP_CONVERSION_CANCEL_TIMEOUT_SECONDS,
        )
        workers_quiesced = self._shutdown_shared_worker_pool_for_cleanup()
        conversion_quiesced = self._cancel_active_conversion_for_cleanup(
            self._CLEANUP_POST_WORKER_CANCEL_TIMEOUT_SECONDS,
        )
        if not conversion_quiesced or not workers_quiesced:
            logger.warning(
                "Shutdown deferred: conversion_quiesced=%s workers_quiesced=%s; resources remain open",
                conversion_quiesced, workers_quiesced,
            )
            return False
        try:
            cleanup_conversion_service = getattr(self._conversion_service, "cleanup", None)
            if callable(cleanup_conversion_service):
                cleanup_conversion_service()
            # Bounded: a running voice preview must not hold shutdown past the
            # supervisor's budget, and SQLite must still close below.
            if not self._voice_service.shutdown(
                timeout_seconds=self._CLEANUP_VOICE_PREVIEW_TIMEOUT_SECONDS,
            ):
                logger.warning("Voice preview engine still in use at shutdown; leaving it to process exit")
            self._cleaner_api_limiter.shutdown()
        except Exception:
            logger.exception("Model cleanup failed during shutdown")
        finally:
            try:
                if self._owns_repository:
                    close = getattr(self.repository, "close", None)
                    if callable(close):
                        close()
            finally:
                self._emit_desktop_shutdown_ack()
        return True

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
