"""Cross-process API cleaner rate limiter primitives."""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional


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
        self._semaphore = mp.BoundedSemaphore(self.max_concurrency)

    @property
    def semaphore(self):
        return self._semaphore

    def acquire(self, timeout: Optional[float] = None) -> None:
        if self._semaphore is None:
            return

        start = time.perf_counter()
        try:
            acquired = self._semaphore.acquire(True, timeout)
        except TypeError:
            acquired = self._semaphore.acquire(True)
        if not acquired:
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

    try:
        await asyncio.to_thread(limiter.acquire, True, timeout)
    except TypeError:
        if timeout is None:
            await asyncio.to_thread(limiter.acquire)
        else:
            await asyncio.to_thread(limiter.acquire, timeout)
    try:
        yield
    finally:
        await asyncio.to_thread(limiter.release)
