"""Bounded teardown primitives for shared worker executors.

Pure functions over an executor object: they own no manager state and take no
manager lock, so they are safe to call from either the owning thread or a
detached shutdown thread.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


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


def _executor_processes(executor: Any) -> list[Any]:
    processes = getattr(executor, "_processes", None)
    if isinstance(processes, dict):
        return [process for process in processes.values() if process is not None]
    if isinstance(processes, (list, tuple, set)):
        return [process for process in processes if process is not None]
    return []


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


def _shutdown_executor_bounded(
    executor: Any,
    *,
    timeout_seconds: Optional[float],
    cancel_futures: bool,
    on_quiesced: Optional[Callable[[], None]] = None,
) -> bool:
    """Shut an executor down within ``timeout_seconds``.

    ``on_quiesced`` marks an executor whose workers cannot be force
    terminated: shutdown always runs with ``wait=True``, the callback fires
    only once that call confirms quiescence, and exceeding the timeout
    leaves the worker unwinding instead of killing processes.
    """
    worker_processes = _executor_processes(executor)
    if on_quiesced is None and timeout_seconds is not None and timeout_seconds <= 0:
        _call_executor_shutdown(
            executor,
            wait=False,
            cancel_futures=cancel_futures,
        )
        _terminate_processes(worker_processes)
        return False

    def _shutdown() -> None:
        try:
            _call_executor_shutdown(
                executor,
                wait=True,
                cancel_futures=cancel_futures,
            )
        except Exception:
            logger.exception("Shared worker executor shutdown failed")
            return
        if on_quiesced is not None:
            on_quiesced()

    shutdown_thread = threading.Thread(
        target=_shutdown,
        name="shared-worker-shutdown",
        daemon=True,
    )
    shutdown_thread.start()
    shutdown_thread.join(timeout=timeout_seconds)
    if not shutdown_thread.is_alive():
        return True
    if on_quiesced is not None:
        return False

    logger.warning(
        "Shared worker executor shutdown exceeded %.2fs; terminating workers",
        timeout_seconds,
    )
    _terminate_processes(worker_processes)
    shutdown_thread.join(timeout=0.25)
    return not shutdown_thread.is_alive()
