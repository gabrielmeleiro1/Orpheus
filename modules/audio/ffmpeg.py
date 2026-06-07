"""FFmpeg executable discovery helpers."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Iterator, Sequence

from ..errors import FFmpegNotFoundError

_SUPPORTED_BINARIES = {"ffmpeg", "ffprobe"}
_COMMON_BINARY_DIRS = (
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
    Path("/usr/bin"),
    Path("/bin"),
    Path("/opt/local/bin"),
)
_BINARY_ENV_VARS = {
    "ffmpeg": ("AUDIOBOOK_FFMPEG_PATH", "FFMPEG_PATH"),
    "ffprobe": ("AUDIOBOOK_FFPROBE_PATH", "FFPROBE_PATH"),
}
_DEFAULT_TIMEOUT_ENV_VAR = "AUDIOBOOK_FFMPEG_TIMEOUT_SECONDS"
_DEFAULT_TIMEOUT_SECONDS = 2 * 60 * 60
_KILL_WAIT_SECONDS = 5.0


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _configured_binary(binary_name: str) -> str | None:
    for env_var in _BINARY_ENV_VARS[binary_name]:
        configured = os.environ.get(env_var)
        if not configured:
            continue

        configured_path = Path(configured).expanduser()
        if _is_executable_file(configured_path):
            return str(configured_path)

        resolved = shutil.which(configured)
        if resolved:
            return resolved

    return None


def _bundled_binary_dirs() -> tuple[Path, ...]:
    candidate_dirs: list[Path] = []

    executable = Path(sys.executable).expanduser()
    candidate_dirs.append(executable.parent / "bin")
    candidate_dirs.append(executable.parent / "_internal" / "bin")

    pyinstaller_root = getattr(sys, "_MEIPASS", None)
    if pyinstaller_root:
        meipass = Path(str(pyinstaller_root)).expanduser()
        candidate_dirs.append(meipass / "bin")
        candidate_dirs.append(meipass.parent / "bin")

    unique_dirs: list[Path] = []
    seen: set[str] = set()
    for directory in candidate_dirs:
        key = str(directory)
        if key not in seen:
            seen.add(key)
            unique_dirs.append(directory)
    return tuple(unique_dirs)


def _binary_names(binary_name: str) -> tuple[str, ...]:
    if os.name == "nt":
        return (binary_name, f"{binary_name}.exe")
    return (binary_name,)


def _resolve_from_directories(binary_name: str, directories: Sequence[Path]) -> str | None:
    for directory in directories:
        for name in _binary_names(binary_name):
            candidate = directory / name
            if _is_executable_file(candidate):
                return str(candidate)
    return None


def resolve_ffmpeg_binary(binary_name: str) -> str | None:
    """Return an executable path for an FFmpeg-family binary."""
    if binary_name not in _SUPPORTED_BINARIES:
        raise ValueError(f"Unsupported FFmpeg binary: {binary_name}")

    configured = _configured_binary(binary_name)
    if configured:
        return configured

    bundled = _resolve_from_directories(binary_name, _bundled_binary_dirs())
    if bundled:
        return bundled

    resolved = shutil.which(binary_name)
    if resolved:
        return resolved

    return _resolve_from_directories(binary_name, _COMMON_BINARY_DIRS)


def require_ffmpeg_binary(binary_name: str) -> str:
    """Return an executable path or raise the app's FFmpeg-not-found error."""
    resolved = resolve_ffmpeg_binary(binary_name)
    if resolved is None:
        raise FFmpegNotFoundError()
    return resolved


def ffmpeg_timeout_seconds(timeout_seconds: float | None = None) -> float:
    """Return a positive timeout for FFmpeg-family subprocess calls."""
    if timeout_seconds is not None:
        return max(0.001, float(timeout_seconds))

    configured = os.environ.get(_DEFAULT_TIMEOUT_ENV_VAR)
    if configured:
        try:
            return max(0.001, float(configured))
        except ValueError:
            pass
    return float(_DEFAULT_TIMEOUT_SECONDS)


def run_ffmpeg_command(
    cmd: Sequence[str],
    *,
    failure_message: str,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an FFmpeg-family command with bounded execution and stderr handling."""
    timeout = ffmpeg_timeout_seconds(timeout_seconds)
    try:
        result = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{failure_message}: timed out after {timeout:g}s") from exc

    if result.returncode != 0:
        stderr = result.stderr or ""
        raise RuntimeError(f"{failure_message}: {stderr}")
    return result


def wait_for_ffmpeg_process(
    process: subprocess.Popen,
    *,
    failure_message: str,
    timeout_seconds: float | None = None,
) -> int:
    """Wait for a streaming FFmpeg process and kill it if it exceeds the timeout."""
    timeout = ffmpeg_timeout_seconds(timeout_seconds)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        kill_ffmpeg_process(process)
        raise RuntimeError(f"{failure_message}: timed out after {timeout:g}s") from exc


def kill_ffmpeg_process(process: subprocess.Popen) -> None:
    """Kill an FFmpeg process and bound the post-kill wait."""
    with contextlib.suppress(Exception):
        process.kill()
    with contextlib.suppress(Exception):
        process.wait(timeout=_KILL_WAIT_SECONDS)


@contextlib.contextmanager
def atomic_output_path(output_path: Path | str) -> Iterator[Path]:
    """
    Yield a same-directory temporary output path and atomically replace the final path.

    The temp path is removed on failure, so failed or timed-out FFmpeg calls do not
    leave partial data at the public output path.
    """
    final_path = Path(output_path)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = final_path.with_name(
        f".{final_path.name}.{uuid.uuid4().hex}.tmp{final_path.suffix}"
    )
    try:
        yield temp_path
        if temp_path.is_file():
            temp_path.replace(final_path)
        else:
            raise RuntimeError(f"FFmpeg command did not create expected output: {temp_path}")
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()
        raise
