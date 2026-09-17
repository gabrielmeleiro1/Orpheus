"""Launch background tools without opening a Windows console."""

import subprocess
import sys


def background_process_kwargs() -> dict[str, int]:
    """Preserve pipes and process ownership while suppressing console windows."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}
