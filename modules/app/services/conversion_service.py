"""Conversion orchestration service."""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from itertools import count
from pathlib import Path
from typing import Any, Callable, Optional

from modules.app.config import AppConfig
from modules.app.events import JobState, make_log_event, make_progress_event, make_state_event
from modules.app.services.conversion_paths import (
    playback_metadata_for_pipeline_result,
    repair_output_dirs,
)
from modules.app.services.conversion_result_adapter import pipeline_to_controller_result
from modules.app.services.conversion_commands import (
    ConversionRunCommand,
    PipelineJobCommand,
    RepairRunCommand,
    resolve_repair_context,
)
from modules.app.services.conversion_runtime import (
    ConversionRuntimeOptions,
    NormalizedConversionRuntimeOptions,
    RuntimeOptionsResolver,
)
from modules.app.services.repair_runtime import (
    effective_runtime_options_for_persistence,
    resolve_repair_runtime_options,
    serialize_persisted_runtime_options,
)
from modules.app.services.progress_math import build_progress_snapshot
from modules.app.services.tts_session import TtsEngineSession
from modules.pipeline.models import ConversionResult as PipelineConversionResult
from modules.pipeline.orchestrator import ConversionPipeline, PipelineConfig
from modules.pipeline.progress import estimate_eta_from_progress
from modules.storage.models import (
    BookCreate,
    ChapterCreate,
    ConversionResult,
    ConversionMode,
    SourceType,
)
from modules.storage.repository import IBookRepository
from modules.storage.permissions import ensure_owner_only_directory, owner_only
from modules.tts.api_rate_limit import GlobalAPILimiter

logger = logging.getLogger(__name__)


_NormalizedConversionRuntimeOptions = NormalizedConversionRuntimeOptions


