"""Contract tests for the desktop backend launcher CLI."""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import shutil
import socket
import subprocess
import stat
import sys
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import TextIO
from unittest.mock import Mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("parent_pid,parent_exists", [(1, True), (42, False)])
def test_parent_watchdog_requests_shutdown_once_when_parent_disappears(
    monkeypatch, parent_pid, parent_exists
) -> None:
    from apps.launchers import desktop_backend

    stop = Mock()
    stop.wait.side_effect = [False, False, True]
    shutdown = Mock()
    timer = Mock()
    timer_factory = Mock(return_value=timer)
    monkeypatch.setattr(desktop_backend.threading, "Timer", timer_factory)
    force_exit = Mock()
    monkeypatch.setattr(desktop_backend.os, "_exit", force_exit)
    shutdown.side_effect = lambda: timer.start.assert_called_once()

    desktop_backend._watch_desktop_parent(
        42, shutdown, stop, getppid=lambda: parent_pid, pid_exists=lambda pid: parent_exists
    )

    shutdown.assert_called_once_with()
    stop.wait.assert_called_once_with(1.0)
    timer_factory.assert_called_once_with(8.0, desktop_backend._exit_orphaned_backend, args=(1,))
    assert timer.daemon is True


def test_parent_watchdog_keeps_running_while_parent_is_alive(monkeypatch) -> None:
    from apps.launchers import desktop_backend

    stop = Mock()
    stop.wait.side_effect = [False, False, True]
    shutdown = Mock()
    pid_exists = Mock(return_value=True)
    timer_factory = Mock()
    monkeypatch.setattr(desktop_backend.threading, "Timer", timer_factory)

    desktop_backend._watch_desktop_parent(
        42, shutdown, stop, getppid=lambda: 42, pid_exists=pid_exists
    )

    assert pid_exists.call_count == 2
    pid_exists.assert_called_with(42)
    shutdown.assert_not_called()
    timer_factory.assert_not_called()


@pytest.mark.parametrize("parent_pid", [None, "", "invalid", "0", "-1"])
def test_parent_watchdog_does_not_start_without_valid_parent_pid(monkeypatch, parent_pid) -> None:
    from apps.launchers import desktop_backend

    if parent_pid is None:
        monkeypatch.delenv("ORPHEUS_DESKTOP_PARENT_PID", raising=False)
    else:
        monkeypatch.setenv("ORPHEUS_DESKTOP_PARENT_PID", parent_pid)
    monkeypatch.setattr(desktop_backend.sys, "platform", "darwin")
    thread_factory = Mock()
    monkeypatch.setattr(desktop_backend.threading, "Thread", thread_factory)

    assert desktop_backend._start_desktop_parent_watchdog(Mock()) is None
    thread_factory.assert_not_called()


def test_parent_watchdog_does_not_start_on_windows(monkeypatch) -> None:
    from apps.launchers import desktop_backend

    monkeypatch.setenv("ORPHEUS_DESKTOP_PARENT_PID", "42")
    monkeypatch.setattr(desktop_backend.sys, "platform", "win32")
    thread_factory = Mock()
    monkeypatch.setattr(desktop_backend.threading, "Thread", thread_factory)

    assert desktop_backend._start_desktop_parent_watchdog(Mock()) is None
    thread_factory.assert_not_called()


def test_parent_watchdog_starts_daemon_for_desktop_parent(monkeypatch) -> None:
    from apps.launchers import desktop_backend

    monkeypatch.setenv("ORPHEUS_DESKTOP_PARENT_PID", "42")
    monkeypatch.setattr(desktop_backend.sys, "platform", "darwin")
    thread_factory = Mock()
    monkeypatch.setattr(desktop_backend.threading, "Thread", thread_factory)
    shutdown = Mock()

    stop = desktop_backend._start_desktop_parent_watchdog(shutdown)

    assert isinstance(stop, threading.Event)
    thread_factory.assert_called_once_with(
        target=desktop_backend._watch_desktop_parent, args=(42, shutdown, stop), daemon=True
    )
    thread_factory.return_value.start.assert_called_once_with()


def test_launcher_monitors_parent_during_server_run_and_stops_monitor_on_return(monkeypatch) -> None:
    from apps.launchers import desktop_backend

    monkeypatch.setattr(desktop_backend, "configure_espeak_runtime", Mock())
    monkeypatch.setattr(desktop_backend.multiprocessing, "freeze_support", Mock())
    monkeypatch.setattr(desktop_backend, "_runtime_profile_identity", lambda: "base")
    monkeypatch.setattr(desktop_backend, "create_app", Mock())
    server = Mock(ready_emitted=True)
    monkeypatch.setattr(desktop_backend, "_build_desktop_server", Mock(return_value=server))
    start_watchdog = Mock()
    monkeypatch.setattr(desktop_backend, "_start_desktop_parent_watchdog", start_watchdog, raising=False)
    server.run.side_effect = lambda: start_watchdog.assert_called_once_with(server.request_shutdown)

    assert desktop_backend.main([]) == 0
    start_watchdog.return_value.set.assert_called_once_with()


@pytest.mark.parametrize("error,expected", [(None, True), (ProcessLookupError, False), (PermissionError, True)])
def test_parent_pid_exists_treats_only_missing_process_as_dead(monkeypatch, error, expected) -> None:
    from apps.launchers import desktop_backend

    kill = Mock(side_effect=error)
    monkeypatch.setattr(desktop_backend.os, "kill", kill)

    assert desktop_backend._pid_exists(42) is expected
    kill.assert_called_once_with(42, 0)


