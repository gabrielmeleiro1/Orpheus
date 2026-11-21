"""Cross-process API cleaner rate limiter primitives."""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import multiprocessing.synchronize as mp_synchronize
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional


# How often a queued acquire on a raw process semaphore checks for abandonment.
_ABANDON_POLL_SECONDS = 0.05


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
        self._wait_count = 0
        self._wait_ms_total = 0.0

    def __reduce__(self):
        return _restore_worker_semaphore, (self._semaphore,)

    def acquire(
        self,
        block: bool = True,
        timeout: Optional[float] = None,
        *,
        abandon: Optional[threading.Event] = None,
    ) -> bool:
        """Take a permit; a set ``abandon`` event ends a blocked wait with False."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        waited_since = None
        with self._condition:
            while True:
                if self._semaphore.acquire(False):
                    self._parent_permits += 1
                    if waited_since is not None:
                        self._wait_count += 1
                        self._wait_ms_total += (time.monotonic() - waited_since) * 1000
                    return True
                remaining = None if deadline is None else deadline - time.monotonic()
                if not block or (remaining is not None and remaining <= 0):
                    return False
                if abandon is not None and abandon.is_set():
                    return False
                if waited_since is None:
                    waited_since = time.monotonic()
                # Worker releases cannot notify a parent-thread condition.
                self._condition.wait(0.01 if remaining is None else min(0.01, remaining))

    def release(self) -> None:
        with self._condition:
            if self._parent_permits <= 0:
                raise ValueError("No parent API limiter permit to release")
            self._semaphore.release()
            self._parent_permits -= 1
            self._condition.notify()

    def wait_stats(self) -> tuple[int, float]:
        """Parent requests that queued for a permit, and their total wait."""
        with self._condition:
            return self._wait_count, self._wait_ms_total

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


def _acquire_process_semaphore(
    semaphore: Any,
    timeout: Optional[float],
    abandon: threading.Event,
) -> bool:
    """Block on a raw process semaphore in slices so ``abandon`` can end the wait."""
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    while True:
        wait = _ABANDON_POLL_SECONDS
        if deadline is not None:
            wait = max(0.0, min(wait, deadline - time.monotonic()))
        if semaphore.acquire(True, wait):
            return True
        if abandon.is_set() or (deadline is not None and time.monotonic() >= deadline):
            return False


class GlobalAPILimiter:
    """
    Cross-process limiter backed by a multiprocessing semaphore.

    The exposed `semaphore` proxy is intended to be passed through pipeline
    config so worker processes share one global concurrency ceiling.
    """

    def __init__(self, max_concurrency: int):
        self.max_concurrency = max(1, int(max_concurrency))
        # Process workers receive the raw semaphore, so wait stats cover only
        # requests made from this process.
        self._semaphore = _RecoverableSemaphore(self.max_concurrency)

    @property
    def semaphore(self):
        return self._semaphore

    def snapshot(self) -> dict[str, float | int]:
        wait_count, wait_ms_total = self._semaphore.wait_stats()
        return {
            "max_concurrency": self.max_concurrency,
            "wait_count": wait_count,
            "wait_ms_total": round(wait_ms_total, 2),
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

    # Set on cancellation so a wait queued on a full limiter gives up promptly
    # instead of holding the batch (and its thread) until a permit frees.
    abandon = threading.Event()

    def _acquire() -> bool:
        if isinstance(limiter, _RecoverableSemaphore):
            return limiter.acquire(True, timeout, abandon=abandon)
        if isinstance(limiter, mp_synchronize.SemLock):
            return _acquire_process_semaphore(limiter, timeout, abandon)
        if timeout is None:
            return _acquire_succeeded(limiter.acquire())
        return _acquire_succeeded(limiter.acquire(True, timeout))

    acquire_task = asyncio.create_task(asyncio.to_thread(_acquire))
    try:
        acquired = await asyncio.shield(acquire_task)
    except asyncio.CancelledError:
        abandon.set()
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
        # Finish the release before cancellation reaches the caller, so a
        # cancelled request never returns while still holding its permit.
        release_task = asyncio.create_task(asyncio.to_thread(limiter.release))
        interrupted = False
        while not release_task.done():
            try:
                await asyncio.shield(release_task)
            except asyncio.CancelledError:
                interrupted = True
        release_task.result()
        if interrupted:
            raise asyncio.CancelledError
