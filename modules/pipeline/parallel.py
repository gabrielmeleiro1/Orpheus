"""
Shared-manager-backed helpers for chapter parallelism.
"""

from __future__ import annotations

import logging
import os
from functools import partial
from functools import lru_cache
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .models import ChapterResult
from .pool_manager import SharedWorkerPoolManager
from .worker_runtime import (
    _init_worker as _init_worker,
    get_worker_cached_models as get_worker_cached_models,
    get_worker_cached_config as get_worker_cached_config,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ParallelConfig",
    "VRAMBudget",
    "ParallelPipelineOrchestrator",
    "get_optimal_worker_count",
    "create_parallel_config",
    "_init_worker",
    "get_worker_cached_models",
    "get_worker_cached_config",
]


def _detect_physical_ram_bytes() -> Optional[int]:
    """Best-effort stdlib RAM detection for POSIX hosts."""
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        page_count = int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None

    if page_size <= 0 or page_count <= 0:
        return None
    return page_size * page_count


@lru_cache(maxsize=1)
def _detect_available_ram_gb() -> float:
    """
    Detect usable unified memory budget in GB.

    Uses ~70% of physical RAM to leave headroom for OS and other processes.
    """
    total_bytes = _detect_physical_ram_bytes()
    if total_bytes is not None:
        total_gb = total_bytes / (1024**3)
        return max(2.0, total_gb * 0.70)

    try:
        import psutil

        total_gb = psutil.virtual_memory().total / (1024**3)
        return max(2.0, total_gb * 0.70)
    except Exception:
        return 8.0


@dataclass
class VRAMBudget:
    """Budget model for chapter-parallel worker selection."""

    total_vram_gb: float = field(default_factory=_detect_available_ram_gb)
    reserved_vram_gb: float = 2.0
    tts_model_size_gb: float = 0.5
    cleaner_model_size_gb: float = 1.5
    safety_margin_gb: float = 1.0

    @property
    def total_device_memory_gb(self) -> float:
        """Compatibility-safe device-memory name for the old VRAM budget."""
        return self.total_vram_gb

    @total_device_memory_gb.setter
    def total_device_memory_gb(self, value: float) -> None:
        self.total_vram_gb = value

    @property
    def available_vram_gb(self) -> float:
        """Calculate available VRAM for processing."""
        return self.total_vram_gb - self.reserved_vram_gb - self.safety_margin_gb

    @property
    def available_device_memory_gb(self) -> float:
        """Calculate available device memory for processing."""
        return self.available_vram_gb

    @property
    def max_concurrent_chapters(self) -> int:
        """Calculate max chapters that can run in parallel based on VRAM."""
        per_chapter_cost = self.tts_model_size_gb + self.cleaner_model_size_gb
        max_chapters = int(self.available_vram_gb / per_chapter_cost)
        return max(1, min(max_chapters, 4))


@dataclass
class ParallelConfig:
    """Configuration for the surviving shared-pool parallel path."""

    max_workers: int = 2
    use_processes: bool = True
    vram_budget: VRAMBudget = field(default_factory=VRAMBudget)
    worker_init_config: Optional[dict[str, Any]] = None
    enforce_vram_budget: bool = True

    def __post_init__(self):
        """Validate and adjust configuration based on VRAM budget."""
        if not self.enforce_vram_budget:
            self.max_workers = max(1, int(self.max_workers or 1))
            return
        max_vram_workers = self.vram_budget.max_concurrent_chapters
        if self.max_workers > max_vram_workers:
            logger.warning(
                f"Reducing max_workers from {self.max_workers} to {max_vram_workers} "
                f"due to device memory constraints"
            )
            self.max_workers = max_vram_workers