class ConversionService:
    """Owns conversion job lifecycle, threading, and pipeline execution."""

    _DEFAULT_CANCEL_WAIT_SECONDS = 2.0
    _PROGRESS_EMIT_INTERVAL_SECONDS = 0.25
    _PROGRESS_EMIT_EPSILON_SECONDS = 1e-9

    def __init__(
        self,
        *,
        config: AppConfig,
        repository: IBookRepository,
        job_cls: type,
        status_enum: Any,
        cleaner_api_limiter: GlobalAPILimiter,
        runtime_validator: Callable[[str, Optional[str]], None],
        shared_worker_pool_manager: Optional[Any] = None,
        pipeline_cls: type = ConversionPipeline,
    ) -> None:
        self._config = config
        self._repository = repository
        self._job_cls = job_cls
        self._status = status_enum
        self._cleaner_api_limiter = cleaner_api_limiter
        self._runtime_validator = runtime_validator
        self._shared_worker_pool_manager = shared_worker_pool_manager
        self._pipeline_cls = pipeline_cls
        self._runtime_options_resolver = RuntimeOptionsResolver(
            config=config,
            cleaner_api_limiter=cleaner_api_limiter,
            runtime_validator=runtime_validator,
            shared_worker_pool_manager=shared_worker_pool_manager,
        )
        self._active_job = None
        self._job_lock = threading.Lock()
        self._job_sequence = count()
        self._storage_cleanup_active = False
        self._storage_cleanup_owner: int | None = None
        # book id -> owning thread ident; only the owner may end the reservation.
        self._book_cleanup_ids: dict[int, int] = {}
        self._tts_session = TtsEngineSession()

    def _job_is_active_or_pending(self, job: Any) -> bool:
        status = getattr(job, "status", None)
        if status in {self._status.IDLE, self._status.RUNNING}:
            return True
        is_active = getattr(job, "is_active", None)
        if callable(is_active):
            try:
                return bool(is_active())
            except Exception:
                logger.debug("Failed to inspect active conversion job state", exc_info=True)
        return False

    def _request_job_cancel(self, job: Any) -> bool:
        cancel = getattr(job, "cancel", None)
        if callable(cancel):
            try:
                if bool(cancel()):
                    return True
            except Exception:
                logger.exception("Failed to request conversion job cancellation")
                return False

        if not self._job_is_active_or_pending(job):
            return False

        setattr(job, "cancel_requested", True)
        pipeline = getattr(job, "pipeline", None)
        pipeline_cancel = getattr(pipeline, "cancel", None)
        if callable(pipeline_cancel):
            pipeline_cancel()
        return True

    def cleanup(self) -> None:
        self._tts_session.cleanup()

    def _ensure_job_slot_available_locked(self) -> None:
        """Refuse new jobs while one is active or a storage cleanup runs.

        Caller must hold ``self._job_lock``.
        """
        if self._storage_cleanup_active:
            raise RuntimeError("A storage cleanup is in progress")
        if self._active_job is not None and self._job_is_active_or_pending(self._active_job):
            raise RuntimeError("A conversion is already in progress")

    def _preflight_job_slot_available(self, *, book_id: int | None = None) -> None:
        """Fail fast before potentially expensive runtime validation.

        The caller must still reserve the slot under ``_job_lock`` after
        validation because another request can start in between.
        """
        with self._job_lock:
            if book_id is not None and int(book_id) in self._book_cleanup_ids:
                raise RuntimeError("Book cleanup is in progress")
            self._ensure_job_slot_available_locked()

    def begin_storage_cleanup(self) -> None:
        """Atomically verify no job is active and block new starts until
        :meth:`end_storage_cleanup` — closes the check-then-prune race."""
        with self._job_lock:
            if self._storage_cleanup_active:
                raise RuntimeError("Storage cleanup is already in progress")
            if self._active_job is not None and self._job_is_active_or_pending(self._active_job):
                raise RuntimeError(
                    "Cannot clean up storage while a conversion is queued or running"
                )
            if self._book_cleanup_ids:
                raise RuntimeError(
                    "Cannot start storage cleanup while a book cleanup is in progress"
                )
            self._storage_cleanup_active = True
            self._storage_cleanup_owner = threading.get_ident()

    def end_storage_cleanup(self) -> None:
        with self._job_lock:
            if not self._storage_cleanup_active:
                return
            if self._storage_cleanup_owner != threading.get_ident():
                raise RuntimeError(
                    "Cannot end storage cleanup reserved by another owner"
                )
            self._storage_cleanup_active = False
            self._storage_cleanup_owner = None

    def begin_book_cleanup(self, book_id: int) -> None:
        """Reserve a book for deletion while preventing a same-book job start."""
        normalized_book_id = int(book_id)
        with self._job_lock:
            if self._storage_cleanup_active:
                raise RuntimeError(
                    "Cannot start book cleanup while storage cleanup is in progress"
                )
            if normalized_book_id in self._book_cleanup_ids:
                raise RuntimeError(
                    f"Book cleanup for {normalized_book_id} is already in progress"
                )
            active_job = self._active_job
            if (
                active_job is not None
                and getattr(active_job, "book_id", None) == normalized_book_id
                and self._job_is_active_or_pending(active_job)
            ):
                raise RuntimeError(
                    "Cannot delete a book while it is being converted or repaired"
                )
            self._book_cleanup_ids[normalized_book_id] = threading.get_ident()

    def end_book_cleanup(self, book_id: int) -> None:
        with self._job_lock:
            normalized_book_id = int(book_id)
            if normalized_book_id not in self._book_cleanup_ids:
                return
            if self._book_cleanup_ids[normalized_book_id] != threading.get_ident():
                raise RuntimeError(
                    f"Cannot end book cleanup for {normalized_book_id} reserved by another owner"
                )
            self._book_cleanup_ids.pop(normalized_book_id)

    def start_conversion(
        self,
        source_path: Path,
        callbacks: Optional[Any] = None,
        conversion_mode: ConversionMode | str = ConversionMode.AUDIOBOOK,
        title: Optional[str] = None,
        author: Optional[str] = None,
        runtime_options: Optional[ConversionRuntimeOptions] = None,
    ) -> Any:
        if not source_path.exists():
            raise ValueError(f"Source file not found: {source_path}")

        self._preflight_job_slot_available()
        runtime_options = runtime_options or ConversionRuntimeOptions()
        self._validate_requested_tts_runtime(runtime_options=runtime_options)

        with self._job_lock:
            self._ensure_job_slot_available_locked()
            job_id = f"conv_{time.time_ns()}_{next(self._job_sequence)}"
            job = self._job_cls(
                id=job_id,
                book_id=None,
                status=self._status.IDLE,
                title=title,
                author=author,
            )
            self._active_job = job

        # Copying a large upload into durable storage must not run under the
        # job lock: status reads (/active), cancel, and cleanup guards would
        # block for the whole copy. The reserved IDLE job above keeps
        # concurrent starts out while the copy runs.
        try:
            source_path = self._durable_source_path_for_conversion(source_path)
        except Exception:
            with self._job_lock:
                if self._active_job is job:
                    self._active_job = None
            raise

        return self._start_pipeline_job(
            job=job,
            target=self._run_conversion,
            kwargs={
                "job": job,
                "source_path": source_path,
                "callbacks": callbacks,
                "conversion_mode": conversion_mode,
                "title": title,
                "author": author,
                "runtime_options": runtime_options,
            },
            callbacks=callbacks,
            started_message="conversion started",
        )

    def _durable_source_path_for_conversion(self, source_path: Path) -> Path:
        upload_root = (self._config.temp_dir / "web_uploads").resolve()
        try:
            resolved_source_path = source_path.resolve()
            resolved_source_path.relative_to(upload_root)
        except ValueError:
            return source_path

        sources_dir = Path(self._config.db_path).parent / "sources"
        ensure_owner_only_directory(sources_dir)
        try:
            for existing in sources_dir.iterdir():
                if existing.is_file():
                    owner_only(existing)
        except OSError:
            pass
        durable_path = sources_dir / source_path.name
        index = 1
        while True:
            try:
                with durable_path.open("xb") as destination_file:
                    with source_path.open("rb") as source_file:
                        shutil.copyfileobj(source_file, destination_file)
                try:
                    shutil.copystat(source_path, durable_path)
                except Exception:
                    self._cleanup_partial_durable_source(durable_path)
                    raise
                owner_only(durable_path)
                return durable_path
            except FileExistsError:
                durable_path = sources_dir / f"{source_path.stem}_{index}{source_path.suffix}"
                index += 1
            except Exception:
                self._cleanup_partial_durable_source(durable_path)
                raise

    @staticmethod
    def _cleanup_partial_durable_source(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove partial durable source copy: %s", path, exc_info=True)

    def start_repair(
        self,
        book_id: int,
        callbacks: Optional[Any] = None,
        runtime_options: Optional[ConversionRuntimeOptions] = None,
        runtime_fields_set: Optional[set[str] | frozenset[str]] = None,
    ) -> Any:
        request_options = runtime_options or ConversionRuntimeOptions()

        self._preflight_job_slot_available(book_id=book_id)

        # None means "no explicit overrides" (controller/direct callers), same as
        # an empty HTTP payload's fields_set — reuse persisted v7 / app defaults.
        fields_set = (
            runtime_fields_set if runtime_fields_set is not None else frozenset()
        )
        detail = self._repository.get_book_with_chapters(book_id)
        if detail is None:
            raise ValueError("Book not found")
        book, _chapters = detail
        resolved_options = resolve_repair_runtime_options(
            book=book,
            request_options=request_options,
            request_fields_set=fields_set,
            app_config=self._config,
        )

        self._validate_requested_tts_runtime(runtime_options=resolved_options)

        with self._job_lock:
            if int(book_id) in self._book_cleanup_ids:
                raise RuntimeError("Book cleanup is in progress")
            self._ensure_job_slot_available_locked()
            job_id = f"repair_{book_id}_{time.time_ns()}_{next(self._job_sequence)}"
            job = self._job_cls(
                id=job_id,
                book_id=book_id,
                status=self._status.IDLE,
                title=getattr(book, "title", None),
                author=getattr(book, "author", None),
            )
            self._active_job = job

        return self._start_pipeline_job(
            job=job,
            target=self._run_repair,
            kwargs={
                "job": job,
                "book_id": book_id,
                "callbacks": callbacks,
                "runtime_options": resolved_options,
            },
            callbacks=callbacks,
            started_message="repair started",
        )

    def _validate_requested_tts_runtime(
        self,
        *,
        runtime_options: ConversionRuntimeOptions,
    ) -> None:
        self._runtime_options_resolver.validate_requested_tts_runtime(
            runtime_options=runtime_options,
        )

    def _start_pipeline_job(
        self,
        *,
        job: Any,
        target: Callable[..., None],
        callbacks: Optional[Any],
        started_message: str,
        args: tuple[Any, ...] = (),
        kwargs: Optional[dict[str, Any]] = None,
    ) -> Any:
        thread = threading.Thread(
            target=target,
            args=args,
            kwargs=kwargs or {},
            daemon=True,
        )
        begin_running = getattr(job, "begin_running", None)
        if callable(begin_running):
            begin_running(thread)
        else:
            job._thread = thread
            job.status = self._status.RUNNING
        # Publish RUNNING before the worker thread can emit a terminal state;
        # otherwise an instantly-failing job would surface FAILED then RUNNING.
        if callbacks and callbacks.on_event:
            callbacks.on_event(make_state_event(JobState.RUNNING, job.id, started_message))
        thread.start()
        return job

    def _normalize_runtime_options(
        self,
        runtime_options: ConversionRuntimeOptions,
    ) -> _NormalizedConversionRuntimeOptions:
        return self._runtime_options_resolver.normalize(runtime_options)

    def get_active_job(self) -> Any:
        with self._job_lock:
            return self._active_job

    def has_active_job(self) -> bool:
        with self._job_lock:
            return self._active_job is not None and self._job_is_active_or_pending(
                self._active_job
            )

    def cancel_conversion(self) -> bool:
        # Take the job out under the lock, then release it: the pool shutdown
        # below can block for the worker-startup probe window, and holding
        # _job_lock across it would stall every job-status read too.
        with self._job_lock:
            job = (
                self._active_job
                if self._active_job and self._job_is_active_or_pending(self._active_job)
                else None
            )

        if job is None:
            return False

        cancelled = self._request_job_cancel(job)
        if cancelled:
            self._shutdown_shared_worker_pool_for_cancel()
        return cancelled

    def _shutdown_shared_worker_pool_for_cancel(self) -> None:
        if self._shared_worker_pool_manager is None:
            return

        shutdown = getattr(self._shared_worker_pool_manager, "shutdown", None)
        if not callable(shutdown):
            return

        try:
            shutdown(timeout_seconds=0.0, cancel_futures=True)
        except TypeError:
            try:
                shutdown(timeout_seconds=0.0)
            except TypeError:
                shutdown()
        except Exception:
            logger.exception("Failed to shut down shared worker pool during cancellation")

    def cancel_active_job(self, timeout_seconds: Optional[float] = None) -> bool:
        """
        Request cancellation for the active job and wait only up to the deadline.

        Returns True when there is no active job or the active job finishes before
        the timeout. Returns False when cancellation was requested but the job is
        still running after the bounded wait.
        """
        with self._job_lock:
            job = (
                self._active_job
                if self._active_job and self._job_is_active_or_pending(self._active_job)
                else None
            )

        if job is None:
            return True

        self._request_job_cancel(job)
        self._shutdown_shared_worker_pool_for_cancel()
        timeout = (
            self._DEFAULT_CANCEL_WAIT_SECONDS
            if timeout_seconds is None
            else max(0.0, float(timeout_seconds))
        )
        wait = getattr(job, "wait", None)
        if callable(wait):
            try:
                return bool(wait(timeout=timeout))
            except Exception:
                logger.exception("Failed while waiting for conversion job cancellation")
                return False

        thread = getattr(job, "_thread", None)
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def _create_pipeline_config(
        self,
        *,
        conversion_mode: ConversionMode | str = ConversionMode.AUDIOBOOK,
        runtime_options: Optional[ConversionRuntimeOptions] = None,
    ) -> PipelineConfig:
        runtime_options = runtime_options or ConversionRuntimeOptions()
        return self._runtime_options_resolver.create_pipeline_config(
            conversion_mode=conversion_mode,
            runtime_options=runtime_options,
        )

    def _create_pipeline_callbacks(
        self,
        callbacks: Optional[Any],
        *,
        job_id: str | None = None,
    ):
        last_stage_value: Optional[str] = None
        last_progress_emit_at: Optional[float] = None
        progress_started_at: Optional[float] = None

        def on_progress(
            stage,
            chapter_idx,
            total_chapters,
            chunk_idx,
            total_chunks,
            message,
            _legacy_eta,
            performance=None,
        ):
            nonlocal last_stage_value, last_progress_emit_at, progress_started_at
            stage_value = str(getattr(stage, "value", stage) or "")
            message_value = message or ""
            progress_snapshot = build_progress_snapshot(
                stage_value=stage_value,
                chapter_idx=chapter_idx,
                total_chapters=total_chapters,
                chunk_idx=chunk_idx,
                total_chunks=total_chunks,
            )
            now = time.monotonic()
            if progress_started_at is None:
                progress_started_at = now
            eta = estimate_eta_from_progress(
                progress_started_at,
                progress_snapshot.overall_progress,
                now=now,
            )
            stage_changed = stage_value != last_stage_value
            last_stage_value = stage_value

            if stage_changed and callbacks and callbacks.on_stage_change:
                callbacks.on_stage_change(stage_value)

            has_progress_subscribers = bool(
                callbacks and (callbacks.on_progress or callbacks.on_event)
            )
            if not has_progress_subscribers:
                return

            should_emit_progress = (
                last_progress_emit_at is None
                or stage_changed
                or progress_snapshot.overall_progress >= 1.0
                or bool(isinstance(performance, dict) and performance.get("fallback_reason"))
                or (now - last_progress_emit_at) + self._PROGRESS_EMIT_EPSILON_SECONDS
                >= self._PROGRESS_EMIT_INTERVAL_SECONDS
            )
            if not should_emit_progress:
                return

            last_progress_emit_at = now
            if callbacks.on_progress:
                callbacks.on_progress(
                    stage_value, progress_snapshot.overall_progress, message_value
                )
            if callbacks.on_event:
                callbacks.on_event(
                    make_progress_event(
                        stage_value,
                        progress_snapshot.overall_progress,
                        message_value,
                        stage_progress=progress_snapshot.stage_progress,
                        eta=eta,
                        chapter_current=progress_snapshot.chapter_current,
                        chapter_total=progress_snapshot.chapter_total,
                        chunk_current=progress_snapshot.chunk_current,
                        chunk_total=progress_snapshot.chunk_total,
                        performance=performance,
                        job_id=job_id,
                    )
                )

        def on_verbose(message: str, msg_type: str):
            if callbacks and callbacks.on_log:
                callbacks.on_log(message, msg_type)
            if callbacks and callbacks.on_event:
                callbacks.on_event(
                    make_log_event(message=message, level=msg_type, job_id=job_id)
                )

        return on_progress, on_verbose

    def _persist_conversion_library_records(
        self,
        source_path: Path,
        pipeline_result: PipelineConversionResult,
        conversion_mode: ConversionMode | str = ConversionMode.AUDIOBOOK,
        runtime_options: Optional[ConversionRuntimeOptions] = None,
    ) -> int:
        try:
            source_type = SourceType(source_path.suffix.lower().lstrip("."))
        except ValueError:
            source_type = SourceType.EPUB

        if not isinstance(conversion_mode, ConversionMode):
            conversion_mode = ConversionMode(str(conversion_mode))

        playback_path, playback_status = playback_metadata_for_pipeline_result(pipeline_result)
        persisted_runtime = None
        if runtime_options is not None:
            try:
                normalized = self._normalize_runtime_options(runtime_options)
                effective_options = effective_runtime_options_for_persistence(
                    request_options=runtime_options,
                    normalized=normalized,
                )
                persisted_runtime = json.dumps(
                    serialize_persisted_runtime_options(effective_options)
                )
            except Exception:
                # Persisting runtime options is best-effort: a completed
                # conversion (audio already on disk) must still get its library
                # record even if these repair-fidelity settings fail to
                # normalize or serialize. Repair falls back to legacy defaults
                # when they are absent.
                logger.warning(
                    "Failed to serialize conversion runtime options; "
                    "persisting book without them",
                    exc_info=True,
                )
                persisted_runtime = None

        book, _ = self._repository.create_book_with_chapters(
            BookCreate(
                title=pipeline_result.title,
                author=pipeline_result.author,
                source_path=str(source_path),
                source_type=source_type,
                conversion_mode=conversion_mode,
                total_chapters=len(pipeline_result.chapters),
                playback_path=playback_path,
                playback_status=playback_status,
                conversion_runtime_options=persisted_runtime,
            ),
            [
                ChapterCreate(
                    book_id=0,
                    chapter_number=chapter.chapter_number,
                    title=chapter.chapter_title,
                    duration_ms=chapter.duration_ms,
                    mp3_path=str(chapter.mp3_path) if chapter.mp3_path else None,
                )
                for chapter in pipeline_result.chapters
            ],
        )
        return book.id

    def _checkpoint_conversion(self, command: ConversionRunCommand, source_path: Path, result: PipelineConversionResult) -> None:
        # Registration and reservation share the job lock: deletion must never
        # see the new book before it has become owned by the active job.
        with self._job_lock:
            book_id = getattr(command.job, "book_id", None)
            if book_id is None:
                book_id = self._persist_conversion_library_records(
                    source_path, result, conversion_mode=command.conversion_mode,
                    runtime_options=command.runtime_options,
                )
                command.job.book_id = book_id
                return
        self._update_conversion_library_records(book_id, result)

    def _update_conversion_library_records(self, book_id: int, result: PipelineConversionResult) -> None:
        detail = self._repository.get_book_with_chapters(book_id)
        if detail is None:
            raise LookupError("Conversion recovery book disappeared")
        _book, stored_chapters = detail
        by_number = {chapter.chapter_number: chapter for chapter in result.chapters}
        playback_path, playback_status = playback_metadata_for_pipeline_result(result)
        self._repository.apply_repair_result(
            book_id,
            repaired_chapter_updates=[
                {
                    "chapter_id": stored.id,
                    "duration_ms": by_number[stored.chapter_number].duration_ms,
                    "mp3_path": str(by_number[stored.chapter_number].mp3_path)
                    if by_number[stored.chapter_number].mp3_path else None,
                }
                for stored in stored_chapters if stored.chapter_number in by_number
            ],
            playback_path=playback_path,
            playback_status=playback_status,
            total_chapters=len(stored_chapters),
        )

    def _handle_conversion_success(
        self,
        *,
        job: Any,
        source_path: Path,
        pipeline_result: PipelineConversionResult,
        callbacks: Optional[Any],
        conversion_mode: ConversionMode | str = ConversionMode.AUDIOBOOK,
        runtime_options: Optional[ConversionRuntimeOptions] = None,
    ) -> None:
        book_id = getattr(job, "book_id", None)
        if book_id is None:
            book_id = self._persist_conversion_library_records(
                source_path,
                pipeline_result,
                conversion_mode=conversion_mode,
                runtime_options=runtime_options,
            )
        else:
            self._update_conversion_library_records(book_id, pipeline_result)
        complete = getattr(job, "complete", None)
        if callable(complete):
            complete(book_id)
        else:
            job.status = self._status.COMPLETED
            job.book_id = book_id
        if callbacks and callbacks.on_complete and job.result is not None:
            callbacks.on_complete(job.result)
        if callbacks and callbacks.on_event:
            callbacks.on_event(make_state_event(JobState.COMPLETED, job.id, "conversion completed"))

    def _handle_conversion_failure(
        self,
        *,
        job: Any,
        error_message: str,
        callbacks: Optional[Any],
    ) -> None:
        failure_result = ConversionResult(
            success=False,
            output_path=None,
            total_duration_ms=0,
            error_message=error_message,
            chapters_completed=0,
            chapters_total=0,
        )
        fail = getattr(job, "fail", None)
        if callable(fail):
            fail(error_message, failure_result)
        else:
            job.status = self._status.FAILED
            job.error = error_message
            existing_result = getattr(job, "result", None)
            if existing_result is None or bool(getattr(existing_result, "success", False)):
                job.result = failure_result
        if callbacks and callbacks.on_error:
            callbacks.on_error(error_message)
        if callbacks and callbacks.on_event:
            callbacks.on_event(make_state_event(JobState.FAILED, job.id, error_message))

    def _handle_conversion_cancelled(
        self,
        *,
        job: Any,
        callbacks: Optional[Any],
        message: str = "conversion cancelled",
    ) -> None:
        mark_cancelled = getattr(job, "mark_cancelled", None)
        if callable(mark_cancelled):
            mark_cancelled(message)
        else:
            job.status = self._status.CANCELLED
            job.error = message
        if callbacks and callbacks.on_event:
            callbacks.on_event(make_state_event(JobState.CANCELLED, job.id, message))

    def _emit_conversion_summary(
        self,
        callbacks: Optional[Any],
        *,
        job_id: str | None = None,
    ) -> None:
        pool_snapshot = {}
        if self._shared_worker_pool_manager is not None:
            try:
                pool_snapshot = self._shared_worker_pool_manager.snapshot()
            except Exception:
                pool_snapshot = {}
        limiter_snapshot = self._cleaner_api_limiter.snapshot()

        message = (
            "[PERF] "
            f"worker_pool_reuse_hits={pool_snapshot.get('reuse_hits', 0)} "
            f"worker_pool_restarts={pool_snapshot.get('restarts', 0)} "
            f"global_api_limiter_wait_count={limiter_snapshot.get('wait_count', 0)} "
            f"global_api_limiter_wait_ms={limiter_snapshot.get('wait_ms_total', 0)}"
        )
        logger.info(message)
        if callbacks and callbacks.on_log:
            callbacks.on_log(message, "info")
        if callbacks and callbacks.on_event:
            # No performance payload here: the pool snapshot describes the idle
            # worker pool (parallel mode, zero counters), and clients treat any
            # attached snapshot as the job's final performance numbers.
            callbacks.on_event(
                make_log_event(
                    message=message,
                    level="info",
                    job_id=job_id,
                )
            )

    def _repair_output_dirs(self, book: Any, chapters: list[Any]) -> tuple[Path, Path]:
        return repair_output_dirs(
            book,
            chapters,
            db_path=Path(self._config.db_path),
            output_dir=self._config.output_dir,
            temp_dir=self._config.temp_dir,
        )

    def _execute_pipeline_job(
        self,
        *,
        command: PipelineJobCommand,
    ) -> None:
        pipeline = None
        job = command.job
        callbacks = command.callbacks
        try:
            pipeline_config = self._create_pipeline_config(**command.pipeline_config_kwargs())
            command.adjust_pipeline_config(pipeline_config)
            on_progress, on_verbose = self._create_pipeline_callbacks(
                callbacks,
                job_id=getattr(job, "id", None),
            )
            pipeline = self._pipeline_cls(
                config=pipeline_config,
                progress_callback=on_progress,
                verbose_callback=on_verbose,
            )
            if isinstance(command, ConversionRunCommand):
                register_recovery = getattr(pipeline, "set_recovery_callback", None)
                if callable(register_recovery):
                    register_recovery(lambda source, result: self._checkpoint_conversion(command, source, result))
            self._tts_session.attach(pipeline)
            cancel_requested = self._attach_pipeline_to_job(job, pipeline)
            if cancel_requested:
                pipeline.cancel()
                self._handle_conversion_cancelled(job=job, callbacks=callbacks)
                return

            pipeline_result = command.run(pipeline, pipeline_config)
            controller_result = pipeline_to_controller_result(
                pipeline_result,
                emit_chapter_mp3_artifacts=pipeline_config.emit_chapter_mp3_artifacts,
                require_packaged_output=True,
            )
            self._set_job_result(job, controller_result)

            if command.is_cancelled_result(pipeline_result):
                self._handle_conversion_cancelled(
                    job=job,
                    callbacks=callbacks,
                    message=command.cancel_message(pipeline_result),
                )
                return
            if not pipeline_result.success:
                self._handle_conversion_failure(
                    job=job,
                    error_message=pipeline_result.error or command.failure_message,
                    callbacks=callbacks,
                )
                return
            if not controller_result.success:
                self._handle_conversion_failure(
                    job=job,
                    error_message=controller_result.error_message
                    or command.invalid_artifacts_message,
                    callbacks=callbacks,
                )
                return

            command.handle_success(self, pipeline_result, controller_result, pipeline_config)
        except Exception as exc:
            self._handle_conversion_failure(
                job=job,
                error_message=str(exc),
                callbacks=callbacks,
            )
        finally:
            self._tts_session.release(
                pipeline,
                cleanup_first=bool(pipeline is not None and command.cleanup_first(pipeline)),
            )
            self._detach_pipeline_from_job(job)
            self._emit_conversion_summary(callbacks, job_id=getattr(job, "id", None))

    @staticmethod
    def _attach_pipeline_to_job(job: Any, pipeline: Any) -> bool:
        attach_pipeline = getattr(job, "attach_pipeline", None)
        if callable(attach_pipeline):
            return bool(attach_pipeline(pipeline))
        job.pipeline = pipeline
        return bool(getattr(job, "cancel_requested", False))

    @staticmethod
    def _detach_pipeline_from_job(job: Any) -> None:
        detach_pipeline = getattr(job, "detach_pipeline", None)
        if callable(detach_pipeline):
            detach_pipeline()
        else:
            job.pipeline = None

    @staticmethod
    def _set_job_result(job: Any, result: Any) -> None:
        set_result = getattr(job, "set_result", None)
        if callable(set_result):
            set_result(result)
        else:
            job.result = result

    def _run_repair(
        self,
        *,
        job: Any,
        book_id: int,
        callbacks: Optional[Any] = None,
        runtime_options: Optional[ConversionRuntimeOptions] = None,
    ) -> None:
        runtime_options = runtime_options or ConversionRuntimeOptions()
        try:
            repair_context = resolve_repair_context(self, book_id=book_id)
        except Exception as exc:
            self._handle_conversion_failure(
                job=job,
                error_message=str(exc),
                callbacks=callbacks,
            )
            self._emit_conversion_summary(callbacks, job_id=getattr(job, "id", None))
            return

        self._execute_pipeline_job(
            command=RepairRunCommand(
                job=job,
                callbacks=callbacks,
                runtime_options=runtime_options,
                conversion_mode=getattr(
                    repair_context.book,
                    "conversion_mode",
                    ConversionMode.AUDIOBOOK,
                ),
                failure_message="Repair failed",
                invalid_artifacts_message="Invalid repair artifacts",
                cancel_message_text="repair cancelled",
                book_id=book_id,
                repair_context=repair_context,
            ),
        )

    def _run_conversion(
        self,
        job: Any,
        source_path: Path,
        callbacks: Optional[Any],
        conversion_mode: ConversionMode | str = ConversionMode.AUDIOBOOK,
        title: Optional[str] = None,
        author: Optional[str] = None,
        runtime_options: Optional[ConversionRuntimeOptions] = None,
    ) -> None:
        runtime_options = runtime_options or ConversionRuntimeOptions()

        self._execute_pipeline_job(
            command=ConversionRunCommand(
                job=job,
                callbacks=callbacks,
                runtime_options=runtime_options,
                conversion_mode=conversion_mode,
                failure_message="Unknown error",
                invalid_artifacts_message="Invalid conversion artifacts",
                cancel_message_text="conversion cancelled",
                source_path=source_path,
                title=title,
                author=author,
            ),
        )
