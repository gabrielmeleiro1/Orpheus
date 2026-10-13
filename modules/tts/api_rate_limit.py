"""Cross-process API cleaner rate limiter primitives."""

from __future__ import annotations

import asyncio
import inspect
import multiprocessing as mp
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional


def _invoke_acquire(acquire: Any, timeout: Optional[float]) -> Any:
    """Call an acquire method using only a signature-compatible argument list."""
    try:
        signature = inspect.signature(acquire)
    except (TypeError, ValueError):
        # Built-in acquire methods generally expose the standard semaphore
        # signature but cannot always be introspected. Invoke once so an
        # internal TypeError is never mistaken for a signature mismatch.
        if timeout is None:
            return acquire()
        return acquire(True, timeout)

    candidates = [(), (True,)] if timeout is None else [(True, timeout), (timeout,)]
    for args in candidates:
        try:
            signature.bind(*args)
        except TypeError:
            continue
        return acquire(*args)

    # Preserve the native error for an unsupported signature, without retrying
    # after the method body has run.
    if timeout is None:
        return acquire()
    return acquire(True, timeout)


def _acquire_succeeded(result: Any) -> bool:
    # Some limiter wrappers return ``None`` on success, while semaphore
    # implementations return a truthy/falsey boolean.
    return result is None or bool(result)


async def _await_task_to_completion(task: asyncio.Task[Any]) -> Any:
    """Await a shielded task despite repeated cancellation of this task."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


def _restore_worker_semaphore(semaphore):
    return semaphore


class _RecoverableSemaphore:
    """Parent-owned capacity with a fixed semaphore snapshot for each worker pool.

    Only the parent counts its surviving requests. Workers receive the raw
    semaphore at spawn and never participate in the parent's bookkeeping lock.
    """

    def __init__(self, capacity: int):
        self._capacity = capacity
        self._semaphore = mp.get_context("spawn").BoundedSemaphore(capacity)
        self._condition = threading.Condition()
        self._parent_permits = 0
        self._worker_generation = None

    def __reduce__(self):
        return _restore_worker_semaphore, (self._semaphore,)

    def acquire(self, block: bool = True, timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                if self._semaphore.acquire(False):
                    self._parent_permits += 1
                    return True
                remaining = None if deadline is None else deadline - time.monotonic()
                if not block or (remaining is not None and remaining <= 0):
                    return False
                # Worker releases cannot notify a parent-thread condition.
                self._condition.wait(0.01 if remaining is None else min(0.01, remaining))

    def release(self) -> None:
        with self._condition:
            if self._parent_permits <= 0:
                raise ValueError("No parent API limiter permit to release")
            self._semaphore.release()
            self._parent_permits -= 1
            self._condition.notify()

    def begin_worker_generation(self):
        with self._condition:
            if self._worker_generation is not None:
                raise RuntimeError("API worker generation has not quiesced")
            self._worker_generation = self._semaphore
            return self._worker_generation

    def retire_worker_generation(self, generation) -> None:
        """Recover dead workers' permits only after their executor has quiesced."""
        with self._condition:
            if self._worker_generation is not generation:
                return
            replacement = mp.get_context("spawn").BoundedSemaphore(self._capacity)
            for _ in range(self._parent_permits):
                replacement.acquire(False)
            self._semaphore = replacement
            self._worker_generation = None
            self._condition.notify_all()


class GlobalAPILimiter:
    """
    Cross-process limiter backed by a multiprocessing semaphore.

    The exposed `semaphore` proxy is intended to be passed through pipeline
    config so worker processes share one global concurrency ceiling.
    """

    def __init__(self, max_concurrency: int):
        self.max_concurrency = max(1, int(max_concurrency))
        self._semaphore = None
        self._stats_lock = threading.Lock()
        self._wait_count = 0
        self._wait_ms_total = 0.0

        # Keep construction lightweight while preserving process sharing for
        # process-backed chapter workers. Stats remain local to this process.
        self._semaphore = _RecoverableSemaphore(self.max_concurrency)

    @property
    def semaphore(self):
        return self._semaphore

    def acquire(self, timeout: Optional[float] = None) -> None:
        if self._semaphore is None:
            return

        start = time.perf_counter()
        acquired = _invoke_acquire(self._semaphore.acquire, timeout)
        if not _acquire_succeeded(acquired):
            raise TimeoutError("Global API limiter acquire timed out")

        wait_ms = (time.perf_counter() - start) * 1000
        if wait_ms <= 0:
            return

        with self._stats_lock:
            self._wait_count += 1
            self._wait_ms_total += wait_ms

    def release(self) -> None:
        if self._semaphore is None:
            return
        self._semaphore.release()

    def snapshot(self) -> dict[str, float | int]:
        with self._stats_lock:
            return {
                "max_concurrency": self.max_concurrency,
                "wait_count": self._wait_count,
                "wait_ms_total": round(self._wait_ms_total, 2),
            }

    def shutdown(self) -> None:
        """No-op: the semaphore is process-shared and owns no manager process."""


@asynccontextmanager
async def acquire_global_limiter_async(
    limiter: Optional[Any],
    timeout: Optional[float] = None,
) -> AsyncIterator[None]:
    """
    Acquire/release a global limiter proxy from async code.

    `limiter` may be:
    - None: no-op
    - a multiprocessing semaphore proxy (acquire/release)
    - any object implementing acquire/release
    """
    if limiter is None:
        yield
        return

    def _acquire() -> bool:
        return _acquire_succeeded(_invoke_acquire(limiter.acquire, timeout))

    acquire_task = asyncio.create_task(asyncio.to_thread(_acquire))
    try:
        acquired = await asyncio.shield(acquire_task)
    except asyncio.CancelledError:
        # Keep waiting for the worker thread: it may acquire after this task is
        # cancelled, and that permit must be returned before cancellation is
        # propagated to the caller.
        try:
            acquired = await _await_task_to_completion(acquire_task)
        except BaseException:
            acquired = False
        if acquired:
            release_task = asyncio.create_task(asyncio.to_thread(limiter.release))
            try:
                await _await_task_to_completion(release_task)
            except BaseException:
                pass
        raise

    if not acquired:
        raise TimeoutError("Global API limiter acquire timed out")

    try:
        yield
    finally:
        await asyncio.to_thread(limiter.release)
