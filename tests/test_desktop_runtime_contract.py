"""Desktop runtime contract tests for the upcoming Tauri + Python flow."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from typing import TextIO

import pytest

from modules.app.config import AppConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _readline_with_timeout(stream: TextIO, timeout_seconds: float) -> str | None:
    result: dict[str, str] = {}

    def _read() -> None:
        line = stream.readline()
        result["line"] = line

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
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    cwd: Path | None = None,
    env_overrides: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[str], dict[str, object]]:
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{PROJECT_ROOT}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(PROJECT_ROOT)
    )
    if env_overrides:
        env.update(env_overrides)

    process = subprocess.Popen(
        [sys.executable, "-m", "apps.launchers.desktop_backend", "--host", host, "--port", str(port)],
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
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
        _stop_process(process)
        raise AssertionError(
            "Desktop backend did not emit a ready payload line. "
            f"return_code={return_code!r} stdout={stdout!r} stderr={stderr!r}"
        )

    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        _stop_process(process)
        raise AssertionError(f"Desktop backend emitted non-JSON ready payload: {line!r}") from exc

    return process, payload


def _health_with_bearer_token(host: str, port: int, token: str) -> dict[str, object]:
    request = urllib.request.Request(
        f"http://{host}:{port}/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=4) as response:
        assert response.status == 200
        return json.loads(response.read().decode("utf-8"))


def _clear_desktop_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "AUDIOBOOK_DATA_DIR",
        "AUDIOBOOK_OUTPUT_DIR",
        "AUDIOBOOK_TEMP_DIR",
        "AUDIOBOOK_DB_PATH",
    ):
        monkeypatch.delenv(key, raising=False)


def test_desktop_backend_ready_payload_for_ephemeral_port(tmp_path: Path) -> None:
    process, payload = _launch_desktop_backend(host="127.0.0.1", port=0, cwd=tmp_path)
    try:
        assert payload["event"] == "ready"
        assert payload["host"] == "127.0.0.1"
        assert isinstance(payload["port"], int)
        assert payload["port"] > 0
        assert isinstance(payload["token"], str)
        assert payload["token"]
    finally:
        _stop_process(process)


def test_desktop_backend_binds_requested_loopback_port(tmp_path: Path) -> None:
    requested_port = _reserve_port()
    process, payload = _launch_desktop_backend(host="127.0.0.1", port=requested_port, cwd=tmp_path)
    try:
        assert payload["host"] == "127.0.0.1"
        assert payload["port"] == requested_port
    finally:
        _stop_process(process)


def test_desktop_backend_token_can_reach_health(tmp_path: Path) -> None:
    process, payload = _launch_desktop_backend(host="127.0.0.1", port=0, cwd=tmp_path)
    try:
        health = _health_with_bearer_token(
            host=str(payload["host"]),
            port=int(payload["port"]),
            token=str(payload["token"]),
        )
        assert health == {"status": "ok"}
    finally:
        _stop_process(process)


def test_desktop_runtime_paths_can_be_injected_without_cwd_change(tmp_path: Path) -> None:
    runtime_root = tmp_path / "desktop-runtime"
    data_dir = runtime_root / "state" / "data"
    output_dir = runtime_root / "state" / "output"
    temp_dir = runtime_root / "state" / "temp"
    db_path = data_dir / "audiobooks-desktop.db"
    working_dir = tmp_path / "working-dir"
    working_dir.mkdir(parents=True, exist_ok=True)

    process, _payload = _launch_desktop_backend(
        host="127.0.0.1",
        port=0,
        cwd=working_dir,
        env_overrides={
            "AUDIOBOOK_DATA_DIR": str(data_dir),
            "AUDIOBOOK_OUTPUT_DIR": str(output_dir),
            "AUDIOBOOK_TEMP_DIR": str(temp_dir),
            "AUDIOBOOK_DB_PATH": str(db_path),
        },
    )
    try:
        assert data_dir.is_dir()
        assert output_dir.is_dir()
        assert temp_dir.is_dir()
        assert db_path.parent == data_dir
        assert not (working_dir / "data").exists()
        assert not (working_dir / "output").exists()
        assert not (working_dir / "temp").exists()
    finally:
        _stop_process(process)


def test_app_config_uses_desktop_path_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_desktop_path_env(monkeypatch)
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))

    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", "~/.orpheus-desktop/data")
    monkeypatch.setenv("AUDIOBOOK_OUTPUT_DIR", "~/.orpheus-desktop/output")
    monkeypatch.setenv("AUDIOBOOK_TEMP_DIR", "~/.orpheus-desktop/temp")
    monkeypatch.setenv("AUDIOBOOK_DB_PATH", "~/.orpheus-desktop/data/runtime.db")

    config = AppConfig()

    expected_root = fake_home / ".orpheus-desktop"
    assert config.data_dir == expected_root / "data"
    assert config.output_dir == expected_root / "output"
    assert config.temp_dir == expected_root / "temp"
    assert config.db_path == expected_root / "data" / "runtime.db"
    assert config.data_dir.is_dir()
    assert config.output_dir.is_dir()
    assert config.temp_dir.is_dir()


def test_app_config_keeps_defaults_when_desktop_env_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_desktop_path_env(monkeypatch)
    monkeypatch.chdir(tmp_path)

    config = AppConfig()

    assert config.data_dir == Path("data")
    assert config.output_dir == Path("output")
    assert config.temp_dir == Path("temp")
    assert config.db_path == Path("data") / "audiobooks.db"
    assert (tmp_path / "data").is_dir()
    assert (tmp_path / "output").is_dir()
    assert (tmp_path / "temp").is_dir()


def test_app_config_does_not_override_explicit_constructor_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_desktop_path_env(monkeypatch)

    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "env-data"))
    monkeypatch.setenv("AUDIOBOOK_OUTPUT_DIR", str(tmp_path / "env-output"))
    monkeypatch.setenv("AUDIOBOOK_TEMP_DIR", str(tmp_path / "env-temp"))
    monkeypatch.setenv("AUDIOBOOK_DB_PATH", str(tmp_path / "env-data" / "env.db"))

    explicit_data_dir = tmp_path / "explicit-data"
    explicit_output_dir = tmp_path / "explicit-output"
    explicit_temp_dir = tmp_path / "explicit-temp"
    explicit_db_path = explicit_data_dir / "explicit.db"

    config = AppConfig(
        data_dir=explicit_data_dir,
        output_dir=explicit_output_dir,
        temp_dir=explicit_temp_dir,
        db_path=explicit_db_path,
    )

    assert config.data_dir == explicit_data_dir
    assert config.output_dir == explicit_output_dir
    assert config.temp_dir == explicit_temp_dir
    assert config.db_path == explicit_db_path
    assert explicit_data_dir.is_dir()
    assert explicit_output_dir.is_dir()
    assert explicit_temp_dir.is_dir()

