"""Pipeline progress + ETA helpers."""

from __future__ import annotations

import time
from typing import Any, Optional


def estimate_eta_from_chars(
    start_time: float | None,
    chars_processed: int,
    total_chars: int,
) -> Optional[float]:
    """Estimate remaining seconds from character throughput."""
    if start_time is None or chars_processed <= 0:
        return None

    elapsed = time.time() - start_time
    if elapsed <= 0:
        return None

    chars_per_second = chars_processed / elapsed
    if chars_per_second <= 0:
        return None

    remaining_chars = max(0, total_chars - chars_processed)
    return remaining_chars / chars_per_second


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

    def reset(self) -> None:
        """Reset counters at the start of a conversion pass."""
        self._state.start_time = time.time()
        self._state.chars_processed = 0
        self._state.total_chars = 0

    def set_total_chars(self, total_chars: int) -> None:
        """Persist total-character estimate used for ETA."""
        self._state.total_chars = max(0, int(total_chars))

    def add_processed_chars(self, chars: int) -> None:
        """Increment processed character count."""
        self._state.chars_processed += max(0, int(chars))

    def estimate_eta(self) -> Optional[float]:
        """Estimate remaining processing time in seconds."""
        return estimate_eta_from_chars(
            self._state.start_time,
            self._state.chars_processed,
            self._state.total_chars,
        )

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
        """Emit progress callback with an ETA snapshot."""
        if callback is None:
            return

        args = (
            stage,
            chapter_idx,
            total_chapters,
            chunk_idx,
            total_chunks,
            message,
            self.estimate_eta(),
        )
        try:
            callback(*args, performance)
        except TypeError as original_error:
            try:
                callback(*args)
            except TypeError:
                raise original_error
