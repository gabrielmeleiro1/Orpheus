"""Normalizers for values crossing the worker boundary.

Workers may hand back dataclasses or plain dicts depending on how they were
pickled; these helpers coerce both shapes without touching manager state.
"""

from __future__ import annotations

from typing import Any

from .models import ChapterResult
from .worker_errors import WorkerStartupStatus


def _result_has_error(result: ChapterResult) -> bool:
    return bool(result.error)


def _normalize_chapter_result(result: Any) -> ChapterResult:
    if isinstance(result, ChapterResult):
        return result
    if isinstance(result, dict):
        return ChapterResult(**result)
    raise TypeError(f"worker returned unsupported result type: {type(result).__name__}")


def _chapter_error_result(chapter: Any, idx: int, error: str) -> ChapterResult:
    chapter_number = getattr(chapter, "number", idx + 1)
    chapter_title = getattr(chapter, "title", f"Chapter {idx + 1}")
    return ChapterResult(
        chapter_number=chapter_number,
        chapter_title=chapter_title,
        error=error,
    )


def _normalize_startup_status(result: Any) -> WorkerStartupStatus:
    if isinstance(result, WorkerStartupStatus):
        return result
    if isinstance(result, dict):
        return WorkerStartupStatus.from_dict(result)
    return WorkerStartupStatus(
        success=False,
        provider="unknown",
        device=None,
        error_type=type(result).__name__,
        message=f"unexpected startup probe result: {result!r}",
    )
