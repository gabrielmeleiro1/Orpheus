"""
VRAM Monitor Module
===================
Real-time VRAM monitoring with dynamic batch sizing and idle timeout unloading.

Features:
- Monitor VRAM usage in real-time
- Dynamically reduce batch size when approaching limits
- Automatically unload models when idle for >5 minutes
- Thread-safe monitoring and adjustment
"""

from __future__ import annotations

import gc
import logging
import threading
import time
from typing import Optional, Callable, TYPE_CHECKING, Any
from collections import deque
import warnings

from .idle_timeout import IdleTimeoutManager
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
    IdleTimeoutConfig,
    VRAMPressureLevel,
    VRAMSnapshot,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AdaptiveMemoryManager",
    "DynamicBatchConfig",
    "GIB",
    "IdleTimeoutConfig",
    "IdleTimeoutManager",
    "SystemMemoryBudget",
    "VRAMMonitor",
    "VRAMPressureLevel",
    "VRAMSnapshot",
    "get_memory_manager",
]

mx: Any = None
MLX_AVAILABLE: Optional[bool] = None

if TYPE_CHECKING:
    pass


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
    Real-time VRAM monitor with dynamic batch sizing.
    
    Continuously monitors VRAM usage and adjusts batch sizes
    dynamically based on available memory.
    
    Example:
        monitor = VRAMMonitor(total_vram_gb=16.0)
        monitor.start_monitoring()
        
        # Get current batch size (auto-adjusted based on VRAM)
        batch_size = monitor.get_tts_batch_size()
        
        monitor.stop_monitoring()
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
        self._current_cleaner_batch_size = self.config.base_cleaner_batch_size
        
        # Monitoring state
        self._monitoring = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # VRAM history for trend analysis
        self._history: deque[VRAMSnapshot] = deque(maxlen=self.config.history_size)
        self._history_lock = threading.Lock()
        
        # Current pressure level
        self._pressure_level = VRAMPressureLevel.NORMAL
        self._pressure_lock = threading.Lock()
        
        # Callbacks for pressure level changes
        self._pressure_callbacks: list[Callable[[VRAMPressureLevel, VRAMPressureLevel], None]] = []
        
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
    
    def start_monitoring(self) -> None:
        """Start the VRAM monitoring thread."""
        if self._monitoring:
            return
        
        self._stop_event.clear()
        self._monitoring = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
    
    def stop_monitoring(self) -> None:
        """Stop the VRAM monitoring thread."""
        if not self._monitoring:
            return
        
        self._stop_event.set()
        self._monitoring = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5.0)
    
    def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while not self._stop_event.is_set():
            try:
                self._check_vram()
            except Exception as e:
                warnings.warn(f"VRAM monitoring error: {e}")
            
            self._stop_event.wait(self.config.check_interval_seconds)
    
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
                timestamp=time.time(),
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
                    self._notify_pressure_change(old_level, new_level)
                    
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
        new_cleaner_size = max(
            self.config.min_cleaner_batch_size,
            int(self.config.base_cleaner_batch_size * multiplier)
        )
        
        self._current_tts_batch_size = new_tts_size
        self._current_cleaner_batch_size = new_cleaner_size
        
        # Force garbage collection on high/critical
        if level in (VRAMPressureLevel.HIGH, VRAMPressureLevel.CRITICAL):
            gc.collect()
            mlx_core = _get_mlx_core()
            if MLX_AVAILABLE and mlx_core is not None:
                try:
                    mlx_core.clear_cache()
                except Exception:
                    pass
    
    def _notify_pressure_change(
        self,
        old_level: VRAMPressureLevel,
        new_level: VRAMPressureLevel,
    ) -> None:
        """Notify callbacks of pressure level change."""
        for callback in self._pressure_callbacks:
            try:
                callback(old_level, new_level)
            except Exception:
                pass
    
    def on_pressure_change(
        self,
        callback: Callable[[VRAMPressureLevel, VRAMPressureLevel], None]
    ) -> None:
        """Register a callback for pressure level changes."""
        self._pressure_callbacks.append(callback)
    
    def get_tts_batch_size(self) -> int:
        """Get current TTS batch size (dynamically adjusted)."""
        with self._pressure_lock:
            return self._current_tts_batch_size
    
    def get_cleaner_batch_size(self) -> int:
        """Get current cleaner batch size (dynamically adjusted)."""
        with self._pressure_lock:
            return self._current_cleaner_batch_size
    
    def get_pressure_level(self) -> VRAMPressureLevel:
        """Get current VRAM pressure level."""
        with self._pressure_lock:
            return self._pressure_level
    
    def get_current_usage(self) -> Optional[VRAMSnapshot]:
        """Get most recent VRAM snapshot."""
        with self._history_lock:
            return self._history[-1] if self._history else None
    
    def get_usage_trend(self) -> float:
        """
        Get VRAM usage trend (bytes/second).
        
        Returns:
            Positive means usage increasing, negative means decreasing
        """
        with self._history_lock:
            if len(self._history) < 2:
                return 0.0
            
            recent = list(self._history)[-5:]  # Last 5 samples
            if len(recent) < 2:
                return 0.0
            
            first = recent[0]
            last = recent[-1]
            time_delta = last.timestamp - first.timestamp
            
            if time_delta <= 0:
                return 0.0
            
            return (last.active_bytes - first.active_bytes) / time_delta


