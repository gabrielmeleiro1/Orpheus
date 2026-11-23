"""Shared process-pool manager for cross-conversion reuse."""

from __future__ import annotations

import multiprocessing as mp
import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from typing import Any, Callable, Optional

from .models import ChapterResult
from .pool_results import (
    _chapter_error_result,
    _normalize_chapter_result,
    _normalize_startup_status,
    _result_has_error,
)
from .pool_shutdown import (
    _call_executor_shutdown,
    _executor_processes,
    _shutdown_executor_bounded,
    _terminate_processes,
)
from .pool_state import _PoolState
from .pool_workers import _health_ping, _worker_startup_probe
from .worker_errors import StartupErrorKind, WorkerStartupError, WorkerStartupStatus
from .worker_runtime import _init_worker

logger = logging.getLogger(__name__)

_WAIT_POLL_TIMEOUT_SECONDS = 0.25
_STARTUP_BARRIER_GRACE_SECONDS = 5.0

__all__ = [
    "SharedWorkerPoolManager",
    "_PoolState",
    "_call_executor_shutdown",
    "_chapter_error_result",
    "_executor_processes",
    "_health_ping",
    "_normalize_chapter_result",
    "_normalize_startup_status",
    "_result_has_error",
    "_shutdown_executor_bounded",
    "_terminate_processes",
    "_worker_startup_probe",
]


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
        idle_shutdown_seconds: float = 120.0,
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
        self._limiter_generations: dict[Any, tuple[Any, Any]] = {}
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
        # Production always uses processes; tests switch to threads as a seam.
        # Thread workers share one process, so _init_worker's process-global
        # model slots do not model real per-worker state there.
        self._use_processes = True
        self._shutdown_timeout_seconds = max(0.0, float(shutdown_timeout_seconds))
        self._startup_probe_timeout_seconds = max(0.0, float(startup_probe_timeout_seconds))
        self._startup_failures = 0
        self._last_startup_error: Optional[str] = None
        self._cancelled_jobs = 0
        self._stopping_executor: Any | None = None
        self._stopping_done = threading.Event()
        self._stopping_done.set()
        self._stop_requested = threading.Event()
        self._shutdown_waiter_lock = threading.Lock()
        # Idle workers keep their models resident (gigabytes of device memory
        # per worker), so an unused pool is torn down after a bounded wait.
        self._idle_shutdown_seconds = max(0.0, float(idle_shutdown_seconds))
        self._idle_timer: threading.Timer | None = None
        self._idle_lock = threading.Lock()

    def _cancel_idle_shutdown(self) -> None:
        with self._idle_lock:
            timer, self._idle_timer = self._idle_timer, None
        if timer is not None:
            timer.cancel()

    def _schedule_idle_shutdown(self) -> None:
        if self._idle_shutdown_seconds <= 0:
            return
        timer = threading.Timer(self._idle_shutdown_seconds, self._shutdown_if_idle)
        timer.daemon = True
        with self._idle_lock:
            previous, self._idle_timer = self._idle_timer, timer
        if previous is not None:
            previous.cancel()
        timer.start()

    def _shutdown_if_idle(self) -> None:
        # A run holds the run lock from startup to its last result; never tear
        # down under it. The finishing run schedules a fresh idle timer.
        if not self._run_lock.acquire(blocking=False):
            return
        try:
            with self._idle_lock:
                if self._idle_timer is None or self._idle_timer is not threading.current_thread():
                    return
                self._idle_timer = None
            with self._lock:
                running = self._state.executor is not None
            if running:
                logger.info("Shutting down idle worker pool to release model memory")
                self.shutdown()
        finally:
            self._run_lock.release()

    @property
    def cleanup_pending(self) -> bool:
        """Inspect exclusion without waiting for a worker startup lock."""
        return self._stop_requested.is_set() or not self._stopping_done.is_set()

    # Executor teardown primitives live in ``pool_shutdown``; they are bound
    # here so existing ``self._…`` call sites keep resolving unchanged.
    _call_executor_shutdown = staticmethod(_call_executor_shutdown)
    _executor_processes = staticmethod(_executor_processes)
    _terminate_processes = staticmethod(_terminate_processes)

    def _shutdown_executor_bounded(self, executor, *, on_quiesced=None, **kwargs):
        ownership = self._limiter_generations.get(executor)
        if ownership is None:
            return _shutdown_executor_bounded(executor, on_quiesced=on_quiesced, **kwargs)
        processes = _executor_processes(executor)

        def retire():
            if any(process.is_alive() for process in processes):
                logger.error("API worker generation still has live processes")
                return
            limiter, generation = ownership
            limiter.retire_worker_generation(generation)
            self._limiter_generations.pop(executor, None)
            if on_quiesced is not None:
                on_quiesced()

        return _shutdown_executor_bounded(executor, on_quiesced=retire, **kwargs)

    # Worker-boundary normalizers live in ``pool_results``.
    _result_has_error = staticmethod(_result_has_error)
    _normalize_chapter_result = staticmethod(_normalize_chapter_result)
    _normalize_startup_status = staticmethod(_normalize_startup_status)
    _chapter_error_result = staticmethod(_chapter_error_result)

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

    def _release_stopping_barrier(self, executor: Any) -> None:
        """Let a successor start now that ``executor`` has confirmed quiescence."""
        with self._lock:
            if self._stopping_executor is executor:
                self._stopping_executor = None
                if not self._shutdown_waiter_lock.locked():
                    self._stop_requested.clear()
                self._stopping_done.set()

    def _finish_thread_executor_shutdown(self, executor: Any) -> None:
        """Shutdown a cancelled thread executor and release its stopping barrier."""
        try:
            self._call_executor_shutdown(
                executor,
                wait=True,
                cancel_futures=True,
            )
        except Exception:
            # Keep the barrier set: without confirmation from shutdown(), a
            # successor must not start while the old worker may still mutate
            # conversion artifacts.
            logger.exception("Cancelled thread worker executor failed to shut down")
            return

        self._release_stopping_barrier(executor)

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
                on_quiesced=lambda: self._release_stopping_barrier(executor),
            )
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
        progress_callback(current, total, message, self._current_performance_snapshot())

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

    def _start_executor_locked(
        self,
        *,
        config_fingerprint: Any,
        max_workers: int,
        available_vram_gb: float,
        worker_init_config: Optional[dict[str, Any]],
    ) -> None:
        if self._use_processes:
            from modules.tts.api_rate_limit import _RecoverableSemaphore

            init_config = dict(worker_init_config or {})
            limiter = init_config.get("cleaner_api_global_limiter")
            generation = None
            if isinstance(limiter, _RecoverableSemaphore):
                generation = limiter.begin_worker_generation()
                init_config["cleaner_api_global_limiter"] = generation
            mp_context = mp.get_context("spawn")
            # A barrier broken by its timeout lets a ready worker report
            # success before its siblings answer, so the parent's deadline
            # must always expire first.
            startup_barrier = mp_context.Barrier(
                max_workers,
                timeout=self._startup_timeout_seconds(max_workers)
                + _STARTUP_BARRIER_GRACE_SECONDS,
            )
            try:
                self._state.executor = ProcessPoolExecutor(
                    max_workers=max_workers,
                    mp_context=mp_context,
                    initializer=_init_worker,
                    initargs=(available_vram_gb, max_workers, init_config, startup_barrier),
                )
            except Exception:
                if generation is not None:
                    limiter.retire_worker_generation(generation)
                raise
            if generation is not None:
                self._limiter_generations[self._state.executor] = (limiter, generation)
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

    def _startup_timeout_seconds(self, worker_count: int) -> float:
        # Workers load their models at the same time and contend for one disk,
        # memory pool and accelerator, so a cold start of N workers can take up
        # to N times as long as one. The configured timeout is per worker.
        return self._startup_probe_timeout_seconds * max(1, worker_count)

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
        startup_timeout = self._startup_timeout_seconds(probe_count)
        deadline = time.monotonic() + startup_timeout
        pending = set(futures)
        while pending:
            if self._stop_requested.is_set():
                self._begin_shutdown_locked(timeout_seconds=0.0, cancel_futures=True)
                raise RuntimeError("Worker startup cancelled")
            remaining = max(0.0, deadline - time.monotonic())
            _done, pending = wait(pending, timeout=min(0.05, remaining))
            if remaining == 0:
                break
        if self._stop_requested.is_set():
            self._begin_shutdown_locked(timeout_seconds=0.0, cancel_futures=True)
            raise RuntimeError("Worker startup cancelled")
        runtime_config = (self._state.worker_init_config or {}).get("tts_runtime")
        provider = "unknown"
        device = None
        if isinstance(runtime_config, dict):
            provider = str(runtime_config.get("provider") or provider)
            device = runtime_config.get("device")
        timeout_message = (
            f"worker startup probe timed out after {startup_timeout:g}s"
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
        raise WorkerStartupError(
            self._last_startup_error,
            statuses=failures,
        )

    def ensure_started(self, config_fingerprint: Any, **kwargs: Any) -> None:
        """
        Ensure an executor is running for the provided configuration fingerprint.

        Restarts the pool when fingerprint or worker count changes. A started
        pool that then sits unused is shut down after the idle timeout.
        """
        # Holding the run lock keeps a restart from replacing the executor
        # under an active run_parallel.
        with self._run_lock:
            self._cancel_idle_shutdown()
            self._ensure_started_impl(config_fingerprint, **kwargs)
            self._schedule_idle_shutdown()

    def _ensure_started_impl(
        self,
        config_fingerprint: Any,
        *,
        max_workers: Optional[int] = None,
        available_vram_gb: Optional[float] = None,
        available_device_memory_gb: Optional[float] = None,
        worker_init_config: Optional[dict[str, Any]] = None,
    ) -> None:
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

        while True:
            with self._lock:
                if self._stop_requested.is_set():
                    raise RuntimeError("Worker pool cleanup is still in progress")
                reuse_ready = (
                    self._state.executor is not None
                    and self._state.config_fingerprint == config_fingerprint
                    and self._state.max_workers == requested_workers
                    and self._state.available_vram_gb == requested_vram
                    and (self._state.worker_init_config or {}) == requested_init
                    and self._state.use_processes == self._use_processes
                )
                if reuse_ready and self._is_executor_healthy_locked():
                    self._reuse_hits += 1
                    return

                if self._state.executor is not None:
                    retiring_executor, retirement_done = self._detach_executor_locked()
                    self._restarts += 1
                else:
                    self._start_executor_locked(
                        config_fingerprint=config_fingerprint,
                        max_workers=requested_workers,
                        available_vram_gb=requested_vram,
                        worker_init_config=requested_init,
                    )
                    started_executor = self._state.executor
                    try:
                        self._run_startup_probes_locked()
                    except Exception as exc:
                        startup_error = exc
                        failed_executor = None
                        # A cancelled startup has already detached its executor.
                        if self._state.executor is started_executor:
                            failed_executor, _done = self._detach_executor_locked()
                        break
                    else:
                        return

            # Replacement must observe the same quiescence barrier as cancellation.
            # Wait outside the lifecycle lock so the teardown callback can release it.
            self._shutdown_executor_bounded(
                retiring_executor,
                timeout_seconds=self._shutdown_timeout_seconds,
                cancel_futures=True,
                on_quiesced=lambda executor=retiring_executor: self._release_stopping_barrier(executor),
            )
            if not retirement_done.is_set():
                raise RuntimeError("Worker pool has not stopped; cannot replace worker runtime")

        # Release the lifecycle lock so cleanup can confirm quiescence before
        # the caller decides whether to load sequential models.
        if failed_executor is not None:
            self._shutdown_executor_bounded(
                failed_executor,
                timeout_seconds=self._shutdown_timeout_seconds,
                cancel_futures=True,
                on_quiesced=lambda: self._release_stopping_barrier(failed_executor),
            )
        raise startup_error

    def run_parallel(
        self,
        *,
        chapters: list[Any],
        process_func: Callable[[Any, int], Any],
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
            try:
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
            finally:
                # The idle clock starts when the run releases the pool.
                self._schedule_idle_shutdown()

    def _run_parallel_impl(
        self,
        *,
        chapters: list[Any],
        process_func: Callable[[Any, int], Any],
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
        # The run owns the pool until it returns; run_parallel restarts the
        # idle clock afterwards.
        self._cancel_idle_shutdown()
        self._ensure_started_impl(
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
                    len(submitted), workers_resident=True
                ):
                    return False

            while next_idx < total:
                idx = next_idx
                chapter = chapters[idx]
                next_idx += 1
                try:
                    future = executor.submit(process_func, chapter, idx)
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

    def _detach_executor_locked(self) -> tuple[Any | None, threading.Event]:
        if self._stopping_executor is not None:
            return None, self._stopping_done
        executor = self._state.executor
        self._state = _PoolState(
            executor=None,
            config_fingerprint=None,
            max_workers=self._state.max_workers or 1,
            available_vram_gb=self._state.available_vram_gb,
            worker_init_config=None,
            use_processes=self._state.use_processes,
        )
        if executor is None:
            if not self._shutdown_waiter_lock.locked():
                self._stop_requested.clear()
            return None, self._stopping_done
        self._stop_requested.set()
        self._stopping_executor = executor
        self._stopping_done = threading.Event()
        return executor, self._stopping_done

    def _begin_shutdown_locked(self, *, timeout_seconds: float, cancel_futures: bool) -> threading.Event:
        executor, done = self._detach_executor_locked()
        if executor is None:
            return done

        def finish():
            self._shutdown_executor_bounded(
                executor,
                timeout_seconds=timeout_seconds,
                cancel_futures=cancel_futures,
                on_quiesced=lambda: self._release_stopping_barrier(executor),
            )

        threading.Thread(target=finish, name="worker-pool-shutdown", daemon=True).start()
        return done

    def shutdown(
        self,
        *,
        timeout_seconds: Optional[float] = None,
        cancel_futures: bool = True,
    ) -> bool:
        """Request teardown, keeping exclusion until workers confirm quiescence."""
        self._cancel_idle_shutdown()
        timeout = (
            self._shutdown_timeout_seconds
            if timeout_seconds is None
            else max(0.0, float(timeout_seconds))
        )
        deadline = time.monotonic() + timeout
        owns_waiter = self._shutdown_waiter_lock.acquire(blocking=False)
        self._stop_requested.set()
        if not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            # Startup observes the request; this also covers a lock held by
            # another lifecycle operation that has no startup probe to interrupt.
            def finish_when_available():
                with self._lock:
                    try:
                        self._begin_shutdown_locked(timeout_seconds=0.0, cancel_futures=cancel_futures)
                    finally:
                        self._shutdown_waiter_lock.release()
                        if self._stopping_executor is None:
                            self._stop_requested.clear()

            if not owns_waiter:
                # Every waiter-lock holder clears the request after releasing
                # it. If that holder finished after our set() above, no one
                # would clear it, so become the waiter once the lock is free.
                owns_waiter = self._shutdown_waiter_lock.acquire(blocking=False)
            if owns_waiter:
                threading.Thread(
                    target=finish_when_available, name="pending-worker-pool-shutdown", daemon=True,
                ).start()
            return False
        try:
            started_teardown = self._state.executor is not None
            done = self._begin_shutdown_locked(
                timeout_seconds=max(0.0, deadline - time.monotonic()),
                cancel_futures=cancel_futures,
            )
        finally:
            if owns_waiter:
                self._shutdown_waiter_lock.release()
                if self._stopping_executor is None:
                    self._stop_requested.clear()
            self._lock.release()
        if timeout == 0 and started_teardown:
            return False
        return done.wait(timeout=max(0.0, deadline - time.monotonic()))

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
