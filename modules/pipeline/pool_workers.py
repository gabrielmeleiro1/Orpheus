"""Worker-side callables submitted into the shared pool.

These live at module scope so ``ProcessPoolExecutor`` can pickle them by
reference; they run inside worker processes, not in the manager.
"""

from __future__ import annotations

from .worker_errors import WorkerStartupStatus
from .worker_runtime import get_worker_startup_status


def _health_ping() -> bool:
    """Top-level no-op used for worker liveness checks."""
    return True


def _worker_startup_probe() -> WorkerStartupStatus:
    """Validate that the worker process completed model initialization."""
    return get_worker_startup_status()
