"""Helpers for opaque uploaded-source handles."""

from __future__ import annotations

from pathlib import Path

UPLOAD_HANDLE_PREFIX = "upload:"
UPLOAD_ROOT_NAME = "web_uploads"


def upload_root(temp_dir: Path) -> Path:
    """Return the server-owned upload root for source files."""
    return temp_dir / UPLOAD_ROOT_NAME


def create_source_handle(stored_name: str) -> str:
    """Build the opaque handle exposed to API clients."""
    return f"{UPLOAD_HANDLE_PREFIX}{stored_name}"


def resolve_source_reference(source_ref: str, temp_dir: Path) -> Path:
    """Resolve either a raw path or an opaque upload handle to a local path."""
    raw_ref = str(source_ref or "").strip()
    if not raw_ref:
        raise ValueError("source path required")

    if not raw_ref.startswith(UPLOAD_HANDLE_PREFIX):
        return Path(raw_ref)

    stored_name = raw_ref.removeprefix(UPLOAD_HANDLE_PREFIX)
    if not stored_name:
        raise ValueError("uploaded source handle is invalid")

    candidate = Path(stored_name)
    if candidate.name != stored_name or candidate != Path(candidate.name):
        raise ValueError("uploaded source handle is invalid")

    resolved = upload_root(temp_dir) / stored_name
    if not resolved.exists():
        raise ValueError("uploaded source handle is invalid or expired")
    return resolved
