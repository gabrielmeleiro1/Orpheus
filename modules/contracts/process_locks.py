"""Standard-library process locks shared by storage and pre-install setup."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import time
from typing import BinaryIO, Callable


def open_process_lock(
    path: Path, *, blocking: bool, windows: bool | None = None,
    on_open: Callable[[BinaryIO], None] | None = None,
) -> BinaryIO:
    """Lock a persistent inode; closing the handle releases ownership."""
    handle = path.open("a+b")
    try:
        if on_open is not None:
            on_open(handle)
        if windows is None:
            windows = os.name == "nt"
        if windows:
            import msvcrt

            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if not blocking or exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except OSError as exc:
        handle.close()
        if not blocking and exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise BlockingIOError(exc.errno, exc.strerror) from exc
        raise
    except BaseException:
        handle.close()
        raise
    return handle
