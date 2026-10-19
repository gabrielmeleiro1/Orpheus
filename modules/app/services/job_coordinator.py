"""Admission, cancellation, and terminal publication for conversion jobs."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from itertools import count
from pathlib import Path
from typing import Any, Callable, Optional

from modules.app.events import JobState, make_state_event
from modules.errors import ConversionCleanupPendingError
from modules.storage.models import ConversionResult

logger = logging.getLogger(__name__)


class JobCoordinator:
    """Own the active job and its admission and maintenance reservations."""

    _DEFAULT_CANCEL_WAIT_SECONDS = 2.0

    def __init__(
        self,
        *,
        job_cls: type,
        status_enum: Any,
        shared_worker_pool_manager: Optional[Any] = None,
    ) -> None:
        self._job_cls = job_cls
        self._status = status_enum
        self._shared_worker_pool_manager = shared_worker_pool_manager
        self._active_job = None
        self._job_lock = threading.Lock()
        self._cancelling_jobs = 0
        self._shutting_down = False
        self._job_sequence = count()
        self._storage_cleanup_active = False
        self._storage_cleanup_owner: int | None = None
        # book id -> owning thread ident; only the owner may end the reservation.
        self._book_cleanup_ids: dict[int, int] = {}
        self._book_cleanup_paths: dict[int, tuple[tuple[Path, bool], ...]] = {}

    @contextmanager
    def upload_cleanup_guard(self, source_path: Path | None = None):
        with self._job_lock:
            if self._storage_cleanup_active:
                raise RuntimeError("Storage cleanup is in progress")
            self._ensure_cleanup_quiesced_locked()
            job = self._active_job
            if job is not None and self._job_is_active_or_pending(job):
                active_source = getattr(job, "upload_source_path", None)
                if source_path is None or active_source is None or active_source == source_path.resolve():
                    raise RuntimeError("An active conversion still references this upload")
            yield

    def _job_is_active_or_pending(self, job: Any) -> bool:
        thread = getattr(job, "_thread", None)
        if thread is not None and thread.is_alive():
            return True
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

    def _request_job_cancel(self, job: Any, cancellation: tuple[bool, Any] | None = None) -> bool:
        if cancellation is not None:
            cancelled, pipeline = cancellation
            try:
                if pipeline is not None:
                    pipeline.cancel()
            except Exception:
                logger.exception("Failed to request conversion job cancellation")
                return False
            return cancelled
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

    def begin_shutdown(self) -> None:
        """Close admission before waiting for the current conversion to exit."""
        with self._job_lock:
            self._shutting_down = True

    def _ensure_job_slot_available_locked(self) -> None:
        """Refuse new jobs while one is active or a storage cleanup runs.

        Caller must hold ``self._job_lock``.
        """
        if self._shutting_down:
            raise RuntimeError("Conversion service is shutting down")
        if self._storage_cleanup_active:
            raise RuntimeError("A storage cleanup is in progress")
        job = self._active_job
        job_owns_resources = job is not None and self._job_is_active_or_pending(job)
        if self._cleanup_pending_locked() or job_owns_resources:
            if getattr(job, "status", None) in {
                self._status.COMPLETED, self._status.FAILED, self._status.CANCELLED,
            }:
                # Terminal publication can precede worker or cancellation pool
                # cleanup, even after the job thread exits. Retrying this refusal
                # cannot create a duplicate job because admission is still closed.
                raise ConversionCleanupPendingError()
            if not job_owns_resources:
                raise ConversionCleanupPendingError()
            raise RuntimeError("A conversion is already in progress")

    def _cleanup_pending_locked(self) -> bool:
        return bool(
            self._cancelling_jobs
            or getattr(self._shared_worker_pool_manager, "cleanup_pending", False) is True
        )

    def _ensure_cleanup_quiesced_locked(self) -> None:
        if self._cleanup_pending_locked():
            raise ConversionCleanupPendingError()

    def _preflight_job_slot_available(self, *, book_id: int | None = None) -> None:
        """Fail fast before potentially expensive runtime validation.

        The caller must still reserve the slot under ``_job_lock`` before
        validation because another request can start in between.
        """
        with self._job_lock:
            if book_id is not None and int(book_id) in self._book_cleanup_ids:
                raise RuntimeError("Book cleanup is in progress")
            self._ensure_job_slot_available_locked()

    def storage_cleanup_blocker(self) -> str | None:
        """Why ``begin_storage_cleanup`` would refuse right now, without reserving."""
        with self._job_lock:
            if self._cleanup_pending_locked():
                return "conversion_cleanup_pending"
            if self._active_job is not None and self._job_is_active_or_pending(self._active_job):
                return "conversion_in_progress"
            if self._storage_cleanup_active or self._book_cleanup_ids:
                return "maintenance_in_progress"
        return None

    def begin_storage_cleanup(self) -> None:
        """Atomically verify no job is active and block new starts until
        :meth:`end_storage_cleanup` — closes the check-then-prune race."""
        with self._job_lock:
            if self._storage_cleanup_active:
                raise RuntimeError("Storage cleanup is already in progress")
            self._ensure_cleanup_quiesced_locked()
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

    def begin_book_cleanup(
        self, book_id: int, *, resolve_cleanup_paths: Callable[[], list[tuple[Path, bool]]] | None = None,
    ) -> None:
        """Reserve a book for deletion while preventing a same-book job start."""
        normalized_book_id = int(book_id)
        with self._job_lock:
            self._ensure_cleanup_quiesced_locked()
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
            # A new conversion has no book ID until recovery registration, but
            # may read a previous book's source or generated Markdown. Resolve while
            # admission is locked, preserving the job -> repository lock order.
            cleanup_paths = tuple(
                (path.resolve(), recursive)
                for path, recursive in (resolve_cleanup_paths() if resolve_cleanup_paths else [])
            )
            active_source = getattr(active_job, "upload_source_path", None)
            if (
                active_job is not None
                and isinstance(active_source, (Path, str))
                and self._source_matches_cleanup(Path(active_source).resolve(), cleanup_paths)
                and self._job_is_active_or_pending(active_job)
            ):
                raise RuntimeError("Cannot delete a book while its files are a conversion source")
            self._book_cleanup_paths[normalized_book_id] = cleanup_paths
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
            self._book_cleanup_paths.pop(normalized_book_id, None)

    @staticmethod
    def _source_matches_cleanup(source: Path, cleanup_paths: tuple[tuple[Path, bool], ...]) -> bool:
        return any(
            source == path or (recursive and source.is_relative_to(path))
            for path, recursive in cleanup_paths
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
        def run_when_ready():
            start_gate.wait()
            if getattr(job, "status", None) == self._status.RUNNING:
                target(*args, **(kwargs or {}))

        thread = threading.Thread(
            target=run_when_ready if start_gate is not None else target,
            args=() if start_gate is not None else args,
            kwargs={} if start_gate is not None else kwargs or {},
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
        try:
            if start_gate is None and callbacks and callbacks.on_event:
                callbacks.on_event(make_state_event(JobState.RUNNING, job.id, started_message))
            thread.start()
        except Exception as exc:
            if start_gate is None:
                self._handle_conversion_failure(job=job, error_message=str(exc), callbacks=callbacks)
            raise
        return job

    def publish_running(self, *, job: Any, callbacks: Optional[Any], started_message: str) -> None:
        try:
            if callbacks and callbacks.on_event:
                callbacks.on_event(make_state_event(JobState.RUNNING, job.id, started_message))
        except Exception as exc:
            self._handle_conversion_failure(job=job, error_message=str(exc), callbacks=callbacks)
            raise

    def get_active_job(self) -> Any:
        with self._job_lock:
            return self._active_job

    def has_active_job(self) -> bool:
        with self._job_lock:
            return self._active_job is not None and self._job_is_active_or_pending(
                self._active_job
            )

    def cancel_conversion(self, expected_job_id: str | None = None) -> bool:
        # Reserve cancellation ownership under the admission lock. Blocking
        # pipeline/pool calls run outside it, while successors remain excluded.
        with self._job_lock:
            job = self._active_job
            if (
                job is None
                or (expected_job_id is not None and str(job.id) != expected_job_id)
                or not self._job_is_active_or_pending(job)
            ):
                return False
            request_cancel = getattr(job, "request_cancel", None)
            cancellation = request_cancel() if callable(request_cancel) else None
            self._cancelling_jobs += 1
        try:
            cancelled = self._request_job_cancel(job, cancellation)
            if cancelled:
                self._shutdown_shared_worker_pool_for_cancel()
            return cancelled
        finally:
            with self._job_lock:
                self._cancelling_jobs -= 1

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

        timeout = (
            self._DEFAULT_CANCEL_WAIT_SECONDS
            if timeout_seconds is None
            else max(0.0, float(timeout_seconds))
        )
        deadline = time.monotonic() + timeout
        self.cancel_conversion(expected_job_id=str(job.id))
        timeout = max(0.0, deadline - time.monotonic())
        if getattr(job, "_thread", None) is None:
            return not self._job_is_active_or_pending(job)
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

    @staticmethod
    def _deliver_observer(callback: Callable[..., None] | None, *args: Any) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            logger.exception("Conversion observer delivery failed")

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
        if callbacks:
            self._deliver_observer(callbacks.on_error, error_message)
            self._deliver_observer(
                callbacks.on_event, make_state_event(JobState.FAILED, job.id, error_message),
            )

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
            self._deliver_observer(
                callbacks.on_event, make_state_event(JobState.CANCELLED, job.id, message),
            )

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

    def reserve_conversion(
        self, source_path: Path, title: Optional[str], author: Optional[str],
        client_request_id: str | None = None,
    ) -> Any:
        with self._job_lock:
            self._ensure_job_slot_available_locked()
            resolved_source = source_path.resolve()
            if any(
                self._source_matches_cleanup(resolved_source, paths)
                for paths in self._book_cleanup_paths.values()
            ):
                raise RuntimeError("Cannot convert a source while its library book is being deleted")
            job_id = f"conv_{time.time_ns()}_{next(self._job_sequence)}"
            job = self._job_cls(
                id=job_id,
                book_id=None,
                status=self._status.IDLE,
                title=title,
                author=author,
            )
            job.client_request_id = client_request_id
            job.upload_source_path = resolved_source
            self._active_job = job
        return job

    def reserve_repair(self, book_id: int, book: Any) -> Any:
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
        return job

    def release_reservation(self, job: Any) -> None:
        with self._job_lock:
            if self._active_job is job:
                self._active_job = None

    def publish_success(self, *, job: Any, book_id: int, callbacks: Optional[Any]) -> None:
        complete = getattr(job, "complete", None)
        if callable(complete):
            complete(book_id)
        else:
            job.status = self._status.COMPLETED
            job.book_id = book_id
        if callbacks:
            if job.result is not None:
                self._deliver_observer(callbacks.on_complete, job.result)
            self._deliver_observer(
                callbacks.on_event,
                make_state_event(JobState.COMPLETED, job.id, "conversion completed"),
            )
