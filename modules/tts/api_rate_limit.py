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


class GlobalAPILimiter:
    """
    Cross-process limiter backed by a multiprocessing semaphore.

    The exposed `semaphore` proxy is intended to be passed through pipeline
    config so worker processes share one global concurrency ceiling.
    """

    def __init__(self, max_concurrency: int):
        self.max_concurrency = max(1, int(max_concurrency))
        self._manager = None
        self._semaphore = None
        self._stats_lock: Any = threading.Lock()
        self._wait_count: Any = 0
        self._wait_ms_total: Any = 0.0

        # Keep construction lightweight while preserving process sharing for
        # process-backed chapter workers. Stats remain local to this process.
        self._semaphore = mp.get_context("spawn").BoundedSemaphore(self.max_concurrency)

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

        if self._manager is None:
            with self._stats_lock:
                self._wait_count = int(self._wait_count) + 1
                self._wait_ms_total = float(self._wait_ms_total) + wait_ms
            return

        with self._stats_lock:
            self._wait_count.set(int(self._wait_count.get()) + 1)
            self._wait_ms_total.set(float(self._wait_ms_total.get()) + wait_ms)

    def release(self) -> None:
        if self._semaphore is None:
            return
        self._semaphore.release()

    def snapshot(self) -> dict[str, float | int]:
        if self._manager is None:
            with self._stats_lock:
                return {
                    "max_concurrency": self.max_concurrency,
                    "wait_count": int(self._wait_count),
                    "wait_ms_total": round(float(self._wait_ms_total), 2),
                }

        with self._stats_lock:
            return {
                "max_concurrency": self.max_concurrency,
                "wait_count": int(self._wait_count.get()),
                "wait_ms_total": round(float(self._wait_ms_total.get()), 2),
            }

    def shutdown(self) -> None:
        if self._manager is not None:
            self._manager.shutdown()


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
