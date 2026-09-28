"""Configure Windows Hub cache symlinks before importing huggingface_hub."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import threading
import uuid

_configured = False
_configure_lock = threading.Lock()


def configure_hf_cache() -> None:
    """Probe once before Hub's parallel downloads can race its symlink check.

    Keep this module standard-library-only: Hub reads these environment values
    at import time. Spawned download workers inherit the disabled setting.
    """
    global _configured
    if sys.platform != "win32":
        return
    with _configure_lock:
        if _configured:
            return
        if "HF_HUB_DISABLE_SYMLINKS" not in os.environ:
            try:
                # Match Hub's cache precedence without importing its constants.
                default_home = os.path.join(os.path.expanduser("~"), ".cache")
                hf_home = os.path.expandvars(
                    os.path.expanduser(
                        os.environ.get(
                            "HF_HOME",
                            os.path.join(
                                os.environ.get("XDG_CACHE_HOME", default_home), "huggingface"
                            ),
                        )
                    )
                )
                cache = os.environ.get(
                    "HF_HUB_CACHE",
                    os.environ.get(
                        "HUGGINGFACE_HUB_CACHE",
                        os.path.join(hf_home, "hub"),
                    ),
                )
                cache_dir = Path(os.path.expandvars(os.path.expanduser(cache))).resolve()
                cache_dir.mkdir(parents=True, exist_ok=True)
                # tempfile.mkdtemp retries PermissionError on Windows when the
                # parent exists, which can stall startup in a restricted cache.
                probe_dir = cache_dir / f".orpheus-symlink-{uuid.uuid4().hex}"
                probe_dir.mkdir()
                source = probe_dir / "source"
                link = probe_dir / "link"
                try:
                    source.write_bytes(b"orpheus")
                    os.symlink(source.name, link)
                    if not link.is_symlink() or link.read_bytes() != b"orpheus":
                        raise OSError("Hub cache symlink probe could not be verified")
                finally:
                    link.unlink(missing_ok=True)
                    source.unlink(missing_ok=True)
                    probe_dir.rmdir()
            except (OSError, ValueError, RuntimeError, NotImplementedError):
                # Permissions, inaccessible caches, and uncertain probe results
                # all use Hub's supported copy/move fallback, including retries
                # where a blob exists but its snapshot file is missing.
                os.environ["HF_HUB_DISABLE_SYMLINKS"] = "1"
                os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
        # Direct runtime users may have imported Hub before this bootstrap.
        constants = sys.modules.get("huggingface_hub.constants")
        if constants is not None:
            for name in ("HF_HUB_DISABLE_SYMLINKS", "HF_HUB_DISABLE_SYMLINKS_WARNING"):
                if name in os.environ:
                    setattr(constants, name, os.environ[name].upper() in {"1", "ON", "YES", "TRUE"})
        _configured = True
