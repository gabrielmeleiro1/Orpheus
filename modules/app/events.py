"""
Application Event Contracts
===========================
Typed events for progress/log/state updates across CLI and TUI surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """High-level event categories."""

    PROGRESS = "progress"
    LOG = "log"
    STATE = "state"


class JobState(str, Enum):
    """Job lifecycle states."""

    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ProgressEvent:
    """Progress update for conversion stage/chunk/chapter operations."""

    event_type: EventType
    timestamp: str
    stage: str
    progress: float
    overall_progress: float
    stage_progress: float
    message: str
    eta: float | None = None
    chapter_current: int = 0
    chapter_total: int = 0
    chunk_current: int = 0
    chunk_total: int = 0
    performance: dict[str, Any] = field(default_factory=dict)
    # Every asynchronous conversion update is scoped to its owning job. Keep
    # this optional for compatibility with direct pipeline/TUI callers that do
    # not have a controller job identifier.
    job_id: str | None = None


def _normalize_counter(value: int | None) -> int:
    try:
        numeric = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, numeric)


def _normalize_counter_pair(current: int | None, total: int | None) -> tuple[int, int]:
    normalized_total = _normalize_counter(total)
    normalized_current = _normalize_counter(current)
    if normalized_total > 0:
        normalized_current = min(normalized_current, normalized_total)
    return normalized_current, normalized_total


def _normalize_float(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric < 0:
        numeric = 0.0
    return round(numeric, 2)


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    normalized: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            normalized.append(text)
    return normalized


def _normalize_performance(performance: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(performance, dict):
        return {}

    normalized: dict[str, Any] = {}
    mode = str(performance.get("pipeline_mode") or "").strip().lower()
    if mode:
        normalized["pipeline_mode"] = mode

    max_workers = _normalize_counter(performance.get("max_workers"))
    if max_workers:
        normalized["max_workers"] = max_workers

    for key in ("requested_parallel_chapters", "effective_parallel_chapters"):
        value = _normalize_counter(performance.get(key))
        if value:
            normalized[key] = value

    worker_mode = str(performance.get("parallel_worker_mode") or "").strip().lower()
    if worker_mode:
        normalized["parallel_worker_mode"] = worker_mode

    for key in (
        "active_workers",
        "queued_chapters",
        "running_chapters",
        "completed_chapters",
        "failed_chapters",
    ):
        value = _normalize_counter(performance.get(key))
        if key == "active_workers" and max_workers:
            value = min(value, max_workers)
        normalized[key] = value

    active_titles = _normalize_string_list(performance.get("active_chapter_titles"))
    if active_titles:
        normalized["active_chapter_titles"] = active_titles

    for key in (
        "cleaner_chunks_per_second",
        "tts_chars_per_second",
        "encoder_chapters_per_second",
        "elapsed_seconds",
        "eta_seconds",
    ):
        value = _normalize_float(performance.get(key))
        if value is not None:
            normalized[key] = value

    fallback_reason = str(performance.get("fallback_reason") or "").strip()
    if fallback_reason:
        normalized["fallback_reason"] = fallback_reason

    return normalized


@dataclass(frozen=True)
class LogEvent:
    """Log message emitted from the pipeline/controller."""

    event_type: EventType
    timestamp: str
    level: str
    message: str
    performance: dict[str, Any] = field(default_factory=dict)
    # See ProgressEvent.job_id; controller-emitted logs always provide it.
    job_id: str | None = None


@dataclass(frozen=True)
class StateEvent:
    """State transition event for conversion jobs."""

    event_type: EventType
    timestamp: str
    state: JobState
    job_id: str
    message: str = ""


def make_progress_event(
    stage: str,
    progress: float,
    message: str = "",
    *,
    stage_progress: float | None = None,
    eta: float | None = None,
    chapter_current: int | None = None,
    chapter_total: int | None = None,
    chunk_current: int | None = None,
    chunk_total: int | None = None,
    performance: dict[str, Any] | None = None,
    job_id: str | None = None,
) -> ProgressEvent:
    """Create a normalized progress event."""
    pct = max(0.0, min(1.0, progress))
    stage_pct = pct if stage_progress is None else max(0.0, min(1.0, stage_progress))
    eta_value = None if eta is None else max(0.0, float(eta))
    normalized_chapter_current, normalized_chapter_total = _normalize_counter_pair(
        chapter_current,
        chapter_total,
    )
    normalized_chunk_current, normalized_chunk_total = _normalize_counter_pair(
        chunk_current,
        chunk_total,
    )
    normalized_performance = _normalize_performance(performance)
    if eta_value is not None and "eta_seconds" not in normalized_performance:
        normalized_performance["eta_seconds"] = round(eta_value, 2)
    return ProgressEvent(
        event_type=EventType.PROGRESS,
        timestamp=_now_iso(),
        stage=stage,
        progress=pct,
        overall_progress=pct,
        stage_progress=stage_pct,
        message=message,
        eta=eta_value,
        chapter_current=normalized_chapter_current,
        chapter_total=normalized_chapter_total,
        chunk_current=normalized_chunk_current,
        chunk_total=normalized_chunk_total,
        performance=normalized_performance,
        job_id=job_id,
    )


def make_log_event(
    message: str,
    level: str = "info",
    *,
    performance: dict[str, Any] | None = None,
    job_id: str | None = None,
) -> LogEvent:
    """Create a normalized log event."""
    return LogEvent(
        event_type=EventType.LOG,
        timestamp=_now_iso(),
        level=level.lower(),
        message=message,
        performance=_normalize_performance(performance),
        job_id=job_id,
    )


def make_state_event(state: JobState, job_id: str, message: str = "") -> StateEvent:
    """Create a normalized state event."""
    return StateEvent(
        event_type=EventType.STATE,
        timestamp=_now_iso(),
        state=state,
        job_id=job_id,
        message=message,
    )
