"""File synchronization and atomic replacement without application dependencies."""

from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, IO, Literal, TypedDict, Unpack


def unlink_file(path: Path) -> None:
    """Retry Windows deletion once for a read-only regular file."""
    try:
        path.unlink()
    except PermissionError:
        if os.name != "nt" or path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            raise
        os.chmod(path, stat.S_IWRITE)
        path.unlink()


def sync_file(path: Path) -> None:
    """Flush an existing file through a writable handle required by Windows."""
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def sync_directory(path: Path) -> None:
    """Sync a directory on platforms supporting directory handles."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_directory_if_openable(path: Path) -> None:
    """Attempt directory sync on every platform, tolerating only open failures."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _WriteOptions(TypedDict, total=False):
    temporary_path: Path | None
    before_write: Callable[[Path], None] | None
    permissions: int | None
    replace: Callable[[Path, Path], object] | None
    sync_parent: Callable[[Path], None]
    cleanup: Literal["always", "before_replace", "none"]


def _atomic_write(
    path: Path,
    write: Callable[[IO[Any]], object],
    *,
    encoding: str | None,
    temporary_path: Path | None = None,
    before_write: Callable[[Path], None] | None = None,
    permissions: int | None = None,
    replace: Callable[[Path, Path], object] | None = None,
    sync_parent: Callable[[Path], None] = sync_directory,
    cleanup: Literal["always", "before_replace", "none"] = "always",
) -> None:
    pending = temporary_path
    replaced = False
    try:
        mode = "wb" if encoding is None else "w"
        if pending is None:
            stream = tempfile.NamedTemporaryFile(
                mode=mode, encoding=encoding, dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            )
            pending = Path(stream.name)
        else:
            stream = pending.open(mode, encoding=encoding)
        with stream:
            if before_write is not None:
                before_write(pending)
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        if permissions is not None:
            os.chmod(pending, permissions)
        if replace is None:
            os.replace(pending, path)
        else:
            replace(pending, path)
        replaced = True
        sync_parent(path.parent)
    finally:
        if pending is not None:
            if cleanup == "always" and pending.exists():
                pending.unlink(missing_ok=True)
            elif cleanup == "before_replace" and not replaced:
                pending.unlink(missing_ok=True)


def atomic_write_bytes(path: Path, payload: bytes, **options: Unpack[_WriteOptions]) -> None:
    """Sync and replace bytes using a sibling temp file; the parent must exist.

    By default a private, uniquely named temp file is removed on every exit.
    Callers with fixed recovery filenames can supply ``temporary_path`` and
    retain it with ``cleanup='none'``. ``before_write`` runs on the opened temp
    before any payload is written; ``permissions`` is applied after file sync.
    ``cleanup='before_replace'`` relinquishes the temp name after replacement;
    ``'always'`` also checks that name after the parent directory is synced.
    ``sync_parent`` selects the caller's directory durability policy.
    ``replace`` can retain a caller's replacement entry point.
    """
    _atomic_write(path, lambda stream: stream.write(payload), encoding=None, **options)


def atomic_write_text(
    path: Path, payload: str, *, encoding: str = "utf-8", **options: Unpack[_WriteOptions],
) -> None:
    """Replace text using native newline translation and the byte writer's policies."""
    _atomic_write(path, lambda stream: stream.write(payload), encoding=encoding, **options)


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    encoding: str = "utf-8",
    indent: int | str | None = None,
    sort_keys: bool = False,
    ensure_ascii: bool = True,
    separators: tuple[str, str] | None = None,
    trailing_newline: bool = False,
    **options: Unpack[_WriteOptions],
) -> None:
    """Stream JSON with explicit formatting, then sync and replace the file."""
    def write(stream: IO[str]) -> None:
        json.dump(
            payload, stream, indent=indent, sort_keys=sort_keys,
            ensure_ascii=ensure_ascii, separators=separators,
        )
        if trailing_newline:
            stream.write("\n")

    _atomic_write(path, write, encoding=encoding, **options)
