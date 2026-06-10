"""Text cleaner runtime errors for local MLX/CUDA and API backends."""

from __future__ import annotations

MEMORY_PRESSURE_ERROR_MARKERS = (
    "memory",
    "vram",
    "oom",
    "metal",
    "out of memory",
)


def is_memory_pressure_error(message: str) -> bool:
    """Return True when an error indicates memory pressure or device OOM."""
    lowered = str(message or "").lower()
    return any(marker in lowered for marker in MEMORY_PRESSURE_ERROR_MARKERS)


class CleanerRuntimeUnavailable(RuntimeError):
    """Requested cleaner runtime is not registered or installed."""


class CleanerRuntimeMemoryError(RuntimeError):
    """Cleaner runtime failed due to memory pressure."""
