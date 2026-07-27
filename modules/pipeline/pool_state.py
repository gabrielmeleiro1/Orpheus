"""Configuration snapshot for the shared worker pool's current executor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class _PoolState:
    executor: Optional[Any] = None
    config_fingerprint: Any = None
    max_workers: int = 0
    available_vram_gb: float = 8.0
    worker_init_config: Optional[dict[str, Any]] = None
    use_processes: bool = True

    @property
    def available_device_memory_gb(self) -> float:
        return self.available_vram_gb

    @available_device_memory_gb.setter
    def available_device_memory_gb(self, value: float) -> None:
        self.available_vram_gb = value
