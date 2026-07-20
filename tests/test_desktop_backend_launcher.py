"""Contract tests for the desktop backend launcher CLI."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import stat
import sys
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import TextIO

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _skip_if_loopback_bind_unavailable() -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
    except PermissionError as exc:
        pytest.skip(f"loopback bind unavailable: {exc}")


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
    _skip_if_loopback_bind_unavailable()
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
    _skip_if_loopback_bind_unavailable()
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
    _skip_if_loopback_bind_unavailable()
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

    def _fake_configure_frozen_espeak_runtime() -> None:
        call_order.append("configure_frozen_espeak_runtime")

    def _fake_umask(mask: int) -> int:
        assert mask == 0o077
        call_order.append("umask")
        return 0o022

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

    monkeypatch.setattr(
        desktop_backend,
        "configure_frozen_espeak_runtime",
        _fake_configure_frozen_espeak_runtime,
    )
    monkeypatch.setattr(desktop_backend.os, "umask", _fake_umask)
    monkeypatch.setattr(desktop_backend.multiprocessing, "freeze_support", _fake_freeze_support)
    monkeypatch.setattr(desktop_backend, "build_parser", lambda: _FakeParser())
    monkeypatch.setattr(desktop_backend, "create_app", lambda **_: object())
    monkeypatch.setattr(desktop_backend, "_DesktopReadyServer", _FakeServer)

    exit_code = desktop_backend.main([])

    assert exit_code == 0
    expected = ["configure_frozen_espeak_runtime", "freeze_support"]
    if os.name != "nt":
        expected = ["umask", *expected]
    assert call_order[: len(expected)] == expected
    assert "parse_args" in call_order


def test_configure_frozen_espeak_runtime_is_noop_when_not_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from phonemizer.backend.espeak.wrapper import EspeakWrapper

    from apps.launchers import desktop_backend

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(EspeakWrapper, "_ESPEAK_LIBRARY", "unchanged-library")
    monkeypatch.setattr(EspeakWrapper, "_ESPEAK_DATA_PATH", "unchanged-data")

    desktop_backend.configure_frozen_espeak_runtime()

    assert EspeakWrapper._ESPEAK_LIBRARY == "unchanged-library"
    assert EspeakWrapper._ESPEAK_DATA_PATH == "unchanged-data"


def test_configure_frozen_espeak_runtime_sets_phonemizer_wrapper_to_packaged_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frozen sidecar must configure phonemizer before TTS uses the dylib default path."""
    from phonemizer.backend.espeak.wrapper import EspeakWrapper

    package_dir = tmp_path / "_internal" / "espeakng_loader"
    data_dir = package_dir / "espeak-ng-data"
    library_path = package_dir / "libespeak-ng.dylib"
    data_dir.mkdir(parents=True)
    (data_dir / "phontab").write_text("phontab", encoding="utf-8")
    library_path.write_bytes(b"dylib")

    compile_time_library = (
        "/Users/runner/work/espeakng-loader/espeakng-loader/"
        "espeak-ng/_dynamic/lib/libespeak-ng.dylib"
    )
    compile_time_data = (
        "/Users/runner/work/espeakng-loader/espeakng-loader/"
        "espeak-ng/_dynamic/share/espeak-ng-data"
    )
    fake_loader = SimpleNamespace(
        get_library_path=lambda: compile_time_library,
        get_data_path=lambda: compile_time_data,
    )

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    monkeypatch.setitem(sys.modules, "espeakng_loader", fake_loader)
    monkeypatch.setattr(EspeakWrapper, "_ESPEAK_LIBRARY", None)
    monkeypatch.setattr(EspeakWrapper, "_ESPEAK_DATA_PATH", None)

    from apps.launchers import desktop_backend

    desktop_backend.configure_frozen_espeak_runtime()

    assert EspeakWrapper._ESPEAK_LIBRARY == str(library_path)
    assert EspeakWrapper._ESPEAK_DATA_PATH == str(data_dir)
    assert fake_loader.get_library_path() == str(library_path)
    assert fake_loader.get_data_path() == str(data_dir)


