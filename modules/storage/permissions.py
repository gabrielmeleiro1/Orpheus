"""Best-effort owner-only permissions for app-owned storage."""

from __future__ import annotations

import os
from pathlib import Path


def owner_only(path: Path, *, directory: bool = False) -> None:
    """Restrict an app-owned path, without affecting unsupported platforms."""
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except OSError:
        # Permissions are defense-in-depth; storage remains usable on unusual
        # filesystems where chmod is unavailable.
        return


def ensure_owner_only_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    owner_only(path, directory=True)
