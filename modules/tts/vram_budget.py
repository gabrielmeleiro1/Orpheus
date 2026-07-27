"""System-memory sampling primitives for unified-memory budgeting.

Host-level probes only: nothing here touches MLX or monitor state, so the
functions stay pure and independently testable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

GIB = 1024 * 1024 * 1024


@dataclass(frozen=True)
class SystemMemoryBudget:
    """Best-effort system and MLX memory sample for unified-memory budgeting."""

    total_bytes: int
    available_bytes: int
    mlx_active_bytes: int = 0
    mlx_cache_bytes: int = 0
    mlx_peak_bytes: int = 0


def _sample_virtual_memory() -> tuple[int, int] | None:
    try:
        import psutil

        memory = psutil.virtual_memory()
        return max(0, int(memory.total)), max(0, int(memory.available))
    except Exception:
        return None


def _sample_sysconf_memory() -> tuple[int, int] | None:
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total_pages = int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None

    if page_size <= 0 or total_pages <= 0:
        return None

    total_bytes = page_size * total_pages
    try:
        available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        available_bytes = page_size * max(0, available_pages)
    except (AttributeError, OSError, TypeError, ValueError):
        available_bytes = int(total_bytes * 0.70)
    return total_bytes, available_bytes


def _coerce_memory_budget(sample: Any) -> SystemMemoryBudget:
    return SystemMemoryBudget(
        total_bytes=max(0, int(getattr(sample, "total_bytes", 0))),
        available_bytes=max(0, int(getattr(sample, "available_bytes", 0))),
        mlx_active_bytes=max(0, int(getattr(sample, "mlx_active_bytes", 0))),
        mlx_cache_bytes=max(0, int(getattr(sample, "mlx_cache_bytes", 0))),
        mlx_peak_bytes=max(0, int(getattr(sample, "mlx_peak_bytes", 0))),
    )


def _budgeted_mlx_bytes(sample: SystemMemoryBudget, reserved_bytes: int) -> int:
    mlx_committed_bytes = sample.mlx_active_bytes + sample.mlx_cache_bytes
    growth_headroom_bytes = max(0, sample.available_bytes - reserved_bytes)
    return max(1, mlx_committed_bytes + growth_headroom_bytes)
