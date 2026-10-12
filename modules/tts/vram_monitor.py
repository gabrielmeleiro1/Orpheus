"""
VRAM Monitor Module
===================
VRAM monitoring with dynamic batch sizing.

Features:
- Dynamically reduce batch size when approaching limits
- Thread-safe adjustment
"""

from __future__ import annotations

import gc
import threading
from typing import Optional, Callable, Any
from collections import deque

from .vram_budget import (
    GIB,
    SystemMemoryBudget,
    _budgeted_mlx_bytes,
    _coerce_memory_budget,
    _sample_sysconf_memory,
    _sample_virtual_memory,
)
from .vram_policy import (
    DynamicBatchConfig,
    VRAMPressureLevel,
    VRAMSnapshot,
)

__all__ = [
    "AdaptiveMemoryManager",
    "DynamicBatchConfig",
    "GIB",
    "SystemMemoryBudget",
    "VRAMMonitor",
    "VRAMPressureLevel",
    "VRAMSnapshot",
    "get_memory_manager",
]

mx: Any = None
MLX_AVAILABLE: Optional[bool] = None


def _get_mlx_core() -> Any:
    """Import MLX lazily so module import does not initialize Metal."""
    global MLX_AVAILABLE, mx
    if MLX_AVAILABLE is not None:
        return mx

    try:
        import mlx.core as mlx_core
    except ImportError:
        mx = None
        MLX_AVAILABLE = False
    else:
        mx = mlx_core
        MLX_AVAILABLE = True
    return mx


def _read_mlx_byte_count(mlx_core: Any, *names: str) -> int:
    for name in names:
        probe = getattr(mlx_core, name, None)
        if not callable(probe):
            continue
        try:
            return max(0, int(probe()))
        except Exception:
            return 0
    return 0


def _sample_mlx_memory(mlx_core: Any = None) -> tuple[int, int, int]:
    if mlx_core is None:
        mlx_core = _get_mlx_core()
    if not mlx_core:
        return 0, 0, 0
    return (
        _read_mlx_byte_count(mlx_core, "get_active_memory", "active_memory"),
        _read_mlx_byte_count(mlx_core, "get_cache_memory", "cache_memory"),
        _read_mlx_byte_count(mlx_core, "get_peak_memory", "peak_memory"),
    )


def _sample_system_memory_budget() -> SystemMemoryBudget:
    """Sample live system headroom and MLX memory with conservative fallbacks."""
    memory = _sample_virtual_memory() or _sample_sysconf_memory()
    if memory is None:
        total_bytes = 8 * GIB
        available_bytes = int(total_bytes * 0.70)
    else:
        total_bytes, available_bytes = memory

    mlx_active, mlx_cache, mlx_peak = _sample_mlx_memory()
    return SystemMemoryBudget(
        total_bytes=total_bytes,
        available_bytes=available_bytes,
        mlx_active_bytes=mlx_active,
        mlx_cache_bytes=mlx_cache,
        mlx_peak_bytes=mlx_peak,
    )


