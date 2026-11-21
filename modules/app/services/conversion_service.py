"""Conversion orchestration service."""

from __future__ import annotations

import json
import re
import hashlib
import logging
import threading
import shutil
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from modules.app.config import AppConfig
from modules.app.events import make_log_event, make_progress_event
from modules.errors import SourceFileNotFoundError
from modules.app.services.conversion_paths import (
    playback_metadata_for_pipeline_result,
    repair_output_dirs,
    resolve_conversion_stored_path,
)
from modules.app.services.conversion_result_adapter import pipeline_to_controller_result, repair_chapter_updates
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
from modules.app.services.job_coordinator import JobCoordinator
from modules.app.services.tts_session import TtsEngineSession
from modules.pipeline.models import ConversionResult as PipelineConversionResult
from modules.pipeline.orchestrator import ConversionPipeline, PipelineConfig
from modules.pipeline.progress import estimate_eta_from_progress
from modules.storage.models import (
    BookCreate,
    ChapterCreate,
    ConversionMode,
    SourceType,
)
from modules.storage.repository import IBookRepository
from modules.storage.sqlite_repo import LibraryReplacedError
from modules.storage.permissions import ensure_owner_only_directory, owner_only
from modules.tts.api_rate_limit import GlobalAPILimiter

if TYPE_CHECKING:
    from modules.app.controller import ConversionJob

logger = logging.getLogger(__name__)


# ``str(OSError)`` reads "[WinError 5] Access is denied: 'C:\\\\path'": the path
# is repr()'d, so its backslashes arrive doubled.
_ACCESS_DENIED_RE = re.compile(
    r"(?:\[WinError 5\]|\[Errno 13\]|Access is denied|Permission denied)[^']*'(?P<path>[^']+)'"
)


def describe_conversion_failure(message: str, *, output_dir: Path, temp_dir: Path) -> str:
    """Name the cause of a permission failure in plain words; keep other messages."""
    match = _ACCESS_DENIED_RE.search(str(message or ""))
    if match is None:
        return message
    path = Path(match["path"].replace("\\\\", "\\"))
    for root, label in ((Path(output_dir), "output folder"), (Path(temp_dir), "working folder")):
        try:
            path.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        return (
            f"Orpheus cannot write to the {label} {root}. Check that the folder exists and "
            "that you are allowed to change it, then try again."
        )
    return f"Orpheus was not allowed to open {path}. Check its permissions, then try again."


def _file_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return stat_result.st_size, stat_result.st_mtime_ns


def _prehash_recovery_source(upload_source: Path | None, recovery_source: Path) -> tuple | None:
    """Digest a recovery source outside the job lock: (path, identity, sha256).

    The result is used only if the file still has the same resolved path, size
    and modification time when the caller re-checks it under the lock.
    """
    if upload_source is None or recovery_source.is_symlink() or not recovery_source.is_file():
        return None
    before = _file_identity(recovery_source)
    try:
        with recovery_source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        resolved = recovery_source.resolve()
    except OSError:
        return None
    if before is None or _file_identity(recovery_source) != before:
        return None
    return resolved, before, digest


def _matching_prehash(prehashed: tuple | None, recovery_source: Path) -> str | None:
    if prehashed is None:
        return None
    resolved, identity, digest = prehashed
    try:
        if recovery_source.resolve() != resolved:
            return None
    except OSError:
        return None
    return digest if _file_identity(recovery_source) == identity else None


