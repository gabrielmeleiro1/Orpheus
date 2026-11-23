"""FFmpeg executable discovery helpers."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Iterator, Sequence

from ..errors import FFmpegNotFoundError
from ..subprocess_utils import background_process_kwargs

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
# Used only when the audio length of a job is unknown.
_DEFAULT_TIMEOUT_SECONDS = 2 * 60 * 60
# With a known audio length the deadline scales with it. Encoding, muxing, and
# probing 24 kHz mono speech run at hundreds of times real time, so allowing
# half of real time (and never less than two minutes) only trips on an FFmpeg
# that has genuinely stalled, while a short chapter no longer waits two hours.
_TIMEOUT_FLOOR_SECONDS = 120.0
_TIMEOUT_SECONDS_PER_AUDIO_SECOND = 0.5
_KILL_WAIT_SECONDS = 5.0
# FFmpeg prints its banner and stream details first and the cause last; keep
# failure messages (which reach job errors) bounded to that tail.
_STDERR_EXCERPT_CHARS = 2000


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


def ffmpeg_failure(failure_message: str, stderr: str | None) -> RuntimeError:
    """Build a bounded failure error from an FFmpeg-family stderr capture."""
    detail = (stderr or "").strip()
    if len(detail) > _STDERR_EXCERPT_CHARS:
        detail = "..." + detail[-_STDERR_EXCERPT_CHARS:]
    return RuntimeError(f"{failure_message}: {detail}")


def ffmpeg_timeout_seconds(
    timeout_seconds: float | None = None, *, audio_seconds: float | None = None,
) -> float:
    """Return a positive timeout for FFmpeg-family subprocess calls.

    Precedence: an explicit ``timeout_seconds``, then the
    ``AUDIOBOOK_FFMPEG_TIMEOUT_SECONDS`` override, then a deadline proportional
    to ``audio_seconds`` (the length of audio the call reads or writes), and
    finally a fixed default when that length is unknown.
    """
    if timeout_seconds is not None:
        return max(0.001, float(timeout_seconds))

    configured = os.environ.get(_DEFAULT_TIMEOUT_ENV_VAR)
    if configured:
        try:
            return max(0.001, float(configured))
        except ValueError:
            pass
    if audio_seconds is not None and audio_seconds >= 0:
        return max(_TIMEOUT_FLOOR_SECONDS, float(audio_seconds) * _TIMEOUT_SECONDS_PER_AUDIO_SECOND)
    return float(_DEFAULT_TIMEOUT_SECONDS)


def wav_duration_seconds(path: Path | str) -> float | None:
    """Read a PCM WAV's duration from its header, or None when it is not one."""
    import wave

    try:
        with wave.open(str(path), "rb") as wav_file:
            rate = wav_file.getframerate()
            return wav_file.getnframes() / rate if rate > 0 else None
    except (OSError, EOFError, wave.Error):
        return None


def run_ffmpeg_command(
    cmd: Sequence[str],
    *,
    failure_message: str,
    timeout_seconds: float | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    audio_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an FFmpeg-family command with bounded execution and stderr handling."""
    timeout = ffmpeg_timeout_seconds(timeout_seconds, audio_seconds=audio_seconds)
    if cancel_requested is not None:
        process = subprocess.Popen(
            list(cmd),
            **background_process_kwargs(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            text=True,
        )
        deadline = time.monotonic() + timeout
        if cancel_requested():
            abandon_ffmpeg_process(process, None)
            raise RuntimeError(f"{failure_message}: cancelled")
        communicate_result: list[tuple[str | None, str | None]] = []
        collect_error: list[BaseException] = []

        def collect_output() -> None:
            try:
                communicate_result.append(process.communicate())
            except BaseException as exc:  # surfaced on the caller thread below
                collect_error.append(exc)

        collector = threading.Thread(target=collect_output, daemon=True)
        collector.start()
        while collector.is_alive():
            if cancel_requested():
                abandon_ffmpeg_process(process, collector)
                raise RuntimeError(f"{failure_message}: cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                abandon_ffmpeg_process(process, collector)
                raise RuntimeError(f"{failure_message}: timed out after {timeout:g}s")
            collector.join(timeout=min(0.1, remaining))
        if collect_error:
            abandon_ffmpeg_process(process, collector)
            raise RuntimeError(f"{failure_message}: {collect_error[0]}") from collect_error[0]
        stdout, stderr = communicate_result[0]
        return_code = int(process.returncode or 0)
        if return_code != 0:
            raise ffmpeg_failure(failure_message, stderr)
        return subprocess.CompletedProcess(list(cmd), return_code, stdout, stderr)
    try:
        result = subprocess.run(
            list(cmd),
            **background_process_kwargs(),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{failure_message}: timed out after {timeout:g}s") from exc

    if result.returncode != 0:
        raise ffmpeg_failure(failure_message, result.stderr)
    return result


def wait_for_ffmpeg_process(
    process: subprocess.Popen,
    *,
    failure_message: str,
    timeout_seconds: float | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> int:
    """Wait for a streaming FFmpeg process and kill it if it exceeds the timeout."""
    timeout = ffmpeg_timeout_seconds(timeout_seconds)
    if cancel_requested is None:
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            kill_ffmpeg_process(process)
            raise RuntimeError(f"{failure_message}: timed out after {timeout:g}s") from exc

    deadline = time.monotonic() + timeout
    while True:
        if cancel_requested():
            kill_ffmpeg_process(process)
            raise RuntimeError(f"{failure_message}: cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            kill_ffmpeg_process(process)
            raise RuntimeError(f"{failure_message}: timed out after {timeout:g}s")
        try:
            return process.wait(timeout=min(0.1, remaining))
        except subprocess.TimeoutExpired:
            continue


def kill_ffmpeg_process(process: subprocess.Popen) -> None:
    """Kill an FFmpeg process and bound the post-kill wait."""
    with contextlib.suppress(Exception):
        process.kill()
    with contextlib.suppress(Exception):
        process.wait(timeout=_KILL_WAIT_SECONDS)


def abandon_ffmpeg_process(
    process: subprocess.Popen, reader: threading.Thread | None,
) -> None:
    """Kill a process, give its pipe reader a bounded join, then close its pipes.

    Pipes are closed only once no reader thread is still using them; a reader
    that outlives the bounded join is a daemon and is left to finish on EOF.
    """
    kill_ffmpeg_process(process)
    if reader is not None:
        reader.join(timeout=_KILL_WAIT_SECONDS)
        if reader.is_alive():
            return
    for name in ("stdin", "stdout", "stderr"):
        pipe = getattr(process, name, None)
        if pipe is not None:
            with contextlib.suppress(Exception):
                pipe.close()


def cleanup_abandoned_atomic_outputs(directory: Path | str) -> None:
    """Remove private encode outputs after their owning processes have stopped."""
    directory = Path(directory)
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        if re.fullmatch(r"\.[0-9a-f]{32}\.tmp\.[^.]+", path.name) and path.is_file():
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


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
        f".{uuid.uuid4().hex}.tmp{final_path.suffix}"
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
