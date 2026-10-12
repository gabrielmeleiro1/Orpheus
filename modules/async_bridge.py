"""Shared sync/async bridge helpers for synchronous runtime code."""

from __future__ import annotations

import asyncio
import threading
from typing import Any


class _LoopThreadRunner:
    """Singleton runner that executes coroutines on a dedicated loop thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def _start_locked(self) -> None:
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            return

        self._ready.clear()

        def _run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._ready.set()
            loop.run_forever()
            loop.close()

        thread = threading.Thread(target=_run_loop, daemon=True, name="async-bridge-loop")
        self._thread = thread
        thread.start()
        self._ready.wait(timeout=2.0)
        if self._loop is None:
            raise RuntimeError("Failed to start async bridge loop thread")

    def run(self, coro: Any) -> Any:
        with self._lock:
            self._start_locked()
            loop = self._loop
        if loop is None:  # pragma: no cover - defensive guard
            raise RuntimeError("Async bridge loop unavailable")

        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()

    def reset_for_tests(self) -> None:
        """Reset loop-thread state for deterministic test assertions."""
        with self._lock:
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None
            self._ready.clear()

        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass

        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)


_RUNNER = _LoopThreadRunner()


def run_coroutine_sync(coro: Any) -> Any:
    """
    Execute an async coroutine from sync code.

    Uses `asyncio.run(...)` when no loop is active in the calling thread and
    a shared dedicated loop thread when a loop is already running.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    return _RUNNER.run(coro)


def _reset_runner_for_tests() -> None:
    _RUNNER.reset_for_tests()
