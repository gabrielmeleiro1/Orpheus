"""Helpers for opaque uploaded-source handles."""

from __future__ import annotations

from pathlib import Path

UPLOAD_HANDLE_PREFIX = "upload:"
UPLOAD_ROOT_NAME = "web_uploads"


def upload_root(temp_dir: Path) -> Path:
    """Return the server-owned upload root for source files."""
    return temp_dir / UPLOAD_ROOT_NAME


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
    # Handles are issued only for regular files created by the upload service.
    # Reject links and directories so a stale/pre-planted entry cannot turn an
    # opaque upload reference into an arbitrary filesystem read.
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError("uploaded source handle is invalid or expired")
    return resolved
