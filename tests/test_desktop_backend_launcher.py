"""Contract tests for the desktop backend launcher CLI."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import stat
import sys
import threading
import urllib.request
from pathlib import Path
from typing import TextIO

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _build_env(env_overrides: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{PROJECT_ROOT}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(PROJECT_ROOT)
    )
    if env_overrides:
        env.update(env_overrides)
    return env


def _readline_with_timeout(stream: TextIO, timeout_seconds: float) -> str | None:
    result: dict[str, str] = {}

    def _read() -> None:
        result["line"] = stream.readline()

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    reader.join(timeout_seconds)
    if reader.is_alive():
        return None
    return result.get("line")


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _launch_desktop_backend(
    *args: str,
    cwd: Path | None = None,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "apps.launchers.desktop_backend", *args],
        cwd=str(cwd) if cwd is not None else str(PROJECT_ROOT),
        env=_build_env(env_overrides),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _read_ready_payload(process: subprocess.Popen[str]) -> dict[str, object]:
    assert process.stdout is not None
    line = _readline_with_timeout(process.stdout, timeout_seconds=8.0)
    if line is None or not line.strip():
        return_code = process.poll()
        stdout = ""
        stderr = ""
        if return_code is not None:
            out, err = process.communicate(timeout=1)
            stdout = out
            stderr = err
        raise AssertionError(
            "Desktop backend did not emit a ready payload line. "
            f"return_code={return_code!r} stdout={stdout!r} stderr={stderr!r}"
        )

    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"Desktop backend emitted non-JSON payload: {line!r}") from exc


def test_desktop_backend_launcher_emits_ready_json_for_ephemeral_port(tmp_path: Path) -> None:
    process = _launch_desktop_backend("--host", "127.0.0.1", "--port", "0", cwd=tmp_path)
    try:
        payload = _read_ready_payload(process)
        assert payload["event"] == "ready"
        assert payload["host"] == "127.0.0.1"
        assert isinstance(payload["port"], int)
        assert payload["port"] > 0
        assert isinstance(payload["token"], str)
        assert payload["token"]

        with urllib.request.urlopen(
            f"http://{payload['host']}:{payload['port']}/health",
            timeout=4,
        ) as response:
            assert response.status == 200
            assert json.loads(response.read().decode("utf-8")) == {"status": "ok"}
    finally:
        _stop_process(process)


def test_desktop_backend_launcher_writes_ready_file_for_packaged_handoff(tmp_path: Path) -> None:
    ready_file = tmp_path / "runtime" / "backend-ready.json"
    process = _launch_desktop_backend(
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        cwd=tmp_path,
        env_overrides={"AUDIOBOOK_DESKTOP_READY_FILE": str(ready_file)},
    )
    try:
        payload = _read_ready_payload(process)

        assert ready_file.is_file()
        stored_payload = json.loads(ready_file.read_text(encoding="utf-8"))
        assert stored_payload == payload
        if os.name != "nt":
            assert stat.S_IMODE(ready_file.stat().st_mode) == 0o600
    finally:
        _stop_process(process)


def test_desktop_backend_launcher_exits_cleanly_on_sigterm(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX SIGTERM exit-code contract is not available on Windows")
    process = _launch_desktop_backend("--host", "127.0.0.1", "--port", "0", cwd=tmp_path)
    try:
        _read_ready_payload(process)
        process.terminate()
        exit_code = process.wait(timeout=5)
        assert exit_code == 0
    finally:
        _stop_process(process)


def test_desktop_backend_launcher_rejects_non_loopback_host() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "apps.launchers.desktop_backend", "--host", "0.0.0.0", "--port", "0"],
        cwd=PROJECT_ROOT,
        env=_build_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "loopback" in result.stderr.lower()


def test_desktop_backend_launcher_rejects_invalid_port_range() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "apps.launchers.desktop_backend", "--host", "127.0.0.1", "--port", "70000"],
        cwd=PROJECT_ROOT,
        env=_build_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "0-65535" in result.stderr


def test_desktop_backend_main_calls_multiprocessing_freeze_support(monkeypatch) -> None:
    from apps.launchers import desktop_backend

    call_order: list[str] = []

    def _fake_freeze_support() -> None:
        call_order.append("freeze_support")

    class _FakeParser:
        def parse_args(self, argv: list[str] | None) -> argparse.Namespace:
            call_order.append("parse_args")
            return argparse.Namespace(host="127.0.0.1", port=0)

    class _FakeServer:
        def __init__(self, config: object, *, ready_host: str, token: str) -> None:
            _ = config
            _ = ready_host
            _ = token
            call_order.append("server_init")
            self.ready_emitted = True

        def run(self) -> None:
            call_order.append("server_run")

    monkeypatch.setattr(desktop_backend.multiprocessing, "freeze_support", _fake_freeze_support)
    monkeypatch.setattr(desktop_backend, "build_parser", lambda: _FakeParser())
    monkeypatch.setattr(desktop_backend, "create_app", lambda **_: object())
    monkeypatch.setattr(desktop_backend, "_DesktopReadyServer", _FakeServer)

    exit_code = desktop_backend.main([])

    assert exit_code == 0
    assert call_order[0] == "freeze_support"
    assert "parse_args" in call_order