class ParallelPipelineOrchestrator:
    """
    Thin async facade over ``SharedWorkerPoolManager``.
    """

    def __init__(
        self,
        config: Optional[ParallelConfig] = None,
        *,
        pool_manager: Optional[Any] = None,
        config_fingerprint: Optional[Any] = None,
        worker_init_config: Optional[dict[str, Any]] = None,
        memory_policy: Optional[Any] = None,
    ):
        self.config = config or ParallelConfig()
        self._cancelled = False
        self._pool_manager = pool_manager
        self._owns_pool_manager = pool_manager is None
        self._config_fingerprint = (
            config_fingerprint
            if config_fingerprint is not None
            else (
                id(self),
                self.config.max_workers,
                self.config.use_processes,
            )
        )
        self._worker_init_config = dict(
            worker_init_config
            if worker_init_config is not None
            else (self.config.worker_init_config or {})
        )
        self._memory_policy = memory_policy

    def _get_pool_manager(self) -> SharedWorkerPoolManager:
        if self._pool_manager is None:
            self._pool_manager = SharedWorkerPoolManager(
                max_workers=self.config.max_workers,
                available_vram_gb=self.config.vram_budget.available_device_memory_gb,
            )
        setattr(self._pool_manager, "_use_processes", self.config.use_processes)
        return self._pool_manager

    def _effective_cancel_requested(
        self,
        cancel_requested: Callable[[], bool] | None,
    ) -> Callable[[], bool]:
        def _check() -> bool:
            if self._cancelled:
                return True
            if cancel_requested is None:
                return False
            return bool(cancel_requested())

        return _check

    def process_chapters_parallel(
        self,
        chapters: list[Any],
        process_func: Callable[[Any, int, Any], Any],
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> list[ChapterResult]:
        """
        Process chapters in parallel with VRAM-aware scheduling.
        """
        if self._cancelled or (
            cancel_requested is not None and bool(cancel_requested())
        ):
            return []
        if not chapters:
            return []

        pool_manager = self._get_pool_manager()
        try:
            return pool_manager.run_parallel(
                chapters=list(chapters),
                process_func=partial(
                    _process_chapter_wrapper,
                    process_func=process_func,
                ),
                progress_callback=progress_callback,
                config_fingerprint=self._config_fingerprint,
                max_workers=self.config.max_workers,
                available_device_memory_gb=self.config.vram_budget.available_device_memory_gb,
                worker_init_config=self._worker_init_config,
                memory_policy=self._memory_policy,
                cancel_requested=self._effective_cancel_requested(cancel_requested),
            )
        finally:
            if self._owns_pool_manager and self._pool_manager is not None:
                self._pool_manager.shutdown()

    def cancel(self) -> None:
        """Cancel parallel processing."""
        self._cancelled = True


def _process_chapter_wrapper(
    chapter: Any,
    chapter_idx: int,
    _file_manager: Any,
    process_func: Callable,
) -> Any:
    """
    Wrapper for chapter processing in worker process.
    """
    _ = _file_manager
    try:
        return process_func(chapter, chapter_idx, None)
    except Exception as e:
        logger.error(f"Chapter {chapter_idx} processing failed: {e}")
        raise


# Convenience functions for pipeline configuration


def get_optimal_worker_count(
    total_vram_gb: Optional[float] = None,
    max_desired: int = 4,
) -> int:
    """
    Calculate optimal worker count based on available VRAM.

    Args:
        total_vram_gb: Total VRAM available in GB
        max_desired: Maximum desired workers (default 4)

    Returns:
        Optimal number of workers (1-4)
    """
    detected_vram_gb = (
        _detect_available_ram_gb() if total_vram_gb is None else total_vram_gb
    )
    budget = VRAMBudget(total_vram_gb=detected_vram_gb)
    return min(budget.max_concurrent_chapters, max_desired)


def create_parallel_config(
    max_workers: Optional[int] = None,
    total_vram_gb: Optional[float] = None,
    enforce_vram_budget: bool = True,
) -> ParallelConfig:
    """
    Create a parallel configuration with smart defaults.
    """
    detected_vram_gb = (
        _detect_available_ram_gb() if total_vram_gb is None else total_vram_gb
    )
    if max_workers is None:
        max_workers = get_optimal_worker_count(detected_vram_gb)

    return ParallelConfig(
        max_workers=max_workers,
        use_processes=True,
        vram_budget=VRAMBudget(total_vram_gb=detected_vram_gb),
        enforce_vram_budget=enforce_vram_budget,
    )
