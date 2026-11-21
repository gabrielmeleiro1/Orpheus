"""Bounded subprocess runner for local release qualification."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .evidence import StreamingSecretRedactor, redact_secrets
from .model import _utc_now_iso


@dataclass(frozen=True)
class CommandSpec:
    label: str
    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str] | None = None
    timeout_seconds: float | None = None

    def __init__(
        self,
        *,
        label: str,
        argv: Sequence[str],
        cwd: str,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        if isinstance(argv, (str, bytes)):
            raise TypeError("argv must be a sequence of arguments, not a shell string")
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "argv", tuple(str(part) for part in argv))
        object.__setattr__(self, "cwd", str(cwd))
        object.__setattr__(self, "env", dict(env) if env is not None else None)
        object.__setattr__(self, "timeout_seconds", timeout_seconds)


@dataclass(frozen=True)
class CommandResult:
    label: str
    argv: tuple[str, ...]
    started_at: str
    ended_at: str
    duration_seconds: float
    exit_code: int
    timed_out: bool
    log_path: str


class _BoundedLog:
    def __init__(self, path: Path, max_bytes: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self._written = 0
        self._truncated = False
        self._handle = path.open("wb")
        self._saw_stdout = False
        self._stderr_header_written = False

    def write(self, data: bytes | str, *, stream: str) -> None:
        if self._truncated:
            return
        raw = data.encode("utf-8", errors="replace") if isinstance(data, str) else data
        if not raw:
            return
        if stream == "stdout":
            self._saw_stdout = True
        elif stream == "stderr" and self._saw_stdout and not self._stderr_header_written:
            self._write_bytes(b"\n--- stderr ---\n", stream=stream)
            self._stderr_header_written = True
            if self._truncated:
                return
        elif stream == "stderr":
            self._stderr_header_written = True
        self._write_bytes(raw, stream=stream)

    def _write_bytes(self, data: bytes, *, stream: str) -> None:
        remaining = self.max_bytes - self._written
        if remaining <= 0:
            self._mark_truncated(stream)
            return
        chunk = data[:remaining]
        # Avoid splitting a multibyte UTF-8 sequence at the budget edge.
        while chunk:
            try:
                chunk.decode("utf-8")
                break
            except UnicodeDecodeError:
                chunk = chunk[:-1]
        if not chunk:
            self._mark_truncated(stream)
            return
        self._handle.write(chunk)
        self._handle.flush()
        self._written += len(chunk)
        if len(data) > len(chunk):
            self._mark_truncated(stream)

    def _mark_truncated(self, stream: str) -> None:
        if self._truncated:
            return
        marker = f"\n[{stream} truncated: log exceeded {self.max_bytes} bytes]\n"
        self._handle.write(marker.encode("utf-8"))
        self._handle.flush()
        self._truncated = True

    def close(self) -> None:
        self._handle.close()


def _sanitize_env(
    env: Mapping[str, str] | None,
    env_allowlist: Sequence[str],
) -> dict[str, str]:
    source = env if env is not None else os.environ
    allow = set(env_allowlist)
    return {key: str(source[key]) for key in allow if key in source}


# On timeout the group gets SIGTERM first so commands can run their cleanup
# (the bundled smoke detaches its DMG); SIGKILL follows after this grace.
TERMINATE_GRACE_SECONDS = 30.0


def _kill_process_group(
    process: subprocess.Popen[bytes], sig: signal.Signals = signal.SIGKILL
) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def _stream_command_output(
    process: subprocess.Popen[bytes],
    *,
    bounded: _BoundedLog,
    username: str | None,
    timeout_seconds: float | None,
) -> tuple[bool, int]:
    assert process.stdout is not None
    assert process.stderr is not None

    timed_out = False
    deadline = (
        None if timeout_seconds is None else time.monotonic() + float(timeout_seconds)
    )
    drain_deadline: float | None = None
    kill_at: float | None = None
    redactors = {
        "stdout": StreamingSecretRedactor(username=username),
        "stderr": StreamingSecretRedactor(username=username),
    }

    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        open_streams = {"stdout", "stderr"}

        while open_streams:
            now = time.monotonic()
            if kill_at is not None and (now >= kill_at or process.poll() is not None):
                # Grace elapsed, or the leader finished its cleanup: stop the whole group.
                _kill_process_group(process)
                kill_at = None
                drain_deadline = now + 1.0
            if process.poll() is not None and drain_deadline is None:
                # Process exited (or was killed): keep reading until EOF or grace elapses.
                drain_deadline = now + (1.0 if timed_out else 0.5)
            select_timeout: float | None
            if drain_deadline is not None:
                select_timeout = max(0.0, drain_deadline - now)
            elif kill_at is not None:
                # Poll so a leader that exits while others hold the pipes is noticed.
                select_timeout = min(0.1, max(0.0, kill_at - now))
            elif deadline is not None:
                select_timeout = max(0.0, deadline - now)
            else:
                select_timeout = None

            events = selector.select(select_timeout)
            now = time.monotonic()

            if not events:
                if (
                    not timed_out
                    and deadline is not None
                    and now >= deadline
                    and process.poll() is None
                ):
                    timed_out = True
                    _kill_process_group(process, signal.SIGTERM)
                    kill_at = time.monotonic() + TERMINATE_GRACE_SECONDS
                    continue
                if drain_deadline is not None and now >= drain_deadline:
                    break
                continue

            for key, _ in events:
                stream = str(key.data)
                chunk = os.read(key.fd, 65_536)
                if not chunk:
                    pending = redactors[stream].flush()
                    if pending:
                        bounded.write(pending, stream=stream)
                    selector.unregister(key.fileobj)
                    open_streams.discard(stream)
                    continue
                redacted = redactors[stream].feed(
                    chunk.decode("utf-8", errors="replace")
                )
                if redacted:
                    bounded.write(redacted, stream=stream)

    for stream_name, redactor in redactors.items():
        pending = redactor.flush()
        if pending:
            bounded.write(pending, stream=stream_name)

    try:
        exit_code = process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        exit_code = process.wait(timeout=1)

    if timed_out:
        _kill_process_group(process)
        if exit_code == 0:
            exit_code = -9
        elif exit_code is None:
            exit_code = -9
    return timed_out, int(exit_code)


def run_command(
    spec: CommandSpec,
    *,
    log_dir: Path,
    env_allowlist: Sequence[str] = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER"),
    max_log_bytes: int = 1_000_000,
    username: str | None = None,
) -> CommandResult:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_name = f"{spec.label}.log"
    log_path = log_dir / log_name
    bounded = _BoundedLog(log_path, max_bytes=max_log_bytes)

    sanitized_env = _sanitize_env(spec.env, env_allowlist)
    started_at = _utc_now_iso()
    started_mono = time.monotonic()

    process = subprocess.Popen(
        list(spec.argv),
        cwd=spec.cwd,
        env=sanitized_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
    )
    try:
        timed_out, exit_code = _stream_command_output(
            process,
            bounded=bounded,
            username=username,
            timeout_seconds=spec.timeout_seconds,
        )
    finally:
        if process.poll() is None:
            _kill_process_group(process)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        for pipe in (process.stdout, process.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()
        bounded.close()

    ended_at = _utc_now_iso()
    duration = time.monotonic() - started_mono
    sanitized_argv = tuple(
        redact_secrets(part, username=username) for part in spec.argv
    )
    return CommandResult(
        label=spec.label,
        argv=sanitized_argv,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration,
        exit_code=exit_code,
        timed_out=timed_out,
        log_path=f"commands/{log_name}",
    )