class ConversionService:
    """Orchestrates conversion pipelines with coordinated job ownership."""

    _PROGRESS_EMIT_INTERVAL_SECONDS = 0.25
    _PROGRESS_EMIT_EPSILON_SECONDS = 1e-9

    def __init__(
        self,
        *,
        config: AppConfig,
        repository: IBookRepository,
        job_cls: type[ConversionJob],
        status_enum: Any,
        cleaner_api_limiter: GlobalAPILimiter,
        runtime_validator: Callable[[str, Optional[str]], None],
        shared_worker_pool_manager: Optional[Any] = None,
        pipeline_cls: type = ConversionPipeline,
        book_cleanup_paths_resolver: Callable[[int], list[tuple[Path, bool]]] | None = None,
    ) -> None:
        self._config = config
        self._repository = repository
        self._status = status_enum
        self._cleaner_api_limiter = cleaner_api_limiter
        self._runtime_validator = runtime_validator
        self._shared_worker_pool_manager = shared_worker_pool_manager
        self._pipeline_cls = pipeline_cls
        self._book_cleanup_paths_resolver = book_cleanup_paths_resolver
        self._runtime_options_resolver = RuntimeOptionsResolver(
            config=config,
            cleaner_api_limiter=cleaner_api_limiter,
            runtime_validator=runtime_validator,
            shared_worker_pool_manager=shared_worker_pool_manager,
        )
        self._job_coordinator = JobCoordinator(
            job_cls=job_cls,
            status_enum=status_enum,
            shared_worker_pool_manager=shared_worker_pool_manager,
        )
        self._recovered_upload_sources: dict[Path, tuple[int, Path, str, int]] = {}
        self._tts_session = TtsEngineSession()
        self.upload_reference_checker: Callable[[Path], bool] | None = None

    @property
    def _active_job(self) -> Any:
        return self._job_coordinator._active_job

    @_active_job.setter
    def _active_job(self, job: Any) -> None:
        self._job_coordinator._active_job = job

    @property
    def _job_lock(self):
        return self._job_coordinator._job_lock

    @property
    def _storage_cleanup_active(self) -> bool:
        return self._job_coordinator._storage_cleanup_active

    @property
    def _book_cleanup_ids(self) -> dict[int, int]:
        return self._job_coordinator._book_cleanup_ids

    def upload_cleanup_guard(self, source_path: Path | None = None):
        return self._job_coordinator.upload_cleanup_guard(source_path)

    def begin_shutdown(self) -> None:
        return self._job_coordinator.begin_shutdown()

    def cleanup(self) -> None:
        self._tts_session.cleanup()

    def _preflight_job_slot_available(self, *, book_id: int | None = None) -> None:
        return self._job_coordinator._preflight_job_slot_available(book_id=book_id)

    def begin_storage_cleanup(self) -> None:
        return self._job_coordinator.begin_storage_cleanup()

    def storage_cleanup_blocker(self) -> str | None:
        return self._job_coordinator.storage_cleanup_blocker()

    def end_storage_cleanup(self) -> None:
        return self._job_coordinator.end_storage_cleanup()

    def begin_book_cleanup(self, book_id: int) -> None:
        def resolve_cleanup_paths() -> list[tuple[Path, bool]]:
            if self._book_cleanup_paths_resolver is not None:
                return self._book_cleanup_paths_resolver(book_id)
            book = self._repository.get_book(book_id)
            raw_path = getattr(book, "source_path", None)
            if not isinstance(raw_path, (str, Path)):
                return []
            source = resolve_conversion_stored_path(
                str(raw_path), db_path=Path(self._config.db_path),
                output_dir=self._config.output_dir, temp_dir=self._config.temp_dir,
            )
            return [(source, False)] if source is not None else []

        return self._job_coordinator.begin_book_cleanup(
            book_id, resolve_cleanup_paths=resolve_cleanup_paths,
        )

    def end_book_cleanup(self, book_id: int) -> None:
        return self._job_coordinator.end_book_cleanup(book_id)

    def start_conversion(
        self,
        source_path: Path,
        callbacks: Optional[Any] = None,
        conversion_mode: ConversionMode | str = ConversionMode.AUDIOBOOK,
        title: Optional[str] = None,
        author: Optional[str] = None,
        runtime_options: Optional[ConversionRuntimeOptions] = None,
        client_request_id: str | None = None,
    ) -> Any:
        if not source_path.exists():
            raise SourceFileNotFoundError(source_path)
        if not source_path.is_file():
            raise ValueError("Choose a source file, not a folder or another non-file item.")

        self._preflight_job_slot_available()
        runtime_options = runtime_options or ConversionRuntimeOptions()
        job = self._job_coordinator.reserve_conversion(source_path, title, author, client_request_id)

        # Copying a large upload into durable storage must not run under the
        # job lock: status reads (/active), cancel, and cleanup guards would
        # block for the whole copy. The reserved IDLE job above keeps
        # concurrent starts out while the copy runs.
        try:
            runtime_options = self._runtime_options_resolver.with_setup_defaults(runtime_options)
            self._validate_requested_tts_runtime(runtime_options=runtime_options)
            source_path = self._durable_source_path_for_conversion(source_path)
        except Exception:
            self._job_coordinator.release_reservation(job)
            raise

        try:
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
        except Exception:
            self._remove_unregistered_upload_copy(job, source_path)
            raise

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
                if not existing.is_symlink() and existing.is_file():
                    owner_only(existing)
        except OSError:
            pass
        durable_path = sources_dir / source_path.name
        index = 1
        while True:
            try:
                destination_file = durable_path.open("xb")
            except FileExistsError:
                durable_path = sources_dir / f"{source_path.stem}_{index}{source_path.suffix}"
                index += 1
                continue
            # Only a file this call created may be removed on failure: any other
            # entry at this name can be another book's durable source.
            try:
                with destination_file, source_path.open("rb") as source_file:
                    shutil.copyfileobj(source_file, destination_file)
                shutil.copystat(source_path, durable_path)
                owner_only(durable_path)
            except BaseException:
                self._cleanup_partial_durable_source(durable_path)
                raise
            return durable_path

    @staticmethod
    def _cleanup_partial_durable_source(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove partial durable source copy: %s", path, exc_info=True)

    @contextmanager
    def repair_reservation(self, book_id: int):
        self._preflight_job_slot_available(book_id=book_id)
        job = self._job_coordinator.reserve_repair(book_id, None)
        try:
            yield job
        except BaseException:
            self._job_coordinator.release_reservation(job)
            raise

    def start_repair(
        self,
        book_id: int,
        callbacks: Optional[Any] = None,
        runtime_options: Optional[ConversionRuntimeOptions] = None,
        runtime_fields_set: Optional[set[str] | frozenset[str]] = None,
        *,
        reserved_job: Any = None,
        expected_generation: int | None = None,
    ) -> Any:
        request_options = runtime_options or ConversionRuntimeOptions()

        if reserved_job is None:
            with self.repair_reservation(book_id) as job:
                return self.start_repair(
                    book_id, callbacks, runtime_options, runtime_fields_set,
                    reserved_job=job,
                    expected_generation=getattr(self._repository, "library_generation", 0),
                )

        if expected_generation is None:
            expected_generation = getattr(self._repository, "library_generation", 0)

        # None means "no explicit overrides" (controller/direct callers), same as
        # an empty HTTP payload's fields_set — reuse persisted v7 / app defaults.
        fields_set = (
            runtime_fields_set if runtime_fields_set is not None else frozenset()
        )
        detail = self._repository.get_book_with_chapters(book_id)
        if detail is None:
            raise ValueError("Book not found")
        book, _chapters = detail
        job = reserved_job
        job.title = getattr(book, "title", None)
        job.author = getattr(book, "author", None)
        resolved_options = resolve_repair_runtime_options(
            book=book,
            request_options=request_options,
            request_fields_set=fields_set,
            app_config=self._config,
        )
        resolved_options = self._runtime_options_resolver.with_setup_defaults(resolved_options)
        self._validate_requested_tts_runtime(runtime_options=resolved_options)

        snapshot = getattr(self._repository, "library_snapshot", None)
        start_gate = threading.Event()
        launch_error = None
        # Launch behind a gate so replacement stays excluded through launch,
        # while observers run without the job or repository lock held.
        with self._job_lock, snapshot() if callable(snapshot) else nullcontext():
            if expected_generation != getattr(self._repository, "library_generation", 0):
                if self._active_job is job:
                    self._active_job = None
                raise LibraryReplacedError()
            try:
                self._start_pipeline_job(
                    job=job,
                    target=self._run_repair,
                    kwargs={
                        "job": job,
                        "book_id": book_id,
                        "callbacks": callbacks,
                        "runtime_options": resolved_options,
                    },
                    callbacks=None,
                    started_message="repair started",
                    start_gate=start_gate,
                )
            except Exception as exc:
                launch_error = exc
        if launch_error is not None:
            self._handle_conversion_failure(job=job, error_message=str(launch_error), callbacks=callbacks)
            raise launch_error
        try:
            self._job_coordinator.publish_running(
                job=job, callbacks=callbacks, started_message="repair started",
            )
        finally:
            start_gate.set()
        return job

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
        start_gate: threading.Event | None = None,
    ) -> Any:
        return self._job_coordinator._start_pipeline_job(
            job=job,
            target=target,
            callbacks=callbacks,
            started_message=started_message,
            args=args,
            kwargs=kwargs,
            start_gate=start_gate,
        )

    def _normalize_runtime_options(
        self,
        runtime_options: ConversionRuntimeOptions,
    ) -> NormalizedConversionRuntimeOptions:
        return self._runtime_options_resolver.normalize(runtime_options)

    def get_active_job(self) -> Any:
        return self._job_coordinator.get_active_job()

    def has_active_job(self) -> bool:
        return self._job_coordinator.has_active_job()

    def cancel_conversion(self, expected_job_id: str | None = None) -> bool:
        return self._job_coordinator.cancel_conversion(expected_job_id)

    def cancel_active_job(self, timeout_seconds: Optional[float] = None) -> bool:
        return self._job_coordinator.cancel_active_job(timeout_seconds)

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
                    packaged_start_ms=chapter.packaged_start_ms,
                )
                for chapter in pipeline_result.chapters
            ],
        )
        return book.id

    def _checkpoint_conversion(self, command: ConversionRunCommand, source_path: Path, result: PipelineConversionResult) -> None:
        # Only this job's thread assigns book_id, so it can be read unlocked.
        book_id = getattr(command.job, "book_id", None)
        if book_id is not None:
            self._update_conversion_library_records(book_id, result)
            return
        # Hash the (up to MAX_UPLOAD_BYTES) recovery source before taking the
        # job lock, which status reads and cancellation also need.
        prehashed = _prehash_recovery_source(getattr(command.job, "upload_source_path", None), source_path)
        # Registration and reservation share the job lock: deletion must never
        # see the new book before it has become owned by the active job.
        with self._job_lock:
            book_id = self._persist_conversion_library_records(
                source_path, result, conversion_mode=command.conversion_mode,
                runtime_options=command.runtime_options,
            )
            command.job.book_id = book_id
            self._remove_intermediate_upload_source(command, source_path)
            self._remove_recoverable_upload(command.job, book_id, prehashed)

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
                    "packaged_start_ms": by_number[stored.chapter_number].packaged_start_ms,
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
            # No recovery checkpoint registered this run, so its upload is
            # transferred here; otherwise the checkpoint already did that.
            book_id = self._persist_conversion_library_records(
                source_path,
                pipeline_result,
                conversion_mode=conversion_mode,
                runtime_options=runtime_options,
            )
            prehashed = _prehash_recovery_source(getattr(job, "upload_source_path", None), source_path)
            with self._job_lock:
                self._remove_recoverable_upload(job, book_id, prehashed)
        else:
            self._update_conversion_library_records(book_id, pipeline_result)
        self._job_coordinator.publish_success(job=job, book_id=book_id, callbacks=callbacks)

    def _is_unreferenced_upload_copy(self, job: Any, source: Path | None) -> bool:
        """Whether ``source`` is start_conversion's data/sources copy of an upload
        that no library book references."""
        upload = getattr(job, "upload_source_path", None)
        upload_root = (self._config.temp_dir / "web_uploads").resolve()
        if upload is None or not upload.resolve().is_relative_to(upload_root):
            return False
        sources_root = (Path(self._config.db_path).parent / "sources").resolve()
        if source is None or source.is_symlink() or not source.resolve().is_relative_to(sources_root):
            return False
        return self.upload_reference_checker is not None and not self.upload_reference_checker(source)

    def _remove_intermediate_upload_source(self, command: ConversionRunCommand, recovery_source: Path) -> None:
        source = getattr(command, "source_path", None)
        if not self._is_unreferenced_upload_copy(command.job, source):
            return
        if source.resolve() == recovery_source.resolve() or not recovery_source.is_file():
            return
        try:
            source.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove intermediate upload copy: %s", source)

    def _remove_unregistered_upload_copy(self, job: Any, source: Path) -> None:
        # The upload itself stays queued, so a retry makes a fresh copy.
        if getattr(job, "book_id", None) is None and self._is_unreferenced_upload_copy(job, source):
            self._cleanup_partial_durable_source(source)

    def resolve_recovered_upload(self, upload_path: Path) -> Path | None:
        """Resolve only uploads transferred to a registered recovery book."""
        upload_path = upload_path.resolve()
        with self._job_lock:
            registered = self._registered_recovery_source_locked(upload_path)
        if registered is None:
            return None
        source, identity = registered
        # Hash outside the job lock; the registration and the file identity are
        # checked again under it, so a restore, deletion or rewrite meanwhile
        # still refuses the handle.
        prehashed = _prehash_recovery_source(upload_path, source)
        if prehashed is None or prehashed[2] != identity[2]:
            return None
        with self._job_lock:
            if self._registered_recovery_source_locked(upload_path) != registered:
                return None
            if _matching_prehash(prehashed, source) is None:
                return None
            return source

    def _registered_recovery_source_locked(self, upload_path: Path) -> tuple[Path, tuple] | None:
        identity = self._recovered_upload_sources.get(upload_path)
        if identity is None:
            return None
        book_id, original_source, _digest, generation = identity
        if book_id in self._book_cleanup_ids:
            return None
        if generation != getattr(self._repository, "library_generation", 0):
            self._recovered_upload_sources.pop(upload_path, None)
            return None
        book = self._repository.get_book(book_id)
        if book is None:
            self._recovered_upload_sources.pop(upload_path, None)
            return None
        source = Path(book.source_path)
        if source.is_symlink() or not source.is_file() or source.resolve() != original_source:
            return None
        return source, identity

    def invalidate_recovered_uploads(self) -> None:
        with self._job_lock:
            self._recovered_upload_sources.clear()

    def _remove_recoverable_upload(self, job: Any, book_id: int, prehashed: tuple | None = None) -> None:
        source = getattr(job, "upload_source_path", None)
        if source is None or not source.is_relative_to((self._config.temp_dir / "web_uploads").resolve()):
            return
        if self.upload_reference_checker is None or self.upload_reference_checker(source):
            return
        book = self._repository.get_book(book_id)
        if book is None:
            return
        recovery_source = Path(book.source_path)
        if recovery_source.is_symlink() or not recovery_source.is_file():
            return
        digest = _matching_prehash(prehashed, recovery_source)
        if digest is None:
            try:
                with recovery_source.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
            except OSError:
                return
        # A book id can be reused by a restored library; retain the source identity.
        self._recovered_upload_sources[source.resolve()] = (
            book_id, recovery_source.resolve(), digest,
            getattr(self._repository, "library_generation", 0),
        )
        try:
            source.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove copied upload: %s", source)

    def _handle_conversion_failure(
        self,
        *,
        job: Any,
        error_message: str,
        callbacks: Optional[Any],
    ) -> None:
        return self._job_coordinator._handle_conversion_failure(
            job=job,
            error_message=describe_conversion_failure(
                error_message, output_dir=self._config.output_dir, temp_dir=self._config.temp_dir,
            ),
            callbacks=callbacks,
        )

    def _handle_conversion_cancelled(
        self,
        *,
        job: Any,
        callbacks: Optional[Any],
        message: str = "conversion cancelled",
    ) -> None:
        return self._job_coordinator._handle_conversion_cancelled(
            job=job, callbacks=callbacks, message=message,
        )

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
            JobCoordinator._deliver_observer(callbacks.on_log, message, "info")
        if callbacks and callbacks.on_event:
            # No performance payload here: the pool snapshot describes the idle
            # worker pool (parallel mode, zero counters), and clients treat any
            # attached snapshot as the job's final performance numbers.
            JobCoordinator._deliver_observer(
                callbacks.on_event,
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
            cancel_requested = job.attach_pipeline(pipeline)
            if cancel_requested:
                pipeline.cancel()
                self._handle_conversion_cancelled(job=job, callbacks=callbacks)
                return

            pipeline_result = command.run(pipeline)
            controller_result = pipeline_to_controller_result(
                pipeline_result,
                emit_chapter_mp3_artifacts=pipeline_config.emit_chapter_mp3_artifacts,
                require_packaged_output=True,
            )
            job.set_result(controller_result)

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

            if isinstance(command, RepairRunCommand):
                pipeline_result.repaired_chapter_updates = repair_chapter_updates(
                    pipeline_result, command.repair_context.chapters,
                )
            command.handle_success(self, pipeline_result, controller_result)
        except Exception as exc:
            if job.status == self._status.COMPLETED:
                # Repair commands also deliver their terminal event after commit.
                logger.exception("Conversion observer delivery failed after completion")
            else:
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
            job.detach_pipeline()
            self._emit_conversion_summary(callbacks, job_id=getattr(job, "id", None))

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

        try:
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
        finally:
            self._remove_unregistered_upload_copy(job, source_path)