def test_configure_frozen_espeak_runtime_shortens_paths_for_libespeak_path_home_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """libespeak POSIX N_PATH_HOME is 160; long .app paths must be bridged.

    Absolute packaged paths inside Orpheus.app exceed that limit, so
    EspeakWrapper.set_data_path alone still makes espeak_Initialize fall back
    to the dylib's compile-time CI data path. Frozen config must expose a short
    usable data path (and ESPEAK_DATA_PATH) that still resolves to packaged data.
    """
    from phonemizer.backend.espeak.wrapper import EspeakWrapper

    # Keep this above POSIX N_PATH_HOME (160 filesystem bytes) after .../espeak-ng-data.
    deep_root = tmp_path.joinpath(*[f"n{i:02d}" for i in range(20)])
    package_dir = deep_root / "_internal" / "espeakng_loader"
    data_dir = package_dir / "espeak-ng-data"
    library_path = package_dir / "libespeak-ng.dylib"
    data_dir.mkdir(parents=True)
    (data_dir / "phontab").write_text("packaged-phontab", encoding="utf-8")
    library_path.write_bytes(b"dylib")
    assert len(os.fsencode(str(data_dir.resolve()))) >= 160

    fake_loader = SimpleNamespace(
        get_library_path=lambda: "wrong-library",
        get_data_path=lambda: "wrong-data",
    )

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(deep_root / "_internal"), raising=False)
    monkeypatch.setitem(sys.modules, "espeakng_loader", fake_loader)
    monkeypatch.setattr(EspeakWrapper, "_ESPEAK_LIBRARY", None)
    monkeypatch.setattr(EspeakWrapper, "_ESPEAK_DATA_PATH", None)
    monkeypatch.delenv("ESPEAK_DATA_PATH", raising=False)
    monkeypatch.delenv("PHONEMIZER_ESPEAK_DATA_PATH", raising=False)
    monkeypatch.delenv("PHONEMIZER_ESPEAK_LIBRARY", raising=False)

    from apps.launchers import desktop_backend

    # Keep the symlink bridge under tmp_path so the test leaves no user cache state.
    monkeypatch.setattr(
        desktop_backend,
        "_frozen_espeak_runtime_base",
        lambda: tmp_path / "e",
    )

    desktop_backend.configure_frozen_espeak_runtime()

    configured_data = Path(EspeakWrapper._ESPEAK_DATA_PATH)
    configured_library = Path(EspeakWrapper._ESPEAK_LIBRARY)
    env_data = Path(os.environ["ESPEAK_DATA_PATH"])

    assert len(os.fsencode(str(configured_data))) < 160
    assert len(os.fsencode(str(env_data))) < 160
    assert (configured_data / "phontab").read_text(encoding="utf-8") == "packaged-phontab"
    assert configured_data.resolve() == data_dir.resolve()
    assert configured_library == library_path
    assert fake_loader.get_data_path() == str(configured_data)
    assert os.environ["PHONEMIZER_ESPEAK_DATA_PATH"] == str(configured_data)
    assert os.environ["PHONEMIZER_ESPEAK_LIBRARY"] == str(library_path)


