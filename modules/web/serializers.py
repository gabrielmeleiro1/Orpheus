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


def _status_value(status) -> str:
    if hasattr(status, "value"):
        return status.value
    return str(status) if status is not None else "unknown"


def job_status_value(job) -> str:
    return _status_value(getattr(job, "status", None))


def job_payload(job) -> dict:
    read_state = getattr(job, "read_state", None)
    if callable(read_state):
        job_id, book_id, status, error = read_state()
    else:
        job_id = job.id
        book_id = getattr(job, "book_id", None)
        status = getattr(job, "status", None)
        error = getattr(job, "error", None)

    return {
        "id": job_id,
        "book_id": book_id,
        "status": _status_value(status),
        "error": error,
    }


def safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
