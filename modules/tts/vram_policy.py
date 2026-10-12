"""Pressure levels, samples, and tuning knobs for adaptive memory management."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class VRAMPressureLevel(Enum):
    """VRAM pressure levels for adaptive batch sizing."""
    NORMAL = auto()      # < 70% usage
    ELEVATED = auto()    # 70-85% usage - slight reduction
    HIGH = auto()        # 85-95% usage - significant reduction
    CRITICAL = auto()    # > 95% usage - emergency measures


@dataclass
class VRAMSnapshot:
    """Snapshot of VRAM usage at a point in time."""
    active_bytes: int
    peak_bytes: int
    cache_bytes: int

    @property
    def peak_mb(self) -> float:
        return self.peak_bytes / (1024 * 1024)


@dataclass
class DynamicBatchConfig:
    """Configuration for dynamic batch sizing."""
    # Base batch sizes
    base_tts_batch_size: int = 4

    # VRAM thresholds (as percentage of available)
    elevated_threshold: float = 0.70
    high_threshold: float = 0.85
    critical_threshold: float = 0.95

    # Batch size multipliers at each level
    elevated_multiplier: float = 0.75
    high_multiplier: float = 0.50
    critical_multiplier: float = 0.25

    # Minimum batch sizes
    min_tts_batch_size: int = 1

    # History for trend analysis
    history_size: int = 10
