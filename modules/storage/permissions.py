"""Best-effort owner-only permissions for app-owned storage."""

from __future__ import annotations

import os
from pathlib import Path
import stat


def owner_only(path: Path, *, directory: bool = False) -> None:
    """Restrict an app-owned path, without affecting unsupported platforms."""
    if os.name == "nt":
        return
    try:
        if directory:
            os.chmod(path, 0o700)
        else:
            # Hide the file from other users without granting the owner write
            # access it lacks: a read-only library must stay read-only.
            os.chmod(path, stat.S_IMODE(os.stat(path).st_mode) & 0o600)
    except OSError:
        # Permissions are defense-in-depth; storage remains usable on unusual
        # filesystems where chmod is unavailable.
        return


def ensure_owner_only_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    owner_only(path, directory=True)
