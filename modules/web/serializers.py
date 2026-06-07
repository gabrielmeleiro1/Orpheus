"""Serialization helpers for web responses."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

def _iso(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def book_summary_to_dict(book) -> dict:
    return {
        "id": book.id,
        "title": book.title,
        "author": book.author,
        "total_chapters": book.total_chapters,
        "completed_chapters": book.completed_chapters,
        "created_at": _iso(book.created_at),
        "source_path": getattr(book, "source_path", None),
        "conversion_mode": getattr(getattr(book, "conversion_mode", None), "value", getattr(book, "conversion_mode", None)),
        "tags": getattr(book, "tags", "") or "",
        "favorite": bool(getattr(book, "favorite", False)),
        "source_available": getattr(book, "source_available", None),
    }


def book_to_dict(book: dict) -> dict:
    payload = dict(book)
    payload["created_at"] = _iso(payload.get("created_at"))
    return payload


def job_status_value(job) -> str:
    status = getattr(job, "status", None)
    if hasattr(status, "value"):
        return status.value
    return str(status) if status is not None else "unknown"


def job_payload(job) -> dict:
    return {
        "id": job.id,
        "book_id": getattr(job, "book_id", None),
        "status": job_status_value(job),
        "error": getattr(job, "error", None),
    }


def safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
