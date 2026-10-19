"""Memory-derived chapter worker recommendations."""

from __future__ import annotations

import os
from typing import Any, Optional

from modules.tts.device_memory import DeviceMemoryPolicy, HostMemoryPolicy

from .memory_policy import MemorySnapshot, SafeMemoryPolicy

STATIC_WORKER_ESTIMATE_BYTES = 4 * 1024**3
DEFAULT_REQUESTED_WORKER_CAP = 4


def detect_cpu_core_workers() -> int:
    """Return a conservative CPU-core worker count for process-safe backends."""
    process_cpu_count = getattr(os, "process_cpu_count", None)
    if callable(process_cpu_count):
        detected = process_cpu_count()
    else:
        detected = os.cpu_count()
    return max(1, int(detected or 1))


def build_cpu_core_worker_recommendation(*, requested_max: Optional[int] = None) -> dict[str, Any]:
    """Return worker recommendation details for process-safe CPU-bound backends."""
    detected_workers = detect_cpu_core_workers()
    requested = max(1, int(requested_max or detected_workers))
    recommended = min(requested, detected_workers)
    return {
        "memory_measurement_available": False,
        "total_memory_bytes": None,
        "available_memory_bytes": None,
        "reserved_memory_bytes": None,
        "headroom_memory_bytes": None,
        "static_estimated_worker_bytes": None,
        "warmup_memory_delta_bytes": None,
        "estimated_per_worker_memory_bytes": None,
        "requested_parallel_chapters": requested,
        "detected_cpu_core_workers": detected_workers,
        "max_safe_parallel_chapters": detected_workers,
        "recommended_parallel_chapters": recommended,
        "reason": "process-safe CPU-core auto worker policy",
        "calculation": f"detected CPU core workers = {detected_workers}; capped at {requested}",
    }


def _gib(value: int) -> float:
    return round(max(0, int(value)) / 1024**3, 2)


def _memory_snapshot_from_device_policy(
    device_memory_policy: DeviceMemoryPolicy,
) -> Optional[MemorySnapshot]:
    snapshot = device_memory_policy.sample()
    if snapshot is None:
        return None
    return MemorySnapshot(
        total_bytes=snapshot.total_bytes,
        available_bytes=snapshot.available_bytes,
        process_rss_bytes=snapshot.process_rss_bytes,
        mlx_active_bytes=snapshot.active_bytes,
        mlx_cache_bytes=snapshot.cache_bytes,
        mlx_peak_bytes=snapshot.peak_bytes,
    )


def sample_memory_snapshot(
    device_memory_policy: DeviceMemoryPolicy | None = None,
) -> Optional[MemorySnapshot]:
    """Sample memory through the selected device policy."""
    return _memory_snapshot_from_device_policy(device_memory_policy or HostMemoryPolicy())


def build_parallel_worker_recommendation(
    *,
    requested_max: int = DEFAULT_REQUESTED_WORKER_CAP,
    snapshot: Optional[MemorySnapshot] = None,
    estimated_worker_bytes: Optional[int] = None,
    warmup_delta_bytes: Optional[int] = None,
    selected_worker_count_reason: Optional[str] = None,
    device_memory_policy: DeviceMemoryPolicy | None = None,
) -> dict[str, Any]:
    """Return UI-safe worker recommendation details from the memory policy."""
    requested_max = max(1, int(requested_max or 1))
    if snapshot is None:
        snapshot = sample_memory_snapshot(device_memory_policy=device_memory_policy)
    static_estimate = STATIC_WORKER_ESTIMATE_BYTES
    worker_estimate = max(1, int(estimated_worker_bytes or static_estimate))
    unavailable_reason = (
        device_memory_policy.sampling_unavailable_reason
        if device_memory_policy is not None
        else None
    ) or "memory measurement unavailable"

    if snapshot is None:
        return {
            "memory_measurement_available": False,
            "total_memory_bytes": None,
            "available_memory_bytes": None,
            "reserved_memory_bytes": None,
            "headroom_memory_bytes": None,
            "static_estimated_worker_bytes": static_estimate,
            "warmup_memory_delta_bytes": warmup_delta_bytes,
            "estimated_per_worker_memory_bytes": worker_estimate,
            "requested_parallel_chapters": requested_max,
            "max_safe_parallel_chapters": 1,
            "recommended_parallel_chapters": 1,
            "reason": unavailable_reason,
            "calculation": "memory unavailable -> 1 worker",
        }

    policy = SafeMemoryPolicy(
        memory_sampler=lambda: snapshot,
        estimated_worker_bytes=worker_estimate,
        enable_warmup_calibration=False,
        unavailable_reason=unavailable_reason,
    )
    if warmup_delta_bytes is not None:
        policy.measured_warmup_delta_bytes = max(0, int(warmup_delta_bytes))

    reserve_bytes = policy.reserve_bytes(snapshot)
    headroom_bytes = max(0, snapshot.available_bytes - reserve_bytes)
    max_safe = policy.allowed_workers(requested_max)
    reason = (
        selected_worker_count_reason
        or policy.selected_worker_count_reason
        or policy.safe_mode_reason
        or "static estimate with live headroom"
    )
    return {
        "memory_measurement_available": True,
        "total_memory_bytes": snapshot.total_bytes,
        "available_memory_bytes": snapshot.available_bytes,
        "reserved_memory_bytes": reserve_bytes,
        "headroom_memory_bytes": headroom_bytes,
        "static_estimated_worker_bytes": static_estimate,
        "warmup_memory_delta_bytes": warmup_delta_bytes,
        "estimated_per_worker_memory_bytes": worker_estimate,
        "requested_parallel_chapters": requested_max,
        "max_safe_parallel_chapters": max_safe,
        "recommended_parallel_chapters": max_safe,
        "reason": reason,
        "calculation": (
            f"available - reserved = {_gib(snapshot.available_bytes)} GiB - "
            f"{_gib(reserve_bytes)} GiB = {_gib(headroom_bytes)} GiB; "
            f"1 + floor(headroom / worker_estimate) capped at {requested_max}"
        ),
    }
