import json
import multiprocessing
import os
import threading
import time

import pytest
from pathlib import Path
import sys

# Add project root to path
PROJECT_ROOT = str(Path(__file__).parent.parent)
if PROJECT_ROOT in sys.path:
    sys.path.remove(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

from modules.runtime_setup.state import STATE_FILENAME, default_state_path  # noqa: E402

_MARKER_ONLY_COVERAGE_GATES = frozenset({"property", "benchmark", "slow"})
# Resolved at import, before the autouse fixtures redirect HOME and APPDATA.
_ORIGINAL_SETUP_STATE = default_state_path()


def _setup_state_snapshot(directory):
    """Record the setup state's content, not what a running Orpheus app rewrites.

    The live app shares this folder: each backend launch writes
    backend-instance.json under the setup lock directory and may clear
    restart_required. A test leak changes the rest of the state.
    """
    try:
        raw = (directory / STATE_FILENAME).read_bytes()
    except FileNotFoundError:
        return {}
    try:
        state = json.loads(raw)
    except ValueError:
        return {STATE_FILENAME: raw}
    if isinstance(state, dict):
        state.pop("restart_required", None)
    return {STATE_FILENAME: state}


def _tree_snapshot(root):
    """Record file names and timestamps without reading contents or following links."""
    def raise_walk_error(error):
        raise error

    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.lstat().st_mtime_ns
        for directory, _, names in os.walk(root, onerror=raise_walk_error)
        for name in names
        for path in (Path(directory) / name,)
    }


def pytest_sessionstart(session):
    watched = (
        ("real user setup", _ORIGINAL_SETUP_STATE.parent, _setup_state_snapshot),
        ("repository data", Path(PROJECT_ROOT) / "data", _tree_snapshot),
        ("repository test data", Path(PROJECT_ROOT) / "tests" / "data", _tree_snapshot),
    )
    session._storage_snapshots = [
        (label, root, snapshot, snapshot(root)) for label, root, snapshot in watched
    ]


def _marker_only_coverage_gate(config) -> bool:
    """Return whether a focused marker run should skip the global threshold."""
    markexpr = str(getattr(config.option, "markexpr", "") or "").strip()
    return markexpr in _MARKER_ONLY_COVERAGE_GATES


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtestloop(session):
    result = yield
    selected_args = {Path(arg).as_posix() for arg in session.config.args}
    if selected_args == {"tests/test_web_route_contract.py"} or _marker_only_coverage_gate(session.config):
        cov_plugin = session.config.pluginmanager.getplugin("_cov")
        if cov_plugin is not None:
            cov_plugin.options.cov_fail_under = 0
    return result

_session_exit_status = 0


def pytest_sessionfinish(session, exitstatus):
    global _session_exit_status
    for label, root, snapshot, before in session._storage_snapshots:
        try:
            after = snapshot(root)
            changed = sorted(name for name, value in after.items() if before.get(name) != value)
            removed = sorted(before.keys() - after.keys())
            if not changed and not removed:
                continue
            sys.stderr.write(
                f"\n[conftest] {label} files added or modified in {root}: "
                f"{', '.join(changed) or 'none'}; removed: {', '.join(removed) or 'none'}\n"
            )
        except OSError as exc:
            sys.stderr.write(f"\n[conftest] could not inspect {label} files in {root}: {exc}\n")
        sys.stderr.flush()
        session.exitstatus = exitstatus or pytest.ExitCode.TESTS_FAILED
    _session_exit_status = int(session.exitstatus)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_unconfigure(config):
    """Refuse to hang on leaked non-daemon threads.

    Interpreter shutdown joins non-daemon threads before atexit runs, so a
    single leaked thread turns a fully green session into a process that
    never exits (observed on the Windows CI runner). By this point every
    other unconfigure hook — including coverage reporting — has completed,
    so name the leakers, dump their stacks, and make leaks fail the session.
    """
    result = yield
    if os.name == "nt":
        # The session result is final; nothing after this point should be
        # interruptible. A stray console CTRL_C_EVENT during interpreter
        # shutdown otherwise corrupts the exit code of a green run.
        import signal

        signal.signal(signal.SIGINT, signal.SIG_IGN)
    lingering = [
        thread
        for thread in threading.enumerate()
        if thread is not threading.main_thread() and not thread.daemon and thread.is_alive()
    ]
    children = multiprocessing.active_children()
    deadline = time.monotonic() + 0.5
    for worker in [*lingering, *children]:
        worker.join(timeout=max(0.0, deadline - time.monotonic()))
    lingering = [thread for thread in lingering if thread.is_alive()]
    children = [child for child in children if child.is_alive()]
    try:
        import psutil

        def _expected_session_child(child):
            # conhost hosts this process's own hidden console on Windows
            # (run_tests launches the suite with CREATE_NO_WINDOW).
            try:
                if child.name().lower() == "conhost.exe":
                    return True
            except psutil.Error:
                return False
            # multiprocessing's resource tracker is a per-process singleton
            # that lives until interpreter exit by design; the first
            # spawn-context test starts it and nothing can stop it
            # mid-session, so reporting it every run is pure noise.
            try:
                cmdline = " ".join(child.cmdline())
            except Exception:
                return False
            return "multiprocessing.resource_tracker" in cmdline

        grandchildren = [
            child
            for child in psutil.Process().children(recursive=True)
            if not _expected_session_child(child)
        ]
        survivors = []
        for child in grandchildren:
            try:
                child.wait(timeout=max(0.0, deadline - time.monotonic()))
            except psutil.TimeoutExpired:
                survivors.append(child)
            except psutil.NoSuchProcess:
                pass
            except psutil.Error:
                survivors.append(child)
        grandchildren = survivors
    except Exception as exc:
        sys.stderr.write(f"\n[conftest] could not inspect subprocesses: {exc}\n")
        sys.stderr.flush()
        grandchildren = []
    if grandchildren:
        details = []
        for child in grandchildren:
            try:
                details.append(f"{child.pid} {' '.join(child.cmdline()[:4])}")
            except Exception:
                details.append(str(child.pid))
        sys.stderr.write(
            f"\n[conftest] subprocesses alive after the session: {'; '.join(details)}\n"
        )
        sys.stderr.flush()
    if lingering or children or grandchildren:
        import faulthandler

        names = ", ".join(thread.name for thread in lingering) or "none"
        procs = ", ".join(str(child.pid) for child in children) or "none"
        sys.stderr.write(
            f"\n[conftest] leaked non-daemon threads: {names}; "
            f"live child processes: {procs}\n"
            "[conftest] dumping stacks and forcing exit to avoid a hang\n"
        )
        faulthandler.dump_traceback(file=sys.stderr)
        sys.stderr.flush()
        os._exit(_session_exit_status or int(pytest.ExitCode.TESTS_FAILED))
    return result


