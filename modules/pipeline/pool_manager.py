"""Shared process-pool manager for cross-conversion reuse."""

from __future__ import annotations

import multiprocessing as mp
import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .models import ChapterResult
from .worker_errors import StartupErrorKind, WorkerStartupError, WorkerStartupStatus
from .worker_runtime import _init_worker, get_worker_startup_status

logger = logging.getLogger(__name__)

_WAIT_POLL_TIMEOUT_SECONDS = 0.25


def _health_ping() -> bool:
    """Top-level no-op used for worker liveness checks."""
    return True


def _worker_startup_probe() -> WorkerStartupStatus:
    """Validate that the worker process completed model initialization."""
    return get_worker_startup_status()


@dataclass
class _PoolState:
    executor: Optional[Any] = None
    config_fingerprint: Any = None
    max_workers: int = 0
    available_vram_gb: float = 8.0
    worker_init_config: Optional[dict[str, Any]] = None
    use_processes: bool = True

    @property
    def available_device_memory_gb(self) -> float:
        return self.available_vram_gb

    @available_device_memory_gb.setter
    def available_device_memory_gb(self, value: float) -> None:
        self.available_vram_gb = value


class SharedWorkerPoolManager:
    """
    Reusable process-pool owner for chapter parallelism.

    The pool is reused across conversions when fingerprint and worker config are
    unchanged, avoiding repeated process/model load and warm-up overhead.
    """

    def __init__(
        self,
        max_workers: int = 2,
        available_vram_gb: float = 8.0,
        available_device_memory_gb: Optional[float] = None,
        shutdown_timeout_seconds: float = 2.0,
        startup_probe_timeout_seconds: float = 30.0,
    ) -> None:
        if available_device_memory_gb is not None and float(available_vram_gb) != 8.0:
            raise ValueError(
                "Pass only one of available_vram_gb or available_device_memory_gb."
            )
        resolved_available_memory_gb = (
            available_vram_gb
            if available_device_memory_gb is None
            else available_device_memory_gb
        )
        self._state = _PoolState(
            executor=None,
            config_fingerprint=None,
            max_workers=max(1, int(max_workers)),
            available_vram_gb=float(resolved_available_memory_gb),
            worker_init_config=None,
        )
        self._lock = threading.RLock()
        # A manager owns one shared executor; concurrent runs would otherwise
        # reset each other's progress state and race cancellation against pool
        # replacement.  Serialize run ownership while retaining worker-level
        # chapter parallelism inside each run.
        self._run_lock = threading.Lock()
        self._reuse_hits = 0
        self._restarts = 0
        self._health_failures = 0
        self._jobs_run = 0
        self._chapters_run = 0
        self._last_start_ms: Optional[float] = None
        self._current_total = 0
        self._current_submitted: set[int] = set()
        self._current_completed: set[int] = set()
        self._current_failed: set[int] = set()
        self._current_chapter_titles: dict[int, str] = {}
        self._use_processes = True
        self._shutdown_timeout_seconds = max(0.0, float(shutdown_timeout_seconds))
        self._startup_probe_timeout_seconds = max(0.0, float(startup_probe_timeout_seconds))
        self._startup_failures = 0
        self._last_startup_error: Optional[str] = None
        self._cancelled_jobs = 0
        self._stopping_executor: Any | None = None
        self._stopping_done = threading.Event()
        self._stopping_done.set()

    @staticmethod
    def _call_executor_shutdown(
        executor: Any,
        *,
        wait: bool,
        cancel_futures: bool,
    ) -> None:
        try:
            executor.shutdown(wait=wait, cancel_futures=cancel_futures)
        except TypeError:
            executor.shutdown(wait=wait)

    @staticmethod
    def _executor_processes(executor: Any) -> list[Any]:
        processes = getattr(executor, "_processes", None)
        if isinstance(processes, dict):
            return [process for process in processes.values() if process is not None]
        if isinstance(processes, (list, tuple, set)):
            return [process for process in processes if process is not None]
        return []

    @staticmethod
    def _terminate_processes(processes: list[Any]) -> None:
        for process in processes:
            is_alive = getattr(process, "is_alive", None)
            if callable(is_alive) and not is_alive():
                continue
            terminate = getattr(process, "terminate", None)
            if callable(terminate):
                try:
                    terminate()
                except Exception:
                    logger.exception("Failed to terminate shared worker process")

        deadline = time.monotonic() + 0.25
        for process in processes:
            join = getattr(process, "join", None)
            if not callable(join):
                continue
            remaining = max(0.0, deadline - time.monotonic())
            try:
                join(timeout=remaining)
            except Exception:
                logger.exception("Failed while joining terminated shared worker process")

        for process in processes:
            is_alive = getattr(process, "is_alive", None)
            if callable(is_alive) and not is_alive():
                continue
            kill = getattr(process, "kill", None)
            if callable(kill):
                try:
                    kill()
                except Exception:
                    logger.exception("Failed to kill shared worker process")

    @classmethod
    def _shutdown_executor_bounded(
        cls,
        executor: Any,
        *,
        timeout_seconds: Optional[float],
        cancel_futures: bool,
    ) -> bool:
        worker_processes = cls._executor_processes(executor)
        if timeout_seconds is not None and timeout_seconds <= 0:
            cls._call_executor_shutdown(
                executor,
                wait=False,
                cancel_futures=cancel_futures,
            )
            cls._terminate_processes(worker_processes)
            return False

        shutdown_finished = threading.Event()

        def _shutdown() -> None:
            try:
                cls._call_executor_shutdown(
                    executor,
                    wait=True,
                    cancel_futures=cancel_futures,
                )
            except Exception:
                logger.exception("Shared worker executor shutdown failed")
            finally:
                shutdown_finished.set()

        shutdown_thread = threading.Thread(
            target=_shutdown,
            name="shared-worker-shutdown",
            daemon=True,
        )
        shutdown_thread.start()
        shutdown_thread.join(timeout=timeout_seconds)
        if not shutdown_thread.is_alive():
            return True

        logger.warning(
            "Shared worker executor shutdown exceeded %.2fs; terminating workers",
            timeout_seconds,
        )
        cls._terminate_processes(worker_processes)
        shutdown_thread.join(timeout=0.25)
        return not shutdown_thread.is_alive()

    def _is_executor_healthy_locked(self, timeout_seconds: float = 1.0) -> bool:
        if self._state.executor is None:
            return False
        try:
            future = self._state.executor.submit(_health_ping)
            return bool(future.result(timeout=timeout_seconds))
        except Exception:
            self._health_failures += 1
            return False

    def _wait_for_stopping_executor(self) -> None:
        """Wait until a previously cancelled executor has fully quiesced."""
        while True:
            with self._lock:
                stopping = self._stopping_executor
                done = self._stopping_done
            if stopping is None:
                return
            done.wait()

    def _clear_current_job_locked(self) -> None:
        self._current_total = 0
        self._current_submitted.clear()
        self._current_completed.clear()
        self._current_failed.clear()
        self._current_chapter_titles.clear()

    def _finish_thread_executor_shutdown(
        self,
        executor: Any,
        completion_event: threading.Event | None = None,
        failure_holder: list[BaseException] | None = None,
        cancel_futures: bool = True,
    ) -> None:
        """Shutdown a thread executor and release its stopping barrier."""
        try:
            self._call_executor_shutdown(
                executor,
                wait=True,
                cancel_futures=cancel_futures,
            )
        except Exception as exc:
            # Keep the barrier set: without confirmation from shutdown(), a
            # successor must not start while the old worker may still mutate
            # conversion artifacts.
            logger.exception("Cancelled thread worker executor failed to shut down")
            if failure_holder is not None:
                failure_holder.append(exc)
            return
        finally:
            if completion_event is not None:
                completion_event.set()

        with self._lock:
            if self._stopping_executor is executor:
                self._stopping_executor = None
                self._stopping_done.set()

    def _stop_executor_after_cancellation(self, executor: Any) -> None:
        """Stop a cancelled executor without allowing identity supersession."""
        with self._lock:
            if self._state.executor is not executor:
                return
            process_executor = bool(self._state.use_processes)
            self._stopping_executor = executor
            self._stopping_done.clear()
            self._state.executor = None
            self._state.config_fingerprint = None
            self._state.worker_init_config = None
            self._clear_current_job_locked()

        if process_executor:
            self._shutdown_executor_bounded(
                executor,
                timeout_seconds=self._shutdown_timeout_seconds,
                cancel_futures=True,
            )
            with self._lock:
                if self._stopping_executor is executor:
                    self._stopping_executor = None
                    self._stopping_done.set()
            return

        threading.Thread(
            target=self._finish_thread_executor_shutdown,
            args=(executor,),
            name="cancelled-thread-worker-shutdown",
            daemon=True,
        ).start()

    def _reset_current_job_locked(self, chapters: list[Any]) -> None:
        self._current_total = len(chapters)
        self._current_submitted = set()
        self._current_completed = set()
        self._current_failed = set()
        self._current_chapter_titles = {
            idx: str(getattr(chapter, "title", f"Chapter {idx + 1}"))
            for idx, chapter in enumerate(chapters)
        }

    def _current_performance_snapshot_locked(self) -> dict[str, Any]:
        total = max(0, self._current_total)
        completed = len(self._current_completed)
        failed = len(self._current_failed)
        pending_indices = sorted(
            self._current_submitted - self._current_completed - self._current_failed
        )
        active_worker_count = min(max(1, self._state.max_workers), len(pending_indices))
        active_indices = pending_indices[:active_worker_count]
        unfinished = max(0, total - completed - failed)
        queued = max(0, unfinished - active_worker_count)
        return {
            "pipeline_mode": "parallel",
            "active_workers": active_worker_count,
            "max_workers": self._state.max_workers,
            "queued_chapters": queued,
            "running_chapters": len(pending_indices),
            "completed_chapters": completed,
            "failed_chapters": failed,
            "active_chapter_titles": [
                self._current_chapter_titles.get(idx, f"Chapter {idx + 1}")
                for idx in active_indices
            ],
        }

    def _current_performance_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._current_performance_snapshot_locked()

    def _emit_progress(
        self,
        progress_callback: Callable[..., None] | None,
        current: int,
        total: int,
        message: str,
    ) -> None:
        if progress_callback is None:
            return
        performance = self._current_performance_snapshot()
        try:
            progress_callback(current, total, message, performance)
        except TypeError as original_error:
            try:
                progress_callback(current, total, message)
            except TypeError:
                raise original_error

    @staticmethod
    def _is_cancel_requested(cancel_requested: Callable[[], bool] | None) -> bool:
        if cancel_requested is None:
            return False
        return bool(cancel_requested())

    def _abort_parallel_run(
        self,
        *,
        executor: Any,
        submitted: dict[Any, int],
        progress_callback: Callable[..., None] | None,
        completed: int,
        total: int,
    ) -> list[ChapterResult]:
        for future in list(submitted):
            future.cancel()

        # A running future cannot be cancelled.  Stop process workers before
        # returning, or retain a stopping barrier for thread workers until
        # executor.shutdown(wait=True) confirms quiescence.
        self._stop_executor_after_cancellation(executor)
        submitted.clear()
        with self._lock:
            self._cancelled_jobs += 1
            self._clear_current_job_locked()
        self._emit_progress(
            progress_callback,
            completed,
            total,
            "Parallel processing cancelled",
        )
        return []

    @staticmethod
    def _result_has_error(result: ChapterResult) -> bool:
        return bool(result.error)

    @staticmethod
    def _normalize_chapter_result(result: Any) -> ChapterResult:
        if isinstance(result, ChapterResult):
            return result
        if isinstance(result, dict):
            return ChapterResult(**result)
        raise TypeError(f"worker returned unsupported result type: {type(result).__name__}")

    def _start_executor_locked(
        self,
        *,
        config_fingerprint: Any,
        max_workers: int,
        available_vram_gb: float,
        worker_init_config: Optional[dict[str, Any]],
    ) -> None:
        if self._use_processes:
            mp_context = mp.get_context("spawn")
            self._state.executor = ProcessPoolExecutor(
                max_workers=max_workers,
                mp_context=mp_context,
                initializer=_init_worker,
                initargs=(available_vram_gb, max_workers, worker_init_config),
            )
        else:
            self._state.executor = ThreadPoolExecutor(
                max_workers=max_workers,
                initializer=_init_worker,
                initargs=(available_vram_gb, max_workers, worker_init_config),
            )
        self._state.config_fingerprint = config_fingerprint
        self._state.max_workers = max_workers
        self._state.available_vram_gb = available_vram_gb
        self._state.worker_init_config = dict(worker_init_config or {})
        self._state.use_processes = self._use_processes
        self._last_start_ms = round(time.time() * 1000, 2)

    @staticmethod
    def _normalize_startup_status(result: Any) -> WorkerStartupStatus:
        if isinstance(result, WorkerStartupStatus):
            return result
        if isinstance(result, dict):
            return WorkerStartupStatus.from_dict(result)
        return WorkerStartupStatus(
            success=False,
            provider="unknown",
            device=None,
            error_type=type(result).__name__,
            message=f"unexpected startup probe result: {result!r}",
        )

    def _run_startup_probes_locked(self) -> None:
        executor = self._state.executor
        if executor is None:  # pragma: no cover - defensive guard
            raise RuntimeError("Cannot probe workers without an executor")

        probe_count = 1 if not self._state.use_processes else max(
            1, int(self._state.max_workers or 1)
        )
        futures = [
            executor.submit(_worker_startup_probe) for _ in range(probe_count)
        ]
        failures: list[WorkerStartupStatus] = []
        _done, pending = wait(
            futures,
            timeout=self._startup_probe_timeout_seconds,
        )
        runtime_config = (self._state.worker_init_config or {}).get("tts_runtime")
        provider = "unknown"
        device = None
        if isinstance(runtime_config, dict):
            provider = str(runtime_config.get("provider") or provider)
            device = runtime_config.get("device")
        timeout_message = (
            "worker startup probe timed out after "
            f"{self._startup_probe_timeout_seconds:g}s"
        )

        for future in futures:
            if future in pending:
                future.cancel()
                failures.append(
                    WorkerStartupStatus(
                        success=False,
                        provider=provider,
                        device=device,
                        error_type=StartupErrorKind.INFRASTRUCTURE,
                        message=timeout_message,
                        phase="startup_probe",
                    )
                )
                continue
            try:
                status = self._normalize_startup_status(future.result())
            except Exception as exc:
                failures.append(
                    WorkerStartupStatus(
                        success=False,
                        provider="unknown",
                        device=None,
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                )
                continue

            if not status.success:
                failures.append(status)

        if not failures:
            return

        self._startup_failures += 1
        primary = failures[0]
        self._last_startup_error = primary.format_failure()
        executor_to_shutdown = self._state.executor
        self._state.executor = None
        self._state.config_fingerprint = None
        self._shutdown_executor_bounded(
            executor_to_shutdown,
            timeout_seconds=self._shutdown_timeout_seconds,
            cancel_futures=True,
        )
        raise WorkerStartupError(
            self._last_startup_error,
            statuses=failures,
        )

    def ensure_started(
        self,
        config_fingerprint: Any,
        *,
        max_workers: Optional[int] = None,
        available_vram_gb: Optional[float] = None,
        available_device_memory_gb: Optional[float] = None,
        worker_init_config: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        Ensure an executor is running for the provided configuration fingerprint.

        Restarts the pool when fingerprint or worker count changes.
        """
        # Cancellation of a thread-backed run returns before an uncooperative
        # worker necessarily exits.  Do not let a successor overlap that
        # worker's artifact writes; the cancellation path releases this
        # barrier only after executor.shutdown(wait=True) confirms quiescence.
        self._wait_for_stopping_executor()
        requested_workers = max(1, int(max_workers or self._state.max_workers or 1))
        if available_vram_gb is not None and available_device_memory_gb is not None:
            raise ValueError(
                "Pass only one of available_vram_gb or available_device_memory_gb."
            )
        requested_memory_gb = (
            available_device_memory_gb
            if available_device_memory_gb is not None
            else available_vram_gb
        )
        requested_vram = float(
            requested_memory_gb
            if requested_memory_gb is not None
            else self._state.available_vram_gb
        )
        requested_init = dict(worker_init_config or {})

        with self._lock:
            reuse_ready = (
                self._state.executor is not None
                and self._state.config_fingerprint == config_fingerprint
                and self._state.max_workers == requested_workers
                and self._state.available_vram_gb == requested_vram
                and (self._state.worker_init_config or {}) == requested_init
                and self._state.use_processes == self._use_processes
            )
            if reuse_ready:
                if self._is_executor_healthy_locked():
                    self._reuse_hits += 1
                    return

            if self._state.executor is not None:
                self._shutdown_executor_bounded(
                    self._state.executor,
                    timeout_seconds=self._shutdown_timeout_seconds,
                    cancel_futures=True,
                )
                self._state.executor = None
                self._state.config_fingerprint = None
                self._restarts += 1

            self._start_executor_locked(
                config_fingerprint=config_fingerprint,
                max_workers=requested_workers,
                available_vram_gb=requested_vram,
                worker_init_config=requested_init,
            )
            self._run_startup_probes_locked()

    def run_parallel(
        self,
        *,
        chapters: list[Any],
        process_func: Callable[[Any, int, Any], Any],
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        config_fingerprint: Any,
        max_workers: Optional[int] = None,
        available_vram_gb: Optional[float] = None,
        available_device_memory_gb: Optional[float] = None,
        worker_init_config: Optional[dict[str, Any]] = None,
        memory_policy: Optional[Any] = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> list[ChapterResult]:
        """Run one chapter batch while owning the shared executor lifecycle."""
        with self._run_lock:
            return self._run_parallel_impl(
                chapters=chapters,
                process_func=process_func,
                progress_callback=progress_callback,
                config_fingerprint=config_fingerprint,
                max_workers=max_workers,
                available_vram_gb=available_vram_gb,
                available_device_memory_gb=available_device_memory_gb,
                worker_init_config=worker_init_config,
                memory_policy=memory_policy,
                cancel_requested=cancel_requested,
            )

    def _run_parallel_impl(
        self,
        *,
        chapters: list[Any],
        process_func: Callable[[Any, int, Any], Any],
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        config_fingerprint: Any,
        max_workers: Optional[int] = None,
        available_vram_gb: Optional[float] = None,
        available_device_memory_gb: Optional[float] = None,
        worker_init_config: Optional[dict[str, Any]] = None,
        memory_policy: Optional[Any] = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> list[ChapterResult]:
        """Run chapter processing using the shared process pool."""
        self.ensure_started(
            config_fingerprint,
            max_workers=max_workers,
            available_vram_gb=available_vram_gb,
            available_device_memory_gb=available_device_memory_gb,
            worker_init_config=worker_init_config,
        )

        with self._lock:
            executor = self._state.executor
            pool_width = max(1, int(self._state.max_workers or 1))
        if executor is None:  # pragma: no cover - defensive guard
            raise RuntimeError("Shared worker pool is not available")

        total = len(chapters)
        results: list[Optional[ChapterResult]] = [None] * total
        submitted: dict[Any, int] = {}
        with self._lock:
            self._reset_current_job_locked(chapters)

        next_idx = 0
        completed = 0
        max_in_flight = max(1, min(total or 1, pool_width))

        def submit_next_available() -> bool:
            nonlocal completed, next_idx

            if self._is_cancel_requested(cancel_requested):
                return False

            if memory_policy is not None:
                should_submit_more = getattr(memory_policy, "should_submit_more", None)
                if callable(should_submit_more) and not should_submit_more(
                    len(submitted)
                ):
                    return False

            while next_idx < total:
                idx = next_idx
                chapter = chapters[idx]
                next_idx += 1
                try:
                    future = executor.submit(process_func, chapter, idx, None)
                except Exception as exc:
                    results[idx] = self._chapter_error_result(chapter, idx, str(exc))
                    completed += 1
                    with self._lock:
                        self._current_failed.add(idx)
                    self._emit_progress(
                        progress_callback,
                        completed,
                        total,
                        f"Failed to queue chapter {idx + 1}/{total}",
                    )
                    continue

                submitted[future] = idx
                with self._lock:
                    self._current_submitted.add(idx)
                self._emit_progress(
                    progress_callback,
                    idx + 1,
                    total,
                    f"Queued chapter {idx + 1}/{total}",
                )
                return True

            return False

        while len(submitted) < max_in_flight and submit_next_available():
            pass

        while submitted:
            if self._is_cancel_requested(cancel_requested):
                return self._abort_parallel_run(
                    executor=executor,
                    submitted=submitted,
                    progress_callback=progress_callback,
                    completed=completed,
                    total=total,
                )

            done, _pending = wait(
                tuple(submitted),
                timeout=_WAIT_POLL_TIMEOUT_SECONDS,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                if self._is_cancel_requested(cancel_requested):
                    return self._abort_parallel_run(
                        executor=executor,
                        submitted=submitted,
                        progress_callback=progress_callback,
                        completed=completed,
                        total=total,
                    )
                continue

            for future in done:
                idx = submitted.pop(future)
                try:
                    results[idx] = self._normalize_chapter_result(future.result())
                except Exception as exc:
                    results[idx] = self._chapter_error_result(chapters[idx], idx, str(exc))

                completed += 1
                with self._lock:
                    if self._result_has_error(results[idx]):
                        self._current_failed.add(idx)
                    else:
                        self._current_completed.add(idx)
                self._emit_progress(
                    progress_callback,
                    completed,
                    total,
                    f"Completed {completed}/{total} chapters",
                )

            while len(submitted) < max_in_flight and submit_next_available():
                pass

        self._jobs_run += 1
        self._chapters_run += total
        for idx, result in enumerate(results):
            if result is None:
                results[idx] = self._chapter_error_result(
                    chapters[idx],
                    idx,
                    "worker returned no result",
                )

        return [result for result in results if result is not None]

    @staticmethod
    def _chapter_error_result(chapter: Any, idx: int, error: str) -> ChapterResult:
        chapter_number = getattr(chapter, "number", idx + 1)
        chapter_title = getattr(chapter, "title", f"Chapter {idx + 1}")
        return ChapterResult(
            chapter_number=chapter_number,
            chapter_title=chapter_title,
            error=error,
        )

    def healthcheck(self, timeout_seconds: float = 2.0) -> bool:
        """Return whether the current worker pool can execute a trivial task."""
        with self._lock:
            executor = self._state.executor
        if executor is None:
            return False

        try:
            future = executor.submit(_health_ping)
            return bool(future.result(timeout=timeout_seconds))
        except Exception:
            self._health_failures += 1
            return False

    def shutdown(
        self,
        *,
        timeout_seconds: Optional[float] = None,
        cancel_futures: bool = True,
    ) -> bool:
        """Shutdown the shared pool and reset lifecycle state with a bounded wait."""
        # A cancelled thread executor may be outside ``_state`` while its
        # worker is still unwinding.  Respect that stopping barrier instead of
        # reporting success and allowing a later ensure_started() to overlap
        # it.  A zero timeout remains a non-blocking probe.
        with self._lock:
            stopping_done = self._stopping_done if self._stopping_executor is not None else None
        if stopping_done is not None:
            timeout = (
                self._shutdown_timeout_seconds
                if timeout_seconds is None
                else max(0.0, float(timeout_seconds))
            )
            return stopping_done.wait(timeout=timeout)

        with self._lock:
            executor = self._state.executor
            use_processes = self._state.use_processes
            if executor is not None and not use_processes:
                # Thread workers cannot be force-terminated after a bounded
                # wait. Keep a barrier until the asynchronous wait=True
                # shutdown confirms that the executor has quiesced.
                self._stopping_executor = executor
                self._stopping_done.clear()
            self._state = _PoolState(
                executor=None,
                config_fingerprint=None,
                max_workers=self._state.max_workers or 1,
                available_vram_gb=self._state.available_vram_gb,
                worker_init_config=None,
                use_processes=self._state.use_processes,
            )
        if executor is None:
            return True

        timeout = (
            self._shutdown_timeout_seconds
            if timeout_seconds is None
            else max(0.0, float(timeout_seconds))
        )
        if not use_processes:
            completion_event = threading.Event()
            failures: list[BaseException] = []
            threading.Thread(
                target=self._finish_thread_executor_shutdown,
                args=(executor, completion_event, failures, cancel_futures),
                name="shared-worker-shutdown",
                daemon=True,
            ).start()
            completed = completion_event.wait(timeout=timeout)
            return completed and not failures

        return self._shutdown_executor_bounded(
            executor,
            timeout_seconds=timeout,
            cancel_futures=cancel_futures,
        )

    def snapshot(self) -> dict[str, Any]:
        """Expose lifecycle counters for observability."""
        with self._lock:
            running = self._state.executor is not None
            workers = self._state.max_workers
            fingerprint = self._state.config_fingerprint
            current_performance = self._current_performance_snapshot_locked()
        return {
            "running": running,
            "max_workers": workers,
            "fingerprint": fingerprint,
            "reuse_hits": self._reuse_hits,
            "restarts": self._restarts,
            "health_failures": self._health_failures,
            "jobs_run": self._jobs_run,
            "chapters_run": self._chapters_run,
            "last_start_ms": self._last_start_ms,
            "startup_failures": self._startup_failures,
            "last_startup_error": self._last_startup_error,
            "cancelled_jobs": self._cancelled_jobs,
            **current_performance,
        }
