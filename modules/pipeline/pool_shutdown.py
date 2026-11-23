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

import psutil

logger = logging.getLogger(__name__)


def shutdown_shared_worker_pool(config: Any) -> None:
    """Release worker-owned models before loading models in the parent process."""
    manager = getattr(config, "shared_worker_pool_manager", None)
    shutdown = getattr(manager, "shutdown", None)
    if callable(shutdown) and shutdown() is False:
        raise RuntimeError("Worker pool has not stopped; cannot load sequential models")


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
    descendants: list[Any] = []
    for process in processes:
        pid = getattr(process, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            continue
        try:
            descendants.extend(psutil.Process(pid).children(recursive=True))
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, OSError):
            logger.exception("Failed to inspect shared worker descendants")
    for child in reversed(descendants):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, OSError):
            logger.exception("Failed to terminate shared worker descendant")

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

    _gone, alive = psutil.wait_procs(descendants, timeout=0.25)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, OSError):
            logger.exception("Failed to kill shared worker descendant")
    psutil.wait_procs(alive, timeout=0.25)
    for process in processes:
        join = getattr(process, "join", None)
        if callable(join):
            try:
                join(timeout=0.25)
            except Exception:
                logger.exception("Failed while joining killed shared worker process")


def _is_process_executor(executor: Any) -> bool:
    return hasattr(executor, "_processes")


def _process_is_alive(process: Any) -> bool:
    is_alive = getattr(process, "is_alive", None)
    if not callable(is_alive):
        return True
    try:
        return bool(is_alive())
    except ValueError:
        # A closed multiprocessing.Process handle has already been reaped.
        return False


def _confirm_worker_processes_stopped(executor: Any, known_processes: list[Any]) -> bool:
    """Return True once every worker process the executor started has exited.

    Stragglers left by a failed or incomplete ``shutdown()`` are terminated
    within a bound; any that still survive keep the caller's barrier closed.
    """
    processes = {id(process): process for process in known_processes}
    # Workers spawned after the caller's snapshot (replacements) count too.
    for process in _executor_processes(executor):
        processes.setdefault(id(process), process)
    alive = [process for process in processes.values() if _process_is_alive(process)]
    if not alive:
        return True
    logger.warning("Terminating %d shared worker process(es) left after shutdown", len(alive))
    _terminate_processes(alive)
    survivors = [process for process in alive if _process_is_alive(process)]
    if survivors:
        logger.error(
            "Shared worker processes survived termination: %s",
            ", ".join(str(getattr(process, "pid", "?")) for process in survivors),
        )
        return False
    return True


def _shutdown_executor_bounded(
    executor: Any,
    *,
    timeout_seconds: Optional[float],
    cancel_futures: bool,
    on_quiesced: Optional[Callable[[], None]] = None,
) -> bool:
    """Shut an executor down within ``timeout_seconds``.

    ``on_quiesced`` fires only after ``shutdown(wait=True)`` confirms quiescence.
    Process workers may be force-terminated; thread workers keep unwinding.
    """
    worker_processes = _executor_processes(executor)
    force_immediately = timeout_seconds is not None and timeout_seconds <= 0
    if force_immediately and (worker_processes or on_quiesced is None):
        _call_executor_shutdown(
            executor,
            wait=False,
            cancel_futures=cancel_futures,
        )
        _terminate_processes(worker_processes)
        if on_quiesced is None:
            return False

    shutdown_succeeded = threading.Event()

    def _shutdown() -> None:
        try:
            _call_executor_shutdown(
                executor,
                wait=True,
                cancel_futures=cancel_futures,
            )
        except Exception:
            logger.exception("Shared worker executor shutdown failed")
            # A thread executor's quiescence cannot be observed directly, so
            # its barrier stays closed. Worker processes can be checked.
            if not _is_process_executor(executor):
                return
        if not _confirm_worker_processes_stopped(executor, worker_processes):
            return
        shutdown_succeeded.set()
        if on_quiesced is not None:
            on_quiesced()

    shutdown_thread = threading.Thread(
        target=_shutdown,
        name="shared-worker-shutdown",
        daemon=True,
    )
    shutdown_thread.start()
    shutdown_thread.join(timeout=timeout_seconds)
    if force_immediately:
        return False
    if not shutdown_thread.is_alive():
        return shutdown_succeeded.is_set()
    if on_quiesced is not None and not worker_processes:
        return False

    logger.warning(
        "Shared worker executor shutdown exceeded %.2fs; terminating workers",
        timeout_seconds,
    )
    _terminate_processes(worker_processes)
    shutdown_thread.join(timeout=0.25)
    return not shutdown_thread.is_alive() and shutdown_succeeded.is_set()