@pytest.fixture(autouse=True)
def _isolated_program_data(tmp_path, monkeypatch):
    # The Windows speech cache falls back to %PROGRAMDATA%; never write the real one.
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    for key, directory in (
        ("AUDIOBOOK_DATA_DIR", "data"),
        ("AUDIOBOOK_OUTPUT_DIR", "output"),
        ("AUDIOBOOK_TEMP_DIR", "temp"),
    ):
        monkeypatch.setenv(key, str(tmp_path / directory))
    monkeypatch.setenv("AUDIOBOOK_DB_PATH", str(tmp_path / "data" / "audiobooks.db"))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "huggingface"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "huggingface" / "hub"))
    monkeypatch.delenv("AUDIOBOOK_DESKTOP_READY_FILE", raising=False)
    yield
    from apps.launchers import desktop_backend

    lock = getattr(desktop_backend, "_library_lock", None)
    if lock is not None:
        lock.close()
        monkeypatch.setattr(desktop_backend, "_library_lock", None)


@pytest.mark.parametrize("database_reference", [
    "default-layout", "different-data-dir", "relative", "symlink",
    pytest.param("windows-case", marks=pytest.mark.skipif(os.name != "nt", reason="Windows paths")),
])
def test_library_lock_rejects_second_launcher_before_app_creation_and_releases_on_exit(
    tmp_path, database_reference,
):
    database = tmp_path / "data" / "audiobooks.db"
    owner_env = _build_env()
    contender_env = _build_env()
    if database_reference == "default-layout":
        owner_env.pop("AUDIOBOOK_DB_PATH")
        contender_env.pop("AUDIOBOOK_DB_PATH")
    else:
        contender_env["AUDIOBOOK_DATA_DIR"] = str(tmp_path / "other-data")
    if database_reference == "relative":
        try:
            contender_env["AUDIOBOOK_DB_PATH"] = os.path.relpath(database)
        except ValueError:
            # Windows has no relative path between drives (CI checks out on D:).
            pytest.skip("the test folder is on a different drive than the working directory")
    elif database_reference == "symlink":
        database.parent.mkdir(parents=True, exist_ok=True)
        database.touch()
        alias = tmp_path / "alias.db"
        try:
            alias.symlink_to(database)
        except OSError as exc:
            if os.name != "nt":
                raise
            pytest.skip(f"symlink creation unavailable: {exc}")
        contender_env["AUDIOBOOK_DB_PATH"] = str(alias)
    elif database_reference == "windows-case":
        contender_env["AUDIOBOOK_DB_PATH"] = str(database).upper()
    script = """
import sys
from unittest.mock import Mock
from apps.launchers import desktop_backend as backend
backend.configure_espeak_runtime = lambda: None
def create_app(**kwargs):
    print('opened', flush=True)
    return Mock()
backend.create_app = create_app
backend._build_desktop_server = lambda *args, **kwargs: Mock(
    ready_emitted=True, run=lambda: sys.stdin.read())
raise SystemExit(backend.main([]))
"""
    command = [sys.executable, "-c", script]
    owner = subprocess.Popen(
        command, env=owner_env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert _readline_with_timeout(owner.stdout, 10) == "opened\n"
        contender = subprocess.run(command, env=contender_env, input="", capture_output=True, text=True, timeout=10)
        assert contender.returncode != 0
        assert "Orpheus is already running with this library" in contender.stderr
        assert "opened" not in contender.stdout
        isolated = subprocess.run(
            command, env=_build_env({
                "AUDIOBOOK_DATA_DIR": str(tmp_path / "other-library"),
                "AUDIOBOOK_DB_PATH": str(tmp_path / "other-library" / "audiobooks.db"),
            }),
            input="", capture_output=True, text=True, timeout=10,
        )
        assert isolated.returncode == 0, isolated.stderr
        assert isolated.stdout == "opened\n"
        separate_database = subprocess.run(
            command, env=_build_env({
                "AUDIOBOOK_DB_PATH": str(tmp_path / "data" / "other.db"),
            }),
            input="", capture_output=True, text=True, timeout=10,
        )
        assert separate_database.returncode == 0, separate_database.stderr
        assert separate_database.stdout == "opened\n"
    finally:
        _stop_process(owner)
        owner.communicate(timeout=5)
    successor = subprocess.run(command, env=owner_env, input="", capture_output=True, text=True, timeout=10)
    assert successor.returncode == 0, successor.stderr
    assert successor.stdout == "opened\n"


@pytest.mark.parametrize("frozen", [False, True])
def test_launcher_configures_utf8_streams_before_dependency_imports(frozen):
    script = f"""
import importlib.abc
import sys
sys.frozen = {frozen!r}
class CheckStreams(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'modules.hf_cache':
            assert sys.stdout.encoding == 'utf-8'
            assert sys.stderr.encoding == 'utf-8'
            assert sys.stdout.errors == 'backslashreplace'
            assert sys.stderr.errors == 'backslashreplace'
            print('José', file=sys.stderr)
            print('\\udcff', file=sys.stderr)
            raise SystemExit(0)
sys.meta_path.insert(0, CheckStreams())
import apps.launchers.desktop_backend
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=_build_env({"PYTHONIOENCODING": "cp1252:strict"}), capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.replace(b"\r\n", b"\n") == "José\n\\udcff\n".encode("utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX parent watchdog")
def test_parent_watchdog_fallback_terminates_descendants_when_shutdown_stalls(tmp_path):
    import psutil

    script = """
import subprocess
import sys
import threading
from apps.launchers import desktop_backend as backend
child = subprocess.Popen([sys.executable, '-c', '''
import signal
import subprocess
import sys
import time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
print(grandchild.pid, flush=True)
time.sleep(60)
'''], stdout=subprocess.PIPE, text=True)
grandchild_pid = int(child.stdout.readline().strip())
print(child.pid, grandchild_pid, flush=True)
try:
    backend.psutil.Process().children(recursive=True)
except PermissionError:
    # Sandboxed macOS cannot enumerate processes; retain real signal/wait coverage.
    descendants = [backend.psutil.Process(child.pid), backend.psutil.Process(grandchild_pid)]
    def children(self, *, recursive):
        assert self.pid == backend.os.getpid()
        assert recursive is True
        return descendants
    backend.psutil.Process.children = children
timer = threading.Timer
backend.threading.Timer = lambda interval, callback, args: timer(0.1, callback, args=args)
backend._watch_desktop_parent(
    42, lambda: threading.Event().wait(), threading.Event(), getppid=lambda: 1)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script], env=_build_env(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    descendants = []
    try:
        line = _readline_with_timeout(process.stdout, 10)
        assert line
        descendants = [psutil.Process(int(pid)) for pid in line.split()]
        assert process.wait(timeout=6) == 1
        stderr = process.stderr.read()
        _, alive = psutil.wait_procs(descendants, timeout=1)
        assert not [child for child in alive if child.status() != psutil.STATUS_ZOMBIE], stderr
    finally:
        _stop_process(process)
        for child in descendants:
            with contextlib.suppress(psutil.NoSuchProcess):
                child.kill()
        psutil.wait_procs(descendants, timeout=2)
        process.communicate(timeout=5)


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


def test_desktop_backend_launcher_reports_selected_runtime_profile(tmp_path: Path) -> None:
    _skip_if_loopback_bind_unavailable()
    process = _launch_desktop_backend(
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        cwd=tmp_path,
        env_overrides={"ORPHEUS_RUNTIME_PROFILE": "base"},
    )
    try:
        payload = _read_ready_payload(process)
        assert payload["runtime_profile"] == "base"
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

    def _fake_configure_espeak_runtime() -> None:
        call_order.append("configure_espeak_runtime")

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
        def __init__(
            self,
            config: object,
            *,
            ready_host: str,
            token: str,
            runtime_profile: str,
        ) -> None:
            _ = config
            _ = ready_host
            _ = token
            _ = runtime_profile
            call_order.append("server_init")
            self.ready_emitted = True

        def run(self) -> None:
            call_order.append("server_run")

        def request_shutdown(self) -> None:
            pytest.fail("unexpected shutdown request")

    monkeypatch.setattr(
        desktop_backend,
        "configure_espeak_runtime",
        _fake_configure_espeak_runtime,
    )
    monkeypatch.setattr(desktop_backend.os, "umask", _fake_umask)
    monkeypatch.setattr(desktop_backend.multiprocessing, "freeze_support", _fake_freeze_support)
    monkeypatch.setattr(desktop_backend, "build_parser", lambda: _FakeParser())
    monkeypatch.setattr(desktop_backend, "create_app", lambda **_: object())
    monkeypatch.setattr(desktop_backend, "_DesktopReadyServer", _FakeServer)

    exit_code = desktop_backend.main([])

    assert exit_code == 0
    expected = ["configure_espeak_runtime", "freeze_support"]
    if os.name != "nt":
        expected = ["umask", *expected]
    assert call_order[: len(expected)] == expected
    assert "parse_args" in call_order


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("folder", ["app é", "x" * 100])
def test_windows_espeak_unsafe_path_is_copied_for_launch_and_reentry(tmp_path, monkeypatch, frozen, folder):
    from apps.launchers import desktop_backend
    from phonemizer.backend.espeak.wrapper import EspeakWrapper

    data_dir = tmp_path / folder / "espeak-ng-data"
    data_dir.mkdir(parents=True)
    (data_dir / "phontab").write_bytes(b"phonemes")
    library = data_dir.parent / "libespeak-ng.dll"
    library.write_bytes(b"library")
    if not folder.isascii():
        assert len(os.fsencode(data_dir.resolve())) < 160
    loader = SimpleNamespace(get_library_path=lambda: str(library), get_data_path=lambda: str(data_dir))
    monkeypatch.setitem(sys.modules, "espeakng_loader", loader)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(EspeakWrapper, "_ESPEAK_LIBRARY", None)
    monkeypatch.setattr(EspeakWrapper, "_ESPEAK_DATA_PATH", None)
    for name in ("ESPEAK_DATA_PATH", "PHONEMIZER_ESPEAK_DATA_PATH", "PHONEMIZER_ESPEAK_LIBRARY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(desktop_backend, "_espeak_runtime_base", lambda: tmp_path / "e")
    monkeypatch.setattr(desktop_backend.tempfile, "gettempdir", lambda: str(tmp_path))

    desktop_backend.configure_espeak_runtime()

    configured = Path(loader.get_data_path()).resolve()
    assert configured != data_dir.resolve()
    assert str(configured).isascii()
    assert len(os.fsencode(configured)) < 160
    assert (configured / "phontab").read_bytes() == b"phonemes"
    assert EspeakWrapper._ESPEAK_DATA_PATH == str(configured)
    assert os.environ["ESPEAK_DATA_PATH"] == str(configured)
    assert os.environ["PHONEMIZER_ESPEAK_DATA_PATH"] == str(configured)

    desktop_backend.configure_espeak_runtime()

    assert loader.get_data_path() == str(configured)
    assert EspeakWrapper._ESPEAK_DATA_PATH == str(configured)


def test_source_espeak_long_path_is_copied_before_phonemizer_import(tmp_path, monkeypatch):
    import builtins

    from apps.launchers import desktop_backend
    from modules.tts.mlx_compat import patch_espeakng_loader_paths
    from phonemizer.backend.espeak.wrapper import EspeakWrapper

    data_dir = _make_overlimit_packaged_data(tmp_path)
    library = data_dir.parent / "libespeak-ng.dylib"
    library.write_bytes(b"library")
    loader = SimpleNamespace(get_library_path=lambda: str(library), get_data_path=lambda: str(data_dir))
    monkeypatch.setitem(sys.modules, "espeakng_loader", loader)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(EspeakWrapper, "_ESPEAK_LIBRARY", None)
    monkeypatch.setattr(EspeakWrapper, "_ESPEAK_DATA_PATH", None)
    for name in ("ESPEAK_DATA_PATH", "PHONEMIZER_ESPEAK_DATA_PATH", "PHONEMIZER_ESPEAK_LIBRARY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(desktop_backend, "_espeak_runtime_base", lambda: tmp_path / "e")
    monkeypatch.setattr(desktop_backend.tempfile, "gettempdir", lambda: str(tmp_path))
    original_import = builtins.__import__
    imports = []

    def check_import(name, *args, **kwargs):
        if name.startswith("phonemizer"):
            configured = Path(loader.get_data_path()).resolve()
            assert len(os.fsencode(configured)) < 160
            assert os.environ["ESPEAK_DATA_PATH"] == str(configured)
            imports.append(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", check_import)
    desktop_backend.configure_espeak_runtime()
    patch_espeakng_loader_paths()

    configured = Path(loader.get_data_path()).resolve()
    assert len(os.fsencode(configured)) < 160
    assert configured != data_dir.resolve()
    assert (configured / "phontab").read_text() == "packaged-phontab"
    assert EspeakWrapper._ESPEAK_DATA_PATH == str(configured)
    assert EspeakWrapper._ESPEAK_LIBRARY == str(library)
    assert os.environ["PHONEMIZER_ESPEAK_DATA_PATH"] == str(configured)
    assert os.environ["PHONEMIZER_ESPEAK_LIBRARY"] == str(library)
    assert imports


def _source_worker_espeak_paths(library, data_dir):
    from modules.tts.mlx_compat import patch_espeakng_loader_paths

    loader = SimpleNamespace(get_library_path=lambda: library, get_data_path=lambda: data_dir)
    sys.modules["espeakng_loader"] = loader
    patch_espeakng_loader_paths()
    return loader.get_library_path(), loader.get_data_path()


@pytest.mark.parametrize("env_name", ["PHONEMIZER_ESPEAK_DATA_PATH", "ESPEAK_DATA_PATH"])
def test_source_worker_keeps_inherited_short_espeak_data_path(tmp_path, monkeypatch, env_name):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    from modules.tts.mlx_compat import patch_espeakng_loader_paths

    data_dir = _make_overlimit_packaged_data(tmp_path)
    library = data_dir.parent / "libespeak-ng.dylib"
    library.write_bytes(b"library")
    configured = tmp_path / "short"
    shutil.copytree(data_dir, configured)
    loader = SimpleNamespace(get_library_path=lambda: str(library), get_data_path=lambda: str(data_dir))
    monkeypatch.setitem(sys.modules, "espeakng_loader", loader)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    for name in ("PHONEMIZER_ESPEAK_DATA_PATH", "ESPEAK_DATA_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(env_name, str(configured))

    patch_espeakng_loader_paths()
    patch_espeakng_loader_paths()

    assert Path(loader.get_data_path()).resolve() == configured.resolve()
    assert len(os.fsencode(Path(loader.get_data_path()).resolve())) < 160
    assert loader.get_library_path() == str(library)
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        worker_library, worker_data = pool.submit(
            _source_worker_espeak_paths, str(library), str(data_dir),
        ).result(timeout=30)
    assert worker_library == str(library)
    assert Path(worker_data).resolve() == configured.resolve()


def test_configure_espeak_runtime_sets_phonemizer_wrapper_to_packaged_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frozen sidecar must configure phonemizer before TTS uses the dylib default path."""
    from phonemizer.backend.espeak.wrapper import EspeakWrapper

    from modules.tts.mlx_compat import _espeak_library_name

    package_dir = tmp_path / "_internal" / "espeakng_loader"
    data_dir = package_dir / "espeak-ng-data"
    library_path = package_dir / _espeak_library_name()
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

    desktop_backend.configure_espeak_runtime()

    assert EspeakWrapper._ESPEAK_LIBRARY == str(library_path)
    assert EspeakWrapper._ESPEAK_DATA_PATH == str(data_dir)
    assert fake_loader.get_library_path() == str(library_path)
    assert fake_loader.get_data_path() == str(data_dir)


@pytest.mark.parametrize("repatch", [False, True])
def test_frozen_espeak_data_stays_short_after_compatibility_patch(tmp_path, monkeypatch, repatch):
    from apps.launchers import desktop_backend
    from modules.tts.mlx_compat import _espeak_library_name, patch_espeakng_loader_paths
    from phonemizer.backend.espeak.wrapper import EspeakWrapper

    data_dir = _make_overlimit_packaged_data(tmp_path)
    (data_dir.parent / _espeak_library_name()).write_bytes(b"dylib")
    (data_dir / "voices").mkdir()
    (data_dir / "voices" / "en").write_text("voice", encoding="utf-8")
    loader = SimpleNamespace(get_library_path=lambda: "missing", get_data_path=lambda: "missing")
    monkeypatch.setitem(sys.modules, "espeakng_loader", loader)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(data_dir.parent.parent), raising=False)
    monkeypatch.setattr(EspeakWrapper, "_ESPEAK_LIBRARY", None)
    monkeypatch.setattr(EspeakWrapper, "_ESPEAK_DATA_PATH", None)
    for name in ("ESPEAK_DATA_PATH", "PHONEMIZER_ESPEAK_DATA_PATH", "PHONEMIZER_ESPEAK_LIBRARY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(desktop_backend, "_espeak_runtime_base", lambda: tmp_path / "e")
    monkeypatch.setattr(desktop_backend.tempfile, "gettempdir", lambda: str(tmp_path))

    desktop_backend.configure_espeak_runtime()
    if repatch:
        patch_espeakng_loader_paths()

    configured = Path(loader.get_data_path())
    assert len(os.fsencode(configured)) < 160
    assert len(os.fsencode(configured.resolve())) < 160
    assert configured == Path(EspeakWrapper._ESPEAK_DATA_PATH)
    assert configured == Path(os.environ["ESPEAK_DATA_PATH"])
    assert (configured / "phontab").read_text(encoding="utf-8") == "packaged-phontab"
    assert (configured / "voices" / "en").read_text(encoding="utf-8") == "voice"


@pytest.mark.skipif(os.name == "nt", reason="POSIX cache fallback")
def test_configure_espeak_runtime_bridges_multibyte_paths_over_byte_limit(
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
        "_espeak_runtime_base",
        lambda: tmp_path / "e",
    )

    desktop_backend.configure_espeak_runtime()

    configured_data = Path(EspeakWrapper._ESPEAK_DATA_PATH)
    env_data = Path(os.environ["ESPEAK_DATA_PATH"])

    assert len(os.fsencode(str(configured_data))) < 160
    assert len(os.fsencode(str(env_data))) < 160
    assert (configured_data / "phontab").read_text(encoding="utf-8") == "packaged-phontab"
    assert len(os.fsencode(configured_data.resolve())) < 160
    assert fake_loader.get_data_path() == str(configured_data)


def _make_overlimit_packaged_data(tmp_path: Path) -> Path:
    """Build a packaged espeak-ng-data dir whose path exceeds N_PATH_HOME bytes."""
    deep_root = tmp_path.joinpath(*[f"n{i:02d}" for i in range(20)])
    data_dir = deep_root / "_internal" / "espeakng_loader" / "espeak-ng-data"
    data_dir.mkdir(parents=True)
    (data_dir / "phontab").write_text("packaged-phontab", encoding="utf-8")
    assert len(os.fsencode(str(data_dir.resolve()))) >= 160
    return data_dir


@pytest.mark.skipif(os.name == "nt", reason="POSIX cache")
def test_short_espeak_cache_reuses_verified_data_and_tracks_content_changes(tmp_path, monkeypatch):
    from apps.launchers import desktop_backend

    source = _make_overlimit_packaged_data(tmp_path)
    monkeypatch.setattr(desktop_backend, "_espeak_runtime_base", lambda: tmp_path / "e")
    monkeypatch.setattr(desktop_backend.tempfile, "gettempdir", lambda: str(tmp_path))
    cached = desktop_backend._ensure_short_espeak_data_path(source)
    assert not cached.is_symlink()
    original_mtime = (cached / "phontab").stat().st_mtime_ns
    assert desktop_backend._ensure_short_espeak_data_path(source) == cached
    assert (cached / "phontab").stat().st_mtime_ns == original_mtime
    (cached / "phontab").write_text("damaged", encoding="utf-8")
    assert desktop_backend._ensure_short_espeak_data_path(source) == cached
    assert (cached / "phontab").read_text(encoding="utf-8") == "packaged-phontab"
    (source / "phontab").write_text("updated-phontab", encoding="utf-8")
    updated = desktop_backend._ensure_short_espeak_data_path(source)
    assert updated != cached
    assert (updated / "phontab").read_text(encoding="utf-8") == "updated-phontab"


@pytest.mark.skipif(os.name == "nt", reason="POSIX cache fallback")
def test_ensure_short_espeak_data_path_rejects_unsafe_path_when_caches_are_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.launchers import desktop_backend

    data_dir = _make_overlimit_packaged_data(tmp_path)

    # Plant files where each candidate's cache dir would be created so both
    # parent mkdir calls raise an OSError (FileExistsError / NotADirectoryError).
    blocked_cache = tmp_path / "cb"
    blocked_cache.write_text("not-a-dir", encoding="utf-8")
    blocked_temp = tmp_path / "tb"
    blocked_temp.write_text("not-a-dir", encoding="utf-8")
    monkeypatch.setattr(desktop_backend, "_espeak_runtime_base", lambda: blocked_cache)
    monkeypatch.setattr(desktop_backend.tempfile, "gettempdir", lambda: str(blocked_temp))

    # Reject the unsafe packaged path before it can reach native initialization.
    with pytest.raises(RuntimeError, match="writable short cache path"):
        desktop_backend._ensure_short_espeak_data_path(data_dir)
    # The planted files must not have been turned into directories.
    assert blocked_cache.is_file()
    assert blocked_temp.is_file()


@pytest.mark.parametrize("folder", ["app é", "x" * 100])
def test_windows_espeak_cache_handles_unicode_and_long_paths(tmp_path, monkeypatch, folder):
    from apps.launchers import desktop_backend

    source = tmp_path / folder / "espeak-ng-data"
    source.mkdir(parents=True)
    (source / "phontab").write_bytes(b"phonemes")
    (source / "lang").mkdir()
    (source / "lang" / "en").write_bytes(b"english")
    monkeypatch.setattr(desktop_backend, "_espeak_runtime_base", lambda: tmp_path / "c")

    cached = desktop_backend._windows_espeak_data_path(source)
    assert cached != source
    assert str(cached.resolve()).isascii()
    assert len(os.fsencode(cached)) < 160
    assert (cached / "lang" / "en").read_bytes() == b"english"
    before = (cached / "phontab").stat().st_mtime_ns
    assert desktop_backend._windows_espeak_data_path(source) == cached
    assert (cached / "phontab").stat().st_mtime_ns == before

    (source / "phontab").write_bytes(b"new phonemes")
    updated = desktop_backend._windows_espeak_data_path(source)
    assert updated != cached
    assert (updated / "phontab").read_bytes() == b"new phonemes"
    assert (cached / "phontab").read_bytes() == b"phonemes"


def test_windows_espeak_cache_falls_back_without_overwriting_blocked_path(tmp_path, monkeypatch):
    from apps.launchers import desktop_backend

    source = tmp_path / "é" / "espeak-ng-data"
    source.mkdir(parents=True)
    (source / "phontab").write_bytes(b"phonemes")
    blocked = tmp_path / "blocked"
    blocked.write_text("keep me")
    monkeypatch.setattr(desktop_backend, "_espeak_runtime_base", lambda: blocked)
    monkeypatch.setattr(desktop_backend.tempfile, "gettempdir", lambda: str(tmp_path))
    _fit_path_limit_to(monkeypatch, desktop_backend, tmp_path / "orpheus-espeak")

    cached = desktop_backend._windows_espeak_data_path(source)
    assert cached.is_relative_to(tmp_path / "orpheus-espeak")
    assert blocked.read_text() == "keep me"


def test_windows_espeak_cache_fails_before_native_library_when_no_safe_path(tmp_path, monkeypatch):
    from apps.launchers import desktop_backend

    source = tmp_path / "é" / "espeak-ng-data"
    source.mkdir(parents=True)
    (source / "phontab").write_bytes(b"phonemes")
    monkeypatch.setattr(desktop_backend, "_espeak_runtime_base", lambda: tmp_path / "é")
    monkeypatch.setattr(desktop_backend.tempfile, "gettempdir", lambda: str(tmp_path / "é"))
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "é"))
    with pytest.raises(RuntimeError, match="ASCII cache path"):
        desktop_backend._windows_espeak_data_path(source)
    assert (source / "phontab").read_bytes() == b"phonemes"


def _fit_path_limit_to(monkeypatch, desktop_backend, program_data: Path) -> None:
    # pytest's temp root can exceed libespeak's limit on its own (macOS).
    limit = max(desktop_backend._LIBESPEAK_PATH_HOME_LIMIT, len(os.fsencode(program_data)) + 50)
    monkeypatch.setattr(desktop_backend, "_LIBESPEAK_PATH_HOME_LIMIT", limit)


def test_windows_espeak_cache_works_with_unicode_profile_and_temp(tmp_path, monkeypatch):
    from apps.launchers import desktop_backend

    profile = tmp_path / "Jörg"
    source = profile / "Orpheus" / "espeak-ng-data"
    source.mkdir(parents=True)
    (source / "phontab").write_bytes(b"phonemes")
    monkeypatch.setattr(Path, "home", lambda: profile)
    monkeypatch.setattr(desktop_backend.tempfile, "gettempdir", lambda: str(profile / "Temp"))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    _fit_path_limit_to(monkeypatch, desktop_backend, tmp_path / "ProgramData")

    cached = desktop_backend._windows_espeak_data_path(source)
    assert cached.is_relative_to(tmp_path / "ProgramData")
    assert str(cached.resolve()).isascii()
    assert (cached / "phontab").read_bytes() == b"phonemes"


def test_windows_espeak_programdata_cache_keeps_short_paths_isolated_and_versioned(tmp_path, monkeypatch):
    from apps.launchers import desktop_backend

    source = tmp_path / "shared é" / "espeak-ng-data"
    source.mkdir(parents=True)
    (source / "phontab").write_bytes(b"phonemes")
    program_data = tmp_path / "ProgramData"
    monkeypatch.setenv("PROGRAMDATA", str(program_data))
    monkeypatch.setattr(desktop_backend, "_espeak_runtime_base", lambda: tmp_path / "é")
    monkeypatch.setattr(desktop_backend.tempfile, "gettempdir", lambda: str(tmp_path / "é"))
    # Leave room for one cache key and espeak-ng-data, but not two hash levels.
    path_limit = len(os.fsencode(program_data)) + 50
    monkeypatch.setattr(desktop_backend, "_LIBESPEAK_PATH_HOME_LIMIT", path_limit)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "Jörg")

    first = desktop_backend._windows_espeak_data_path(source)
    assert len(os.fsencode(first)) < path_limit
    assert first.is_relative_to(program_data)
    assert desktop_backend._windows_espeak_data_path(source) == first

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "René")
    second = desktop_backend._windows_espeak_data_path(source)
    assert second != first
    (source / "phontab").write_bytes(b"updated phonemes")
    updated = desktop_backend._windows_espeak_data_path(source)
    assert updated != second
    assert (first / "phontab").read_bytes() == b"phonemes"
    assert (second / "phontab").read_bytes() == b"phonemes"
    assert (updated / "phontab").read_bytes() == b"updated phonemes"


@pytest.mark.parametrize("alteration", ["same_size", "missing", "extra", "renamed"])
def test_windows_espeak_programdata_cache_replaces_untrusted_contents(tmp_path, monkeypatch, alteration):
    from apps.launchers import desktop_backend

    source = tmp_path / "é" / "espeak-ng-data"
    source.mkdir(parents=True)
    (source / "phontab").write_bytes(b"phonemes")
    (source / "lang").mkdir()
    (source / "lang" / "en").write_bytes(b"english")
    monkeypatch.setattr(desktop_backend, "_espeak_runtime_base", lambda: tmp_path / "é")
    monkeypatch.setattr(desktop_backend.tempfile, "gettempdir", lambda: str(tmp_path / "é"))
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    _fit_path_limit_to(monkeypatch, desktop_backend, tmp_path / "ProgramData")
    target = desktop_backend._windows_espeak_data_path(source)
    if alteration == "same_size":
        before = (target / "phontab").stat()
        (target / "phontab").write_bytes(b"tampered")
        os.utime(target / "phontab", ns=(before.st_atime_ns, before.st_mtime_ns))
    elif alteration == "missing":
        (target / "lang" / "en").unlink()
    elif alteration == "extra":
        (target / "injected").write_bytes(b"extra data")
    else:
        (target / "lang" / "en").rename(target / "lang" / "xx")

    real_manifest = desktop_backend._espeak_data_manifest
    source_reads = []

    def counted_manifest(root):
        if root == source:
            source_reads.append(root)
        return real_manifest(root)

    real_copytree = shutil.copytree
    copies = []

    def staged_copy(src, dst, *args, **kwargs):
        if Path(src) == source:
            assert Path(dst) != target
            assert target.is_dir()
            copies.append(dst)
        return real_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(desktop_backend, "_espeak_data_manifest", counted_manifest)
    monkeypatch.setattr(desktop_backend.shutil, "copytree", staged_copy)
    assert desktop_backend._windows_espeak_data_path(source) == target
    assert source_reads == [source]
    assert len(copies) == 1
    assert real_manifest(target) == real_manifest(source)
    assert set(target.parent.iterdir()) == {target, target.parent / ".publish.lock"}

    copies.clear()
    assert desktop_backend._windows_espeak_data_path(source) == target
    assert not copies


def test_windows_espeak_programdata_cache_does_not_publish_failed_copy(tmp_path, monkeypatch):
    from apps.launchers import desktop_backend

    source = tmp_path / "é" / "espeak-ng-data"
    source.mkdir(parents=True)
    (source / "phontab").write_bytes(b"phonemes")
    monkeypatch.setattr(desktop_backend, "_espeak_runtime_base", lambda: tmp_path / "é")
    monkeypatch.setattr(desktop_backend.tempfile, "gettempdir", lambda: str(tmp_path / "é"))
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    _fit_path_limit_to(monkeypatch, desktop_backend, tmp_path / "ProgramData")
    target = desktop_backend._windows_espeak_data_path(source)
    (target / "phontab").write_bytes(b"tampered")

    def fail_copy(src, dst):
        Path(dst).mkdir()
        (Path(dst) / "phontab").write_bytes(b"partial")
        raise OSError("interrupted copy")

    monkeypatch.setattr(desktop_backend.shutil, "copytree", fail_copy)
    with pytest.raises(RuntimeError, match="ASCII cache path"):
        desktop_backend._windows_espeak_data_path(source)
    assert (target / "phontab").read_bytes() == b"tampered"
    assert set(target.parent.iterdir()) == {target, target.parent / ".publish.lock"}


def test_windows_espeak_programdata_cache_creates_private_directory(tmp_path, monkeypatch):
    from apps.launchers import desktop_backend

    source = tmp_path / "é" / "espeak-ng-data"
    source.mkdir(parents=True)
    (source / "phontab").write_bytes(b"phonemes")
    program_data = tmp_path / "ProgramData"
    program_data.mkdir()
    monkeypatch.setenv("PROGRAMDATA", str(program_data))
    monkeypatch.setattr(desktop_backend, "_espeak_runtime_base", lambda: tmp_path / "é")
    monkeypatch.setattr(desktop_backend.tempfile, "gettempdir", lambda: str(tmp_path / "é"))
    _fit_path_limit_to(monkeypatch, desktop_backend, program_data)
    real_mkdir = Path.mkdir
    modes = []

    def record_mkdir(path, mode=0o777, parents=False, exist_ok=False):
        if path.parent == program_data:
            modes.append(mode)
        return real_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", record_mkdir)
    target = desktop_backend._windows_espeak_data_path(source)
    assert target.is_relative_to(program_data)
    assert modes == [0o700]


def test_unavailable_speech_cache_does_not_prevent_library_backend_start(tmp_path, monkeypatch, capsys):
    from apps.launchers import desktop_backend
    from phonemizer.backend.espeak.wrapper import EspeakWrapper

    source = tmp_path / "Jörg" / "espeak-ng-data"
    source.mkdir(parents=True)
    (source / "phontab").write_bytes(b"phonemes")
    library = source.parent / "libespeak-ng.dll"
    library.write_bytes(b"library")
    loader = SimpleNamespace(get_library_path=lambda: str(library), get_data_path=lambda: str(source))
    monkeypatch.setitem(sys.modules, "espeakng_loader", loader)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(desktop_backend, "_ensure_short_espeak_data_path", desktop_backend._windows_espeak_data_path)
    monkeypatch.setattr(desktop_backend, "_espeak_runtime_base", lambda: source)
    monkeypatch.setattr(desktop_backend.tempfile, "gettempdir", lambda: str(source))
    blocked = tmp_path / "blocked"
    blocked.write_text("not a writable directory")
    monkeypatch.setenv("PROGRAMDATA", str(blocked))
    monkeypatch.setattr(EspeakWrapper, "library", EspeakWrapper.__dict__["library"])
    monkeypatch.setattr(desktop_backend.multiprocessing, "freeze_support", lambda: None)
    monkeypatch.setattr(desktop_backend, "_runtime_profile_identity", lambda: "cuda")
    started = []
    monkeypatch.setattr(desktop_backend, "_build_desktop_server", lambda *_args, **_kwargs: SimpleNamespace(
        ready_emitted=True, run=lambda: started.append(True),
        request_shutdown=lambda: pytest.fail("unexpected shutdown request"),
    ))

    assert desktop_backend.main([]) == 0
    assert started == [True]
    assert "Speech initialization is unavailable" in capsys.readouterr().err
    with pytest.raises(RuntimeError, match="ASCII cache path"):
        loader.get_data_path()
    with pytest.raises(RuntimeError, match="ASCII cache path"):
        EspeakWrapper.library()


@pytest.mark.skipif(os.name != "nt", reason="Windows narrow-path native library regression")
def test_frozen_windows_phonemizer_survives_unicode_install_path(tmp_path):
    loader = pytest.importorskip("espeakng_loader")
    pytest.importorskip("phonemizer")
    source = tmp_path / "app é" / "espeak-ng-data"
    shutil.copytree(loader.get_data_path(), source)
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import sys; from pathlib import Path; from types import SimpleNamespace; "
            "from apps.launchers import desktop_backend as launcher; "
            "launcher._espeak_runtime_base=lambda:Path(sys.argv[3]); "
            "sys.modules['espeakng_loader']=SimpleNamespace("
            "get_library_path=lambda:sys.argv[1], get_data_path=lambda:sys.argv[2]); "
            "sys.frozen=True; launcher.configure_espeak_runtime(); "
            "from phonemizer.backend import EspeakBackend; "
            "assert EspeakBackend('en-us').phonemize(['Hello world.'],strip=True)[0]; "
            "print('phonemizer survived')",
            loader.get_library_path(), str(source), str(tmp_path / "c"),
        ],
        cwd=PROJECT_ROOT, env=_build_env(), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "phonemizer survived" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX cache fallback")
def test_ensure_short_espeak_data_path_never_deletes_real_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real directory at a candidate path must be left untouched, then skipped."""
    from apps.launchers import desktop_backend

    data_dir = _make_overlimit_packaged_data(tmp_path)

    cache_base = tmp_path / "e"
    monkeypatch.setattr(desktop_backend, "_espeak_runtime_base", lambda: cache_base)
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
    # The returned physical cache contains the packaged data.
    assert len(os.fsencode(result.resolve())) < 160
    assert (result / "phontab").read_text(encoding="utf-8") == "packaged-phontab"


@pytest.mark.skipif(os.name == "nt", reason="POSIX cache fallback")
def test_ensure_short_espeak_data_path_bypasses_existing_symlink_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy symlink is left intact but never passed to phonemizer."""
    from apps.launchers import desktop_backend

    data_dir = _make_overlimit_packaged_data(tmp_path)

    cache_base = tmp_path / "e"
    monkeypatch.setattr(desktop_backend, "_espeak_runtime_base", lambda: cache_base)

    # Pre-create the exact bridge symlink the launcher would otherwise build.
    link_path = cache_base / "espeak-ng-data"
    link_path.parent.mkdir(parents=True)
    link_path.symlink_to(data_dir.resolve(), target_is_directory=True)
    inode_before = link_path.lstat().st_ino

    result = desktop_backend._ensure_short_espeak_data_path(data_dir)

    assert result != link_path
    assert link_path.is_symlink()
    assert link_path.lstat().st_ino == inode_before
    assert len(os.fsencode(result.resolve())) < 160
    assert (result / "phontab").read_text(encoding="utf-8") == "packaged-phontab"


@pytest.mark.parametrize("windows", [False, True])
def test_espeak_copy_keeps_matching_cache_published_during_staging(tmp_path, monkeypatch, windows):
    from apps.launchers import desktop_backend

    source = _make_overlimit_packaged_data(tmp_path)
    cache_base = tmp_path / ("é" if windows else "e")
    monkeypatch.setattr(desktop_backend, "_espeak_runtime_base", lambda: cache_base)
    monkeypatch.setattr(desktop_backend.tempfile, "gettempdir", lambda: str(cache_base))
    if windows:
        _fit_path_limit_to(monkeypatch, desktop_backend, tmp_path / "ProgramData")
    ensure = (
        desktop_backend._windows_espeak_data_path
        if windows else desktop_backend._ensure_short_espeak_data_path
    )
    target = ensure(source)
    shutil.rmtree(target)
    copytree = shutil.copytree
    rename = Path.rename

    def publish_during_copy(src, dst, *args, **kwargs):
        result = copytree(src, dst, *args, **kwargs)
        if Path(src) == source and not target.exists():
            copytree(source, target)
        return result

    def keep_published_copy(path, destination):
        assert path != target, "matching published speech data must stay available"
        return rename(path, destination)

    monkeypatch.setattr(desktop_backend.shutil, "copytree", publish_during_copy)
    monkeypatch.setattr(Path, "rename", keep_published_copy)
    assert ensure(source) == target
    assert desktop_backend._espeak_data_manifest(target) == desktop_backend._espeak_data_manifest(source)


def test_espeak_publication_waits_for_other_process_and_reuses_its_copy(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "phontab").write_bytes(b"phonemes")
    target = tmp_path / "version" / "espeak-ng-data"
    target.parent.mkdir()
    copied = tmp_path / "copied"
    preamble = """
import shutil
import sys
from pathlib import Path
from apps.launchers import desktop_backend as backend
source, target, marker = map(Path, sys.argv[1:])
"""
    owner_script = preamble + """
with backend._open_process_lock(target.parent / '.publish.lock', blocking=True):
    print('locked', flush=True)
    sys.stdin.read()
    shutil.copytree(source, target)
"""
    contender_script = preamble + """
copytree = shutil.copytree
def copy(src, dst, *args, **kwargs):
    marker.touch()
    return copytree(src, dst, *args, **kwargs)
shutil.copytree = copy
print('checking', flush=True)
backend._publish_espeak_data(source, target, backend._espeak_data_manifest(source))
"""
    arguments = [str(source), str(target), str(copied)]
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_script, *arguments], env=_build_env(),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    contender = None
    try:
        assert _readline_with_timeout(owner.stdout, 10) == "locked\n"
        contender = subprocess.Popen(
            [sys.executable, "-c", contender_script, *arguments], env=_build_env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert _readline_with_timeout(contender.stdout, 10) == "checking\n"
        with pytest.raises(subprocess.TimeoutExpired):
            contender.wait(timeout=0.2)
        assert not copied.exists()
        _, stderr = owner.communicate(input="", timeout=5)
        assert owner.returncode == 0, stderr
        _, stderr = contender.communicate(timeout=5)
        assert contender.returncode == 0, stderr
        assert not copied.exists()
        assert (target / "phontab").read_bytes() == b"phonemes"
    finally:
        _stop_process(owner)
        owner.communicate(timeout=5)
        if contender is not None:
            _stop_process(contender)
            contender.communicate(timeout=5)


@pytest.mark.parametrize("blocking", [False, True])
def test_windows_process_lock_locks_first_byte_and_only_waits_when_requested(tmp_path, monkeypatch, blocking):
    from apps.launchers import desktop_backend

    lock_path = tmp_path / "process.lock"
    calls = []

    def lock(fd, mode, count):
        calls.append((os.lseek(fd, 0, os.SEEK_CUR), mode, count))
        if len(calls) == 1:
            raise OSError(errno.EACCES, "already locked")

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=2, locking=lock))
    monkeypatch.setattr(desktop_backend, "os", SimpleNamespace(name="nt", fstat=os.fstat))
    sleep = Mock()
    monkeypatch.setattr(desktop_backend.time, "sleep", sleep)
    if blocking:
        with desktop_backend._open_process_lock(lock_path, blocking=True):
            assert calls == [(0, 2, 1), (0, 2, 1)]
        sleep.assert_called_once_with(0.05)
    else:
        with pytest.raises(OSError, match="already locked"):
            desktop_backend._open_process_lock(lock_path, blocking=False)
        assert calls == [(0, 2, 1)]
        sleep.assert_not_called()
    assert lock_path.read_bytes() == b"\0"
