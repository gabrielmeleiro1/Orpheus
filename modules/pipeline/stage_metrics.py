"""Stage timing decorator for production chapter processing."""

from __future__ import annotations

import time
from functools import wraps
from typing import Any, Callable


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
