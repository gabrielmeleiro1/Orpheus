"""Stage timing helpers for pipeline execution and benchmarks."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from functools import wraps
from types import MethodType
from typing import Any, Callable


@dataclass
class StageTimings:
    """Accumulate coarse pipeline stage timings for one benchmark run."""

    totals: dict[str, float] = field(default_factory=dict)

    def add(self, key: str, elapsed_seconds: float) -> None:
        self.totals[key] = self.totals.get(key, 0.0) + max(0.0, elapsed_seconds)

    def get(self, key: str) -> float:
        return self.totals.get(key, 0.0)

    def rounded(self, key: str) -> float:
        return round(self.get(key), 4)

    def per_chunk(self, total_seconds: float, chunks: int) -> float | None:
        if chunks <= 0 or total_seconds <= 0:
            return None
        return round(total_seconds / chunks, 4)


def timed_stage(timing_key: str):
    def decorate(method: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(method)
        def timed(self: Any, *args: Any, **kwargs: Any) -> Any:
            timings = getattr(self, "_stage_timings", None)
            if timings is None:
                return method(self, *args, **kwargs)

            started_at = time.perf_counter()
            try:
                return method(self, *args, **kwargs)
            finally:
                timings[timing_key] = timings.get(timing_key, 0.0) + max(
                    0.0,
                    time.perf_counter() - started_at,
                )

        return timed

    return decorate


def time_instance_method(
    instance: Any,
    method_name: str,
    timing_key: str,
    timings: StageTimings,
) -> None:
    """Wrap an instance method to accumulate elapsed time into ``timings``."""
    original = getattr(instance, method_name, None)
    if not callable(original):
        return

    def timed(_self: Any, *args: Any, **kwargs: Any) -> Any:
        started_at = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            timings.add(timing_key, time.perf_counter() - started_at)

    setattr(instance, method_name, MethodType(timed, instance))