class AdaptiveMemoryManager:
    """
    Combined memory manager integrating VRAM monitoring and idle timeout.
    
    Provides a unified interface for adaptive memory management.
    
    Example:
        manager = AdaptiveMemoryManager(total_vram_gb=16.0)
        manager.start()
        
        # Register models
        manager.register_for_idle_timeout("tts", tts_engine.unload_model)
        
        # Get dynamically adjusted batch sizes
        batch_size = manager.get_tts_batch_size()
        
        manager.stop()
    """
    
    def __init__(
        self,
        total_vram_gb: Optional[float] = None,
        idle_timeout_seconds: float = 300.0,
        dynamic_config: Optional[DynamicBatchConfig] = None,
        idle_config: Optional[IdleTimeoutConfig] = None,
        memory_budget_provider: Optional[Callable[[], Any]] = None,
    ):
        self.vram_monitor = VRAMMonitor(
            total_vram_gb=total_vram_gb,
            config=dynamic_config,
            memory_budget_provider=memory_budget_provider,
        )
        self.idle_manager = IdleTimeoutManager(
            timeout_seconds=idle_timeout_seconds,
            config=idle_config,
        )
        self._started = False
    
    def start(self) -> None:
        """Start all monitoring."""
        if self._started:
            return
        
        self.vram_monitor.start_monitoring()
        self.idle_manager.start()
        self._started = True
    
    def stop(self) -> None:
        """Stop all monitoring."""
        if not self._started:
            return
        
        self.vram_monitor.stop_monitoring()
        self.idle_manager.stop()
        self._started = False
    
    def register_for_idle_timeout(self, name: str, unload_fn: Callable[[], None]) -> None:
        """Register a model for idle timeout management."""
        self.idle_manager.register_model(name, unload_fn)
    
    def record_activity(self) -> None:
        """Record activity to reset idle timer."""
        self.idle_manager.record_activity()
    
    def get_tts_batch_size(self) -> int:
        """Get current TTS batch size (dynamically adjusted)."""
        return self.vram_monitor.get_tts_batch_size()
    
    def get_cleaner_batch_size(self) -> int:
        """Get current cleaner batch size (dynamically adjusted)."""
        return self.vram_monitor.get_cleaner_batch_size()
    
    def get_vram_pressure(self) -> VRAMPressureLevel:
        """Get current VRAM pressure level."""
        return self.vram_monitor.get_pressure_level()

    def refresh_memory_pressure(self) -> None:
        """Refresh VRAM pressure from live memory counters."""
        self.vram_monitor.refresh()
    
    def get_idle_time(self) -> float:
        """Get current idle time in seconds."""
        return self.idle_manager.get_idle_time()


# Convenience function
def get_memory_manager(
    total_vram_gb: Optional[float] = None,
    idle_timeout_seconds: float = 300.0,
    dynamic_config: Optional[DynamicBatchConfig] = None,
    memory_budget_provider: Optional[Callable[[], Any]] = None,
) -> AdaptiveMemoryManager:
    """Get the global adaptive memory manager instance."""
    return AdaptiveMemoryManager(
        total_vram_gb=total_vram_gb,
        idle_timeout_seconds=idle_timeout_seconds,
        dynamic_config=dynamic_config,
        memory_budget_provider=memory_budget_provider,
    )
