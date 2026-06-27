"""Benchmark summary helpers for ingestion observability."""

from __future__ import annotations

from typing import Any


def skipped_sections_from_document(document: Any) -> list[dict[str, object]]:
    """Return serialized non-prose skips attached to a parsed document."""
    raw = getattr(document, "skipped_non_prose_sections", None)
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def skipped_sections_from_metrics(metrics: dict[str, Any]) -> list[dict[str, object]]:
    """Return non-prose skips recorded in benchmark parse metrics."""
    raw = metrics.get("skipped_non_prose_sections")
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]