class VRAMMonitor:
    """
    VRAM monitor with dynamic batch sizing.
    
    Samples VRAM usage on refresh and adjusts batch sizes
    dynamically based on available memory.
    """
    
    _instance: Optional[VRAMMonitor] = None
    _instance_lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        """Singleton pattern for global monitor."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(
        self,
        total_vram_gb: Optional[float] = None,
        reserved_vram_gb: float = 2.0,
        config: Optional[DynamicBatchConfig] = None,
        memory_budget_provider: Optional[Callable[[], Any]] = None,
    ):
        # Only initialize once
        if hasattr(self, '_initialized') and self._initialized:
            self.configure(
                total_vram_gb=total_vram_gb,
                reserved_vram_gb=reserved_vram_gb,
                config=config,
                memory_budget_provider=memory_budget_provider,
            )
            return

        self._manual_total_vram_bytes = (
            int(total_vram_gb * GIB) if total_vram_gb is not None else None
        )
        self._memory_budget_provider = (
            memory_budget_provider or _sample_system_memory_budget
        )
        self.total_vram_bytes = 0
        self.reserved_vram_bytes = int(reserved_vram_gb * GIB)
        self.available_vram_bytes = 1
        self._last_memory_budget: Optional[SystemMemoryBudget] = None
        self.config = config or DynamicBatchConfig()
        self._refresh_memory_budget()
        
        # Current batch sizes (dynamically adjusted)
        self._current_tts_batch_size = self.config.base_tts_batch_size
        
        # VRAM history for trend analysis
        self._history: deque[VRAMSnapshot] = deque(maxlen=self.config.history_size)
        self._history_lock = threading.Lock()
        
        # Current pressure level
        self._pressure_level = VRAMPressureLevel.NORMAL
        self._pressure_lock = threading.Lock()
        
        self._initialized = True

    def configure(
        self,
        *,
        total_vram_gb: Optional[float] = None,
        reserved_vram_gb: Optional[float] = None,
        config: Optional[DynamicBatchConfig] = None,
        memory_budget_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        """Apply updated runtime sizing inputs to the singleton monitor."""
        if total_vram_gb is not None:
            self._manual_total_vram_bytes = int(total_vram_gb * GIB)
        if reserved_vram_gb is not None:
            self.reserved_vram_bytes = int(reserved_vram_gb * GIB)
        if memory_budget_provider is not None:
            self._memory_budget_provider = memory_budget_provider
        if config is not None:
            self.config = config
            with self._history_lock:
                self._history = deque(maxlen=self.config.history_size)

        self._refresh_memory_budget()
        with self._pressure_lock:
            self._adjust_batch_sizes(self._pressure_level)

    def _refresh_memory_budget(
        self,
        *,
        mlx_active_bytes: Optional[int] = None,
        mlx_cache_bytes: Optional[int] = None,
        mlx_peak_bytes: Optional[int] = None,
    ) -> None:
        """Refresh the usable MLX budget from manual or live system inputs."""
        if self._manual_total_vram_bytes is not None:
            active = max(0, int(mlx_active_bytes or 0))
            cache = max(0, int(mlx_cache_bytes or 0))
            peak = max(0, int(mlx_peak_bytes or 0))
            self.total_vram_bytes = self._manual_total_vram_bytes
            self.available_vram_bytes = max(
                1,
                self.total_vram_bytes - self.reserved_vram_bytes,
            )
            self._last_memory_budget = SystemMemoryBudget(
                total_bytes=self.total_vram_bytes,
                available_bytes=self.available_vram_bytes,
                mlx_active_bytes=active,
                mlx_cache_bytes=cache,
                mlx_peak_bytes=peak,
            )
            return

        try:
            sample = _coerce_memory_budget(self._memory_budget_provider())
        except Exception:
            sample = _sample_system_memory_budget()

        if mlx_active_bytes is not None:
            sample = SystemMemoryBudget(
                total_bytes=sample.total_bytes,
                available_bytes=sample.available_bytes,
                mlx_active_bytes=max(0, int(mlx_active_bytes)),
                mlx_cache_bytes=sample.mlx_cache_bytes,
                mlx_peak_bytes=sample.mlx_peak_bytes,
            )
        if mlx_cache_bytes is not None:
            sample = SystemMemoryBudget(
                total_bytes=sample.total_bytes,
                available_bytes=sample.available_bytes,
                mlx_active_bytes=sample.mlx_active_bytes,
                mlx_cache_bytes=max(0, int(mlx_cache_bytes)),
                mlx_peak_bytes=sample.mlx_peak_bytes,
            )
        if mlx_peak_bytes is not None:
            sample = SystemMemoryBudget(
                total_bytes=sample.total_bytes,
                available_bytes=sample.available_bytes,
                mlx_active_bytes=sample.mlx_active_bytes,
                mlx_cache_bytes=sample.mlx_cache_bytes,
                mlx_peak_bytes=max(0, int(mlx_peak_bytes)),
            )

        if sample.total_bytes <= 0:
            fallback = _sample_system_memory_budget()
            sample = SystemMemoryBudget(
                total_bytes=fallback.total_bytes,
                available_bytes=fallback.available_bytes,
                mlx_active_bytes=sample.mlx_active_bytes,
                mlx_cache_bytes=sample.mlx_cache_bytes,
                mlx_peak_bytes=sample.mlx_peak_bytes,
            )

        self.total_vram_bytes = max(1, sample.total_bytes)
        self.available_vram_bytes = _budgeted_mlx_bytes(
            sample,
            self.reserved_vram_bytes,
        )
        self._last_memory_budget = sample

    def _system_headroom_exhausted(self) -> bool:
        if self._manual_total_vram_bytes is not None or self._last_memory_budget is None:
            return False
        return self._last_memory_budget.available_bytes <= self.reserved_vram_bytes

    def refresh(self) -> None:
        """Refresh memory pressure immediately without waiting for the monitor."""
        self._check_vram()
    
    def _check_vram(self) -> None:
        """Check VRAM usage and adjust batch sizes."""
        mlx_core = _get_mlx_core()
        if not MLX_AVAILABLE or mlx_core is None:
            self._refresh_memory_budget()
            return
        
        try:
            # Get current VRAM stats
            active = mlx_core.get_active_memory()
            peak = mlx_core.get_peak_memory()
            cache = mlx_core.get_cache_memory()
            self._refresh_memory_budget(
                mlx_active_bytes=active,
                mlx_cache_bytes=cache,
                mlx_peak_bytes=peak,
            )
            
            snapshot = VRAMSnapshot(
                active_bytes=active,
                peak_bytes=peak,
                cache_bytes=cache,
            )
            
            with self._history_lock:
                self._history.append(snapshot)
            
            # Calculate usage ratio
            committed_bytes = active + cache
            usage_ratio = 1.0 if self._system_headroom_exhausted() else (
                committed_bytes / self.available_vram_bytes
                if self.available_vram_bytes > 0
                else 1.0
            )
            
            # Determine pressure level
            new_level = self._calculate_pressure_level(usage_ratio)
            
            with self._pressure_lock:
                old_level = self._pressure_level
                if new_level != old_level:
                    self._pressure_level = new_level
                    self._adjust_batch_sizes(new_level)
                    
        except Exception:
            pass  # Silently ignore MLX errors
    
    def _calculate_pressure_level(self, usage_ratio: float) -> VRAMPressureLevel:
        """Calculate VRAM pressure level from usage ratio."""
        if usage_ratio >= self.config.critical_threshold:
            return VRAMPressureLevel.CRITICAL
        elif usage_ratio >= self.config.high_threshold:
            return VRAMPressureLevel.HIGH
        elif usage_ratio >= self.config.elevated_threshold:
            return VRAMPressureLevel.ELEVATED
        return VRAMPressureLevel.NORMAL
    
    def _adjust_batch_sizes(self, level: VRAMPressureLevel) -> None:
        """Adjust batch sizes based on pressure level."""
        if level == VRAMPressureLevel.NORMAL:
            multiplier = 1.0
        elif level == VRAMPressureLevel.ELEVATED:
            multiplier = self.config.elevated_multiplier
        elif level == VRAMPressureLevel.HIGH:
            multiplier = self.config.high_multiplier
        else:  # CRITICAL
            multiplier = self.config.critical_multiplier
        
        # Calculate new batch sizes
        new_tts_size = max(
            self.config.min_tts_batch_size,
            int(self.config.base_tts_batch_size * multiplier)
        )
        
        self._current_tts_batch_size = new_tts_size
        
        # Force garbage collection on high/critical
        if level in (VRAMPressureLevel.HIGH, VRAMPressureLevel.CRITICAL):
            gc.collect()
            mlx_core = _get_mlx_core()
            if MLX_AVAILABLE and mlx_core is not None:
                try:
                    mlx_core.clear_cache()
                except Exception:
                    pass
    
    def get_tts_batch_size(self) -> int:
        """Get current TTS batch size (dynamically adjusted)."""
        with self._pressure_lock:
            return self._current_tts_batch_size
    
    def get_pressure_level(self) -> VRAMPressureLevel:
        """Get current VRAM pressure level."""
        with self._pressure_lock:
            return self._pressure_level
    
    def get_current_usage(self) -> Optional[VRAMSnapshot]:
        """Get most recent VRAM snapshot."""
        with self._history_lock:
            return self._history[-1] if self._history else None


class AdaptiveMemoryManager:
    """
    Memory manager integrating VRAM monitoring.
    
    Provides a unified interface for adaptive memory management.
    """
    
    def __init__(
        self,
        dynamic_config: Optional[DynamicBatchConfig] = None,
        memory_budget_provider: Optional[Callable[[], Any]] = None,
    ):
        self.vram_monitor = VRAMMonitor(
            config=dynamic_config,
            memory_budget_provider=memory_budget_provider,
        )
    
    def get_tts_batch_size(self) -> int:
        """Get current TTS batch size (dynamically adjusted)."""
        return self.vram_monitor.get_tts_batch_size()
    
    def get_vram_pressure(self) -> VRAMPressureLevel:
        """Get current VRAM pressure level."""
        return self.vram_monitor.get_pressure_level()

    def refresh_memory_pressure(self) -> None:
        """Refresh VRAM pressure from live memory counters."""
        self.vram_monitor.refresh()


# Convenience function
def get_memory_manager(
    dynamic_config: Optional[DynamicBatchConfig] = None,
) -> AdaptiveMemoryManager:
    """Get the global adaptive memory manager instance."""
    return AdaptiveMemoryManager(dynamic_config=dynamic_config)