def _isolate_user_paths(monkeypatch, root):
    root = root.resolve()
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    hf_home = home / ".cache" / "huggingface"
    paths = {
        "AUDIOBOOK_DATA_DIR": root / "data",
        "AUDIOBOOK_OUTPUT_DIR": root / "output",
        "AUDIOBOOK_TEMP_DIR": root / "temp",
        "HOME": home,
        "USERPROFILE": home,
        "LOCALAPPDATA": home / "AppData" / "Local",
        "APPDATA": home / "AppData" / "Roaming",
        "XDG_DATA_HOME": home / ".local" / "share",
        "XDG_CACHE_HOME": home / ".cache",
        "HF_HOME": hf_home,
        "HF_HUB_CACHE": hf_home / "hub",
        "HUGGINGFACE_HUB_CACHE": hf_home / "hub",
        "HF_ASSETS_CACHE": hf_home / "assets",
        "HUGGINGFACE_ASSETS_CACHE": hf_home / "assets",
        "HF_XET_CACHE": hf_home / "xet",
        "HF_TOKEN_PATH": hf_home / "token",
        "HF_STORED_TOKENS_PATH": hf_home / "stored_tokens",
    }
    for name, path in paths.items():
        monkeypatch.setenv(name, str(path))
    # An empty override derives the database from data_dir and blocks .env.local defaults.
    monkeypatch.setenv("AUDIOBOOK_DB_PATH", "")
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_OIDC_RESOURCE", "HF_OIDC_ID_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    # Do not import Hub here: tests also exercise its first-import behaviour.
    constants = sys.modules.get("huggingface_hub.constants")
    if constants is not None:
        for name, path in paths.items():
            if hasattr(constants, name):
                monkeypatch.setattr(constants, name, str(path))
        for name, path in (
            ("default_home", home / ".cache"),
            ("default_cache_path", hf_home / "hub"),
            ("default_assets_cache_path", hf_home / "assets"),
            ("default_xet_cache_path", hf_home / "xet"),
        ):
            if hasattr(constants, name):
                monkeypatch.setattr(constants, name, str(path))
    cache_manager = sys.modules.get("huggingface_hub.utils._cache_manager")
    if cache_manager is not None:
        monkeypatch.setattr(cache_manager, "HF_HUB_CACHE", str(hf_home / "hub"))


@pytest.fixture(autouse=True)
def _isolated_user_paths(monkeypatch, tmp_path_factory):
    """Isolate user defaults while allowing tests to supply their own overrides."""
    root = tmp_path_factory.mktemp("user-paths")
    _isolate_user_paths(monkeypatch, root)
    return root


@pytest.fixture(autouse=True)
def _deterministic_host_memory(monkeypatch, _isolated_user_paths):
    """Pin the host-RAM probe so worker-count math never depends on the machine.

    VRAMBudget defaults to 70% of physical RAM; on small CI runners that
    clamps max_workers to 1 and breaks any test asserting a worker count.
    Tests that exercise the clamp itself pass an explicit budget.
    """
    from modules.pipeline import parallel

    monkeypatch.setattr(parallel, "_detect_physical_ram_bytes", lambda: 32 * 1024**3)
    parallel._detect_available_ram_gb.cache_clear()
    yield
    parallel._detect_available_ram_gb.cache_clear()


@pytest.fixture
def registry_snapshot():
    """Preserve registry contents across tests that register custom backends."""
    from modules.tts.cleaner_backends.registry import _REGISTRY

    original = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(original)

@pytest.fixture
def test_data_dir():
    """Return path to test fixtures directory."""
    return Path(__file__).parent / "fixtures"

@pytest.fixture
def sample_pdf(test_data_dir):
    """Return path to the committed sample PDF fixture."""
    path = test_data_dir / "sample.pdf"
    if not path.is_file():
        pytest.fail(f"Missing committed PDF fixture: {path}")
    return path

@pytest.fixture
def sample_epub(test_data_dir):
    """Return path to the committed sample EPUB fixture."""
    path = test_data_dir / "sample.epub"
    if not path.is_file():
        pytest.fail(f"Missing committed EPUB fixture: {path}")
    return path

# Shared request-only fixtures for the module regression tests. The autouse
# isolated_paths fixture stays imported by the modules that need it.
from tests._factories_group2_conversion import conversion  # noqa: E402, F401
from tests._factories_group2_library import library  # noqa: E402, F401
from tests._factories_group2_paths import backend, speech_paths  # noqa: E402, F401
from tests._factories_group2_setup import setup_context  # noqa: E402, F401
