"""Conservative memory policy for safe chapter parallelism."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class MemorySnapshot:
    total_bytes: int
    available_bytes: int
    process_rss_bytes: int = 0
    mlx_active_bytes: int = 0
    mlx_cache_bytes: int = 0
    mlx_peak_bytes: int = 0


class SafeMemoryPolicy:
    def __init__(
        self,
        *,
        memory_sampler: Callable[[], Optional[MemorySnapshot]],
        reserve_fraction: float = 0.25,
        reserve_floor_bytes: int = 4 * 1024**3,
        calibration_buffer_bytes: int = 1024**3,
        estimated_worker_bytes: Optional[int] = None,
        warmup_safety_multiplier: float = 1.5,
        warmup_floor_bytes: int = 1024**3,
        enable_warmup_calibration: bool = True,
        unavailable_reason: str = "memory measurement unavailable",
    ) -> None:
        self._memory_sampler = memory_sampler
        self._reserve_fraction = reserve_fraction
        self._reserve_floor_bytes = reserve_floor_bytes
        self._calibration_buffer_bytes = max(0, int(calibration_buffer_bytes))
        self._warmup_safety_multiplier = max(1.0, float(warmup_safety_multiplier))
        self._warmup_floor_bytes = max(1, int(warmup_floor_bytes))
        self.enable_warmup_calibration = bool(enable_warmup_calibration)
        self._baseline_snapshot: Optional[MemorySnapshot] = None
        self.measured_warmup_delta_bytes: Optional[int] = None
        self.estimated_worker_bytes: Optional[int] = (
            max(1, int(estimated_worker_bytes))
            if estimated_worker_bytes is not None
            else None
        )
        self._force_sequential = False
        self.safe_mode_reason: Optional[str] = None
        self.selected_worker_count_reason: Optional[str] = None
        self._unavailable_reason = str(unavailable_reason or "memory measurement unavailable")

    @property
    def estimated_per_worker_memory_bytes(self) -> Optional[int]:
        return self.estimated_worker_bytes

    def reserve_bytes(self, snapshot: MemorySnapshot) -> int:
        return max(
            self._reserve_floor_bytes,
            int(snapshot.total_bytes * self._reserve_fraction),
        )

    def record_baseline(self, snapshot: Optional[MemorySnapshot] = None) -> None:
        self._baseline_snapshot = snapshot if snapshot is not None else self._memory_sampler()

    def record_warmup(self, snapshot: Optional[MemorySnapshot] = None) -> None:
        if not self.enable_warmup_calibration:
            return
        self._record_worker_observation(snapshot, source="warmup")

    def record_memory_error(self) -> None:
        self._force_sequential = True
        self.safe_mode_reason = "memory pressure error"
        self.selected_worker_count_reason = "memory pressure error"

    @staticmethod
    def _model_memory_bytes(snapshot: MemorySnapshot) -> int:
        return (
            snapshot.process_rss_bytes
            + snapshot.mlx_active_bytes
            + snapshot.mlx_cache_bytes
        )

    def _observed_worker_delta(self, snapshot: MemorySnapshot) -> int:
        if self._baseline_snapshot is None:
            return 0
        available_delta = (
            self._baseline_snapshot.available_bytes - snapshot.available_bytes
        )
        model_delta = self._model_memory_bytes(snapshot) - self._model_memory_bytes(
            self._baseline_snapshot
        )
        return max(0, int(available_delta), int(model_delta))

    def _record_worker_observation(
        self,
        snapshot: Optional[MemorySnapshot] = None,
        *,
        source: str,
    ) -> None:
        observed = snapshot if snapshot is not None else self._memory_sampler()
        if observed is None:
            return
        delta = self._observed_worker_delta(observed)
        if delta <= 0:
            return
        if source == "warmup":
            self.measured_warmup_delta_bytes = delta
            estimated = max(
                self._warmup_floor_bytes,
                int(delta * self._warmup_safety_multiplier),
            )
            self.estimated_worker_bytes = estimated
        else:
            estimated = delta + self._calibration_buffer_bytes
            if self.estimated_worker_bytes is None:
                self.estimated_worker_bytes = estimated
            else:
                self.estimated_worker_bytes = max(
                    self.estimated_worker_bytes,
                    estimated,
                )

    def allowed_workers(self, requested_max: int) -> int:
        requested_max = max(1, int(requested_max))
        if self._force_sequential:
            self.safe_mode_reason = "memory pressure error"
            self.selected_worker_count_reason = "memory pressure error"
            return 1
        snapshot = self._memory_sampler()
        if snapshot is None:
            self.safe_mode_reason = self._unavailable_reason
            self.selected_worker_count_reason = self._unavailable_reason
            return 1
        if snapshot.available_bytes <= self.reserve_bytes(snapshot):
            self.safe_mode_reason = "memory headroom below safe reserve"
            self.selected_worker_count_reason = "memory headroom below safe reserve"
            return 1
        if requested_max > 1 and not self.estimated_worker_bytes:
            self.safe_mode_reason = "memory worker cost unknown"
            self.selected_worker_count_reason = "memory worker cost unknown"
            return 1
        if requested_max > 1:
            headroom_bytes = max(
                0,
                snapshot.available_bytes - self.reserve_bytes(snapshot),
            )
            extra_workers = int(headroom_bytes / max(1, self.estimated_worker_bytes))
            allowed = max(1, min(requested_max, 1 + extra_workers))
            if allowed < requested_max:
                self.safe_mode_reason = "memory headroom limits parallel workers"
                self.selected_worker_count_reason = (
                    "memory headroom limits parallel workers"
                )
            else:
                self.safe_mode_reason = None
                if self.measured_warmup_delta_bytes is not None:
                    self.selected_worker_count_reason = (
                        "warmup estimate with live headroom"
                    )
                else:
                    self.selected_worker_count_reason = (
                        "static estimate with live headroom"
                    )
            return allowed
        self.safe_mode_reason = None
        self.selected_worker_count_reason = "requested sequential mode"
        return requested_max

    def sequential_regardless_of_free_memory(self, requested_max: int) -> bool:
        """True when ``allowed_workers`` returns 1 however much memory is freed.

        Side-effect free. The best case is every byte of the device free
        (``available == total``); ``allowed_workers`` never grows as available
        memory shrinks, so a single worker there means a single worker for any
        live sample taken after releasing memory.
        """
        requested_max = max(1, int(requested_max))
        if requested_max <= 1:
            return False
        if self._force_sequential or not self.estimated_worker_bytes:
            return True
        snapshot = self._memory_sampler()
        if snapshot is None:
            return False
        best_headroom = snapshot.total_bytes - self.reserve_bytes(snapshot)
        if best_headroom <= 0:
            return True
        return int(best_headroom / max(1, self.estimated_worker_bytes)) == 0

    def should_submit_more(
        self,
        active_workers: int,
        *,
        workers_resident: bool = False,
    ) -> bool:
        """Return whether another chapter may run alongside ``active_workers``.

        A pool whose workers have already loaded their models passes
        ``workers_resident=True``: the live sample already reflects their
        memory, so only the reserve floor applies.
        """
        if self._force_sequential:
            self.safe_mode_reason = "memory pressure error"
            return active_workers < 1

        snapshot = self._memory_sampler()
        if snapshot is None:
            self.safe_mode_reason = self._unavailable_reason
            return active_workers < 1
        if snapshot.available_bytes <= self.reserve_bytes(snapshot):
            self.safe_mode_reason = "memory headroom below safe reserve"
            return active_workers < 1
        if active_workers <= 0 or workers_resident:
            self.safe_mode_reason = None
            return True
        if not self.estimated_worker_bytes:
            self.safe_mode_reason = "memory worker cost unknown"
            return active_workers < 1

        headroom_bytes = max(0, snapshot.available_bytes - self.reserve_bytes(snapshot))
        self.safe_mode_reason = None
        return headroom_bytes >= active_workers * self.estimated_worker_bytes