def test_configure_frozen_espeak_runtime_bridges_multibyte_paths_over_byte_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N_PATH_HOME is filesystem bytes; char count under 160 can still overflow.

    A multibyte Unicode packaged path may have fewer than 160 code points while
    os.fsencode(path) is at or above 160. The launcher must bridge that path.
    """
    from phonemizer.backend.espeak.wrapper import EspeakWrapper

    # UTF-8 "あ" is 3 bytes / 1 code point — crosses the byte limit under 160 chars.
    unit = "あ"
    suffix = Path("_internal") / "espeakng_loader" / "espeak-ng-data"
    data_dir: Path | None = None
    for count in range(1, 80):
        candidate = tmp_path / (unit * count) / suffix
        path_str = str(candidate.resolve())
        if len(path_str) < 160 and len(os.fsencode(path_str)) >= 160:
            data_dir = candidate
            break
    assert data_dir is not None, "could not build multibyte path under char limit over byte limit"

    package_dir = data_dir.parent
    library_path = package_dir / "libespeak-ng.dylib"
    data_dir.mkdir(parents=True)
    (data_dir / "phontab").write_text("packaged-phontab", encoding="utf-8")
    library_path.write_bytes(b"dylib")

    resolved = data_dir.resolve()
    assert len(str(resolved)) < 160
    assert len(os.fsencode(str(resolved))) >= 160

    fake_loader = SimpleNamespace(
        get_library_path=lambda: "wrong-library",
        get_data_path=lambda: "wrong-data",
    )

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(data_dir.parent.parent), raising=False)
    monkeypatch.setitem(sys.modules, "espeakng_loader", fake_loader)
    monkeypatch.setattr(EspeakWrapper, "_ESPEAK_LIBRARY", None)
    monkeypatch.setattr(EspeakWrapper, "_ESPEAK_DATA_PATH", None)
    monkeypatch.delenv("ESPEAK_DATA_PATH", raising=False)
    monkeypatch.delenv("PHONEMIZER_ESPEAK_DATA_PATH", raising=False)
    monkeypatch.delenv("PHONEMIZER_ESPEAK_LIBRARY", raising=False)

    from apps.launchers import desktop_backend

    monkeypatch.setattr(
        desktop_backend,
        "_frozen_espeak_runtime_base",
        lambda: tmp_path / "e",
    )

    desktop_backend.configure_frozen_espeak_runtime()

    configured_data = Path(EspeakWrapper._ESPEAK_DATA_PATH)
    env_data = Path(os.environ["ESPEAK_DATA_PATH"])

    assert len(os.fsencode(str(configured_data))) < 160
    assert len(os.fsencode(str(env_data))) < 160
    assert (configured_data / "phontab").read_text(encoding="utf-8") == "packaged-phontab"
    assert configured_data.resolve() == data_dir.resolve()
    assert fake_loader.get_data_path() == str(configured_data)


def _make_overlimit_packaged_data(tmp_path: Path) -> Path:
    """Build a packaged espeak-ng-data dir whose path exceeds N_PATH_HOME bytes."""
    deep_root = tmp_path.joinpath(*[f"n{i:02d}" for i in range(20)])
    data_dir = deep_root / "_internal" / "espeakng_loader" / "espeak-ng-data"
    data_dir.mkdir(parents=True)
    (data_dir / "phontab").write_text("packaged-phontab", encoding="utf-8")
    assert len(os.fsencode(str(data_dir.resolve()))) >= 160
    return data_dir


def test_ensure_short_espeak_data_path_falls_back_when_cache_mkdir_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-creatable bridge cache dir must skip that candidate, not abort startup.

    The bridge helper is meant to degrade gracefully across candidates. If the
    per-user cache path cannot be created (already a file, or non-writable
    parent), the candidate must be skipped rather than raising out of
    configure_frozen_espeak_runtime and crashing the sidecar.
    """
    from apps.launchers import desktop_backend

    data_dir = _make_overlimit_packaged_data(tmp_path)

    # Plant files where each candidate's cache dir would be created so both
    # parent mkdir calls raise an OSError (FileExistsError / NotADirectoryError).
    blocked_cache = tmp_path / "cb"
    blocked_cache.write_text("not-a-dir", encoding="utf-8")
    blocked_temp = tmp_path / "tb"
    blocked_temp.write_text("not-a-dir", encoding="utf-8")
    monkeypatch.setattr(desktop_backend, "_frozen_espeak_runtime_base", lambda: blocked_cache)
    monkeypatch.setattr(desktop_backend.tempfile, "gettempdir", lambda: str(blocked_temp))

    # Must not raise; every candidate is unusable so the packaged path is the
    # last resort, and it still resolves to real packaged data.
    result = desktop_backend._ensure_short_espeak_data_path(data_dir)

    assert result.resolve() == data_dir.resolve()
    assert (result / "phontab").read_text(encoding="utf-8") == "packaged-phontab"
    # The planted files must not have been turned into directories.
    assert blocked_cache.is_file()
    assert blocked_temp.is_file()


def test_ensure_short_espeak_data_path_never_deletes_real_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real directory at a candidate path must be left untouched, then skipped."""
    from apps.launchers import desktop_backend

    data_dir = _make_overlimit_packaged_data(tmp_path)

    cache_base = tmp_path / "e"
    monkeypatch.setattr(desktop_backend, "_frozen_espeak_runtime_base", lambda: cache_base)
    # Keep the temp fallback candidate under tmp_path so no real system state leaks.
    monkeypatch.setattr(desktop_backend.tempfile, "gettempdir", lambda: str(tmp_path / "t2"))

    # Plant a REAL directory (with user contents) where candidate 1's bridge
    # symlink would go. It must never be unlinked or replaced.
    planted = cache_base / "espeak-ng-data"
    planted.mkdir(parents=True)
    sentinel = planted / "user-data.txt"
    sentinel.write_text("precious", encoding="utf-8")

    result = desktop_backend._ensure_short_espeak_data_path(data_dir)

    # The real directory survives intact (not unlinked, not swapped for a symlink).
    assert planted.is_dir()
    assert not planted.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "precious"
    # A usable fallback resolving to real packaged data is still returned.
    assert result.resolve() == data_dir.resolve()
    assert (result / "phontab").read_text(encoding="utf-8") == "packaged-phontab"


def test_ensure_short_espeak_data_path_reuses_existing_symlink_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing bridge symlink pointing at packaged data is reused in place."""
    from apps.launchers import desktop_backend

    data_dir = _make_overlimit_packaged_data(tmp_path)

    cache_base = tmp_path / "e"
    monkeypatch.setattr(desktop_backend, "_frozen_espeak_runtime_base", lambda: cache_base)

    # Pre-create the exact bridge symlink the launcher would otherwise build.
    link_path = cache_base / "espeak-ng-data"
    link_path.parent.mkdir(parents=True)
    link_path.symlink_to(data_dir.resolve(), target_is_directory=True)
    inode_before = link_path.lstat().st_ino

    result = desktop_backend._ensure_short_espeak_data_path(data_dir)

    # Fast path returns the existing link without recreating it.
    assert result == link_path
    assert link_path.is_symlink()
    assert link_path.lstat().st_ino == inode_before
    assert result.resolve() == data_dir.resolve()
