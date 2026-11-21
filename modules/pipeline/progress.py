"""Pipeline progress + ETA helpers."""

from __future__ import annotations

import time
from typing import Any, Optional


def estimate_eta_from_progress(
    start_time: float | None,
    progress: float | None,
    *,
    now: float | None = None,
    min_progress: float = 0.10,
    min_elapsed_seconds: float = 1.0,
) -> Optional[float]:
    """Estimate remaining seconds from weighted overall progress."""
    if start_time is None or progress is None:
        return None

    progress_value = max(0.0, min(1.0, float(progress)))
    if progress_value >= 1.0:
        return 0.0
    if progress_value <= 0.0 or progress_value < max(0.0, float(min_progress)):
        return None

    now_value = time.monotonic() if now is None else float(now)
    elapsed = max(0.0, now_value - float(start_time))
    if elapsed < max(0.0, float(min_elapsed_seconds)):
        return None

    return max(0.0, elapsed * ((1.0 - progress_value) / progress_value))


def chapter_progress_message(chapter_idx: int, total_chapters: int, activity: str) -> str:
    """Prefix chapter-local progress with its chapter ("Chapter 3/8: cleaning chunk 1/4").

    Chunk counters restart in every chapter, so without the chapter they read
    as whole-book progress.
    """
    if total_chapters <= 0:
        return activity[:1].upper() + activity[1:]
    return f"Chapter {chapter_idx + 1}/{total_chapters}: {activity}"


class PipelineProgressTracker:
    """Tracks coarse-grained progress state for a conversion pipeline instance."""

    def __init__(self, state: Any):
        self._state = state

    def set_total_chars(self, total_chars: int) -> None:
        """Persist the total-character count used by the adaptive worker guard."""
        self._state.total_chars = max(0, int(total_chars))

    def notify(
        self,
        *,
        callback: Any,
        stage: Any,
        chapter_idx: int = 0,
        total_chapters: int = 0,
        chunk_idx: int = 0,
        total_chunks: int = 0,
        message: str = "",
        performance: dict[str, Any] | None = None,
    ) -> None:
        """Emit a progress callback with the current performance snapshot."""
        if callback is None:
            return

        callback(
            stage,
            chapter_idx,
            total_chapters,
            chunk_idx,
            total_chunks,
            message,
            performance,
        )
