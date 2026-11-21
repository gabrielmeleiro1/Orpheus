"""Persist and coordinate runtime setup and backend startup state."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any, BinaryIO, Iterable, Mapping
import uuid

from modules.contracts.durable_files import atomic_write_json, sync_directory_if_openable
from modules.contracts.process_locks import open_process_lock
from modules.runtime_profiles import get_runtime_profile
from modules.runtime_setup.errors import (
    SetupExecutionError as SetupExecutionError,
    SetupValidationError as SetupValidationError,
)
from modules.runtime_setup.selection import _validated_model_groups, validate_model_selection


STATE_SCHEMA_VERSION = 3
STATE_FILENAME = "runtime-setup.json"
# Tauri bundle identifier (desktop/src-tauri/tauri.conf.json); names the app data directory.
DESKTOP_APP_IDENTIFIER = "com.orpheus.desktop"
BACKEND_INSTANCE_FILENAME = "backend-instance.json"
LOCK_STALE_SECONDS = 300.0
DEFAULT_LOCK_TIMEOUT_SECONDS = 2.0
# Process start times and wall-clock lock timestamps come from different
# clocks; only a clearly later start proves a recorded lock owner's PID was reused.
_PID_REUSE_CLOCK_TOLERANCE_SECONDS = 5.0
_setup_gate_handles: set[BinaryIO] = set()


def _close_inherited_setup_gates() -> None:
    for handle in _setup_gate_handles:
        handle.close()
    _setup_gate_handles.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_close_inherited_setup_gates)


class SetupLock:
    """Cross-platform lock-directory guard shared by setup and acknowledgement."""

    _released_owners: dict[Path, tuple[int, str, int, int]] = {}

    def __init__(self, state_path: Path, *, timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS):
        self.path = state_path.with_name(f"{state_path.name}.lock")
        self.timeout_seconds = float(timeout_seconds)
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds < 0:
            raise SetupValidationError("Lock timeout must be a finite non-negative number.")
        self.token = uuid.uuid4().hex
        self._owned = False

    def __enter__(self) -> "SetupLock":
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                with self._metadata_gate(deadline):
                    try:
                        self.path.mkdir()
                    except FileExistsError:
                        self._remove_stale_lock_locked()
                    else:
                        try:
                            _atomic_write_json(
                                self.path / "owner.json",
                                {"pid": os.getpid(), "created_at": time.time(), "token": self.token},
                            )
                        except OSError:
                            shutil.rmtree(self.path, ignore_errors=True)
                            raise
                        self._owned = True
                        self._released_owners.pop(self.path.absolute(), None)
                        return self
            except OSError as exc:
                raise SetupExecutionError(f"Could not acquire runtime setup lock: {exc}") from exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SetupExecutionError("Another runtime setup operation is already running.")
            time.sleep(min(0.05, remaining))

    @contextmanager
    def _metadata_gate(self, deadline: float):
        """Serialize directory transitions through an inode that is never removed."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        gate_path = self.path.with_name(f"{self.path.name}.gate")
        while True:
            opened = []

            def register(handle):
                opened.append(handle)
                _setup_gate_handles.add(handle)

            try:
                try:
                    open_process_lock(gate_path, blocking=False, on_open=register)
                except BlockingIOError:
                    pass
                else:
                    yield
                    return
            finally:
                for handle in opened:
                    _setup_gate_handles.discard(handle)
                    handle.close()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SetupExecutionError("Another runtime setup operation is already running.")
            time.sleep(min(0.05, remaining))

    def _remove_stale_lock_locked(self) -> None:
        released = None
        try:
            metadata = json.loads((self.path / "owner.json").read_text(encoding="utf-8"))
            pid = int(metadata["pid"])
            created_at = float(metadata["created_at"])
            age = time.time() - created_at
            identity = self.path.stat()
            released = self._released_owners.get(self.path.absolute())
            # Only the exact directory we released can bypass a live owner.
            own_released_lock = released == (
                os.getpid(), metadata.get("token"), identity.st_dev, identity.st_ino,
            ) and pid == os.getpid()
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return
            pid = -1
            created_at = 0.0
            own_released_lock = False
        if not own_released_lock and (
            age < 0 or (pid > 0 and _pid_is_running(pid) and not _pid_reused_since(pid, created_at))
        ):
            return
        # A recorded owner that no longer exists cannot still hold the lock.
        # Recover it immediately so an interrupted download does not block the
        # next app launch for the generic ambiguous-lock grace period.
        if pid <= 0 and age < LOCK_STALE_SECONDS:
            return
        stale_path = self.path.with_name(f"{self.path.name}.stale-{uuid.uuid4().hex}")
        try:
            os.replace(self.path, stale_path)
        except OSError:
            return
        if released is not None and self._released_owners.get(self.path.absolute()) == released:
            self._released_owners.pop(self.path.absolute(), None)
        shutil.rmtree(stale_path, ignore_errors=True)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if not self._owned:
            return
        # If the gate cannot be acquired, preserve ownership and surface the
        # bounded failure so the caller can retry instead of abandoning a live lock.
        with self._metadata_gate(time.monotonic() + self.timeout_seconds):
            self._release_owned_lock()

    def _release_owned_lock(self) -> None:
        try:
            for attempt in range(3):
                metadata = json.loads((self.path / "owner.json").read_text(encoding="utf-8"))
                if metadata.get("token") != self.token or metadata.get("pid") != os.getpid():
                    return
                identity = self.path.stat()
                # An open owner file can delay Windows removal beyond this release.
                released = (
                    os.getpid(), self.token, identity.st_dev, identity.st_ino,
                )
                self._released_owners[self.path.absolute()] = released
                try:
                    shutil.rmtree(self.path)
                except OSError:
                    if attempt == 2:
                        return
                    time.sleep(0.05)
                else:
                    if self._released_owners.get(self.path.absolute()) == released:
                        self._released_owners.pop(self.path.absolute(), None)
                    return
        except (OSError, json.JSONDecodeError):
            return
        finally:
            self._owned = False


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_is_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_reused_since(pid: int, recorded_at: float) -> bool:
    """Whether ``pid`` now names a process started after ``recorded_at``.

    The owner records the lock after it starts, so a process created later
    cannot be that owner: the owner exited and the OS reused its PID (common on
    Windows). Without this, a crash while holding the lock would block setup
    until the unrelated process exits. Any ambiguity keeps the lock held.
    """
    try:
        import psutil

        started_at = psutil.Process(pid).create_time()
    except Exception:
        return False
    return started_at > recorded_at + _PID_REUSE_CLOCK_TOLERANCE_SECONDS


def _windows_pid_is_running(pid: int) -> bool:
    # os.kill(pid, 0) is a trap on Windows: signal 0 equals CTRL_C_EVENT, so
    # instead of probing the pid it broadcasts a console Ctrl+C — and when the
    # broadcast fails it raises, making a live owner look dead and letting a
    # concurrent setup steal a held lock. Probe through the process API.
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_ACCESS_DENIED = 5
    STILL_ACTIVE = 259

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Denied access still proves the process exists; treat the owner as
        # alive so the lock is never stolen on an ambiguous answer.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def default_state_path(*, environ: Mapping[str, str] | None = None) -> Path:
    """Resolve setup state beside the desktop app's existing data directory.

    Without ``AUDIOBOOK_DATA_DIR`` this is the desktop shell's own app data
    directory (Tauri ``app_data_dir`` for the ``com.orpheus.desktop``
    identifier), so a source-checkout setup and the desktop app share state.
    """
    env = os.environ if environ is None else environ
    configured_data_dir = str(env.get("AUDIOBOOK_DATA_DIR", "")).strip()
    if configured_data_dir:
        return Path(configured_data_dir).expanduser().resolve().parent / STATE_FILENAME

    home = Path.home()
    if sys.platform == "darwin":
        root = home / "Library" / "Application Support"
    elif os.name == "nt":
        root = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
    else:
        root = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    return root / DESKTOP_APP_IDENTIFIER / STATE_FILENAME


def _legacy_default_state_path(environ: Mapping[str, str] | None = None) -> Path:
    """Where earlier builds kept standalone setup state (read-only fallback).

    On Windows that folder is also the installer's install directory.
    """
    env = os.environ if environ is None else environ
    home = Path.home()
    if sys.platform == "darwin":
        root = home / "Library" / "Application Support" / "Orpheus"
    elif os.name == "nt":
        root = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local") / "Orpheus"
    else:
        root = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "orpheus"
    return root / STATE_FILENAME


def _readable_state_path(state_path: Path) -> Path:
    """Read legacy standalone state until the current default is first written.

    The legacy file is never written, and only a runtime reset removes it; the
    next state write lands at the current default and takes precedence from
    then on.
    """
    if state_path.is_file() or str(os.environ.get("AUDIOBOOK_DATA_DIR", "")).strip():
        return state_path
    if state_path != default_state_path():
        return state_path
    legacy = _legacy_default_state_path()
    return legacy if legacy.is_file() else state_path


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        path, payload, sort_keys=True, separators=(",", ":"), trailing_newline=True,
        permissions=0o600, sync_parent=sync_directory_if_openable,
    )


def pending_model_deletions(
    state_path: Path, *, installation_id: str | None = None,
) -> list[tuple[Path, dict[str, Any]]]:
    """Block state replacement until deletion journals are resolved under the lock.

    Only the transaction currently removing models may write its own commit.
    Even unreadable journals must retain the installation evidence they need.
    """
    from modules.hf_cache import hf_hub_cache_path

    # The source installer checks journals before pip can install Hub. Honor
    # already-loaded constants in the app, and use the same stdlib-only cache
    # resolution on a fresh environment instead of importing that dependency.
    hf_constants = sys.modules.get("huggingface_hub.constants")
    cache_root = Path(hf_constants.HF_HUB_CACHE) if hf_constants is not None else hf_hub_cache_path()

    pending = []
    for path in cache_root.glob(".orpheus-delete-*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if not path.is_symlink() else None
            current, error = _read_setup_state(state_path)
            if (
                installation_id is None or not isinstance(data, dict)
                or data.get("installation_id") != installation_id
                or data.get("settings_committed") is not False
                or "previous_installation_id" not in data or error
                or data["previous_installation_id"] != (current["installation_id"] if current else None)
            ):
                raise SetupExecutionError("Pending model deletion must be resolved before replacing setup state.")
        except (OSError, ValueError) as exc:
            raise SetupExecutionError("Pending model deletion could not be read.") from exc
        pending.append((path, data))
    return pending


def write_setup_state(
    state_path: Path,
    *,
    profile_id: str,
    model_groups: Iterable[str],
    installation_id: str | None = None,
    backend_instance_id: str | None = None,
    restart_required: bool = True,
    cleaner_cuda_quantization: str = "4bit",
) -> dict[str, Any]:
    """Atomically write the deliberately small, non-secret setup state.

    Installer callers keep ``restart_required=True`` (the backend must reload
    to pick up freshly installed packages). The adopt flow passes ``False``
    because the already-running backend has the runtime loaded already.
    """
    profile = get_runtime_profile(profile_id)
    groups = _validated_model_groups(profile, model_groups)
    validate_model_selection(groups)
    if cleaner_cuda_quantization not in ("4bit", "8bit", "fp16"):
        raise SetupValidationError("Unsupported cleaner quantization.")
    installed_at = datetime.now(timezone.utc)
    if backend_instance_id is not None and not re.fullmatch(r"[0-9a-f]{32}", backend_instance_id):
        raise SetupValidationError("Invalid backend instance identity.")
    payload: dict[str, Any] = {
        "installed_at": installed_at.isoformat(),
        "installation_id": installation_id or uuid.uuid4().hex,
        "installed_during_backend_instance_id": backend_instance_id,
        "installed_without_backend_instance": backend_instance_id is None,
        "model_groups": list(groups),
        "cleaner_cuda_quantization": cleaner_cuda_quantization,
        "profile": profile.id,
        "restart_required": bool(restart_required),
        "schema_version": STATE_SCHEMA_VERSION,
    }
    try:
        pending = pending_model_deletions(state_path, installation_id=installation_id)
        _atomic_write_json(state_path, payload)
        for path, data in pending:
            data["settings_committed"] = True
            _atomic_write_json(path, data)
    except OSError as exc:
        raise SetupExecutionError(f"Could not write runtime setup state: {exc}") from exc
    return payload


def _read_setup_state(state_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    state_path = _readable_state_path(state_path)
    if not state_path.is_file():
        return None, None
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != STATE_SCHEMA_VERSION:
            raise ValueError("unsupported setup state schema")
        profile = get_runtime_profile(str(raw.get("profile", "")))
        groups = _validated_model_groups(profile, raw.get("model_groups", ()))
        quantization = raw.get("cleaner_cuda_quantization", "4bit")
        if quantization not in ("4bit", "8bit", "fp16"):
            raise ValueError("unsupported cleaner quantization")
        installation_id = str(raw.get("installation_id", ""))
        if not re.fullmatch(r"[0-9a-f]{32}", installation_id):
            raise ValueError("invalid installation identity")
        installed_backend_id = raw.get("installed_during_backend_instance_id")
        if installed_backend_id is not None and not re.fullmatch(
            r"[0-9a-f]{32}", str(installed_backend_id)
        ):
            raise ValueError("invalid install-time backend identity")
        installed_without_backend = bool(raw.get("installed_without_backend_instance", False))
        if installed_without_backend != (installed_backend_id is None):
            raise ValueError("inconsistent install-time backend identity")
        return {
            "installed_at": str(raw.get("installed_at", "")),
            "installation_id": installation_id,
            "installed_during_backend_instance_id": installed_backend_id,
            "installed_without_backend_instance": installed_without_backend,
            "model_groups": list(groups),
            "cleaner_cuda_quantization": quantization,
            "profile": profile.id,
            "restart_required": bool(raw.get("restart_required", False)),
            "schema_version": STATE_SCHEMA_VERSION,
        }, None
    except OSError as exc:
        raise SetupExecutionError(f"Could not read runtime setup state: {exc}") from exc
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        # RecursionError: deeply nested JSON exhausts the decoder's stack.
        return None, f"Setup state is invalid: {exc}"


def read_setup_state(state_path: Path) -> dict[str, Any]:
    """Return validated state for startup integration and focused diagnostics."""
    state, error = _read_setup_state(state_path)
    if state is None:
        raise SetupExecutionError(error or "Runtime setup state does not exist.")
    return state


def get_setup_state_cache_key(
    *,
    state_path: Path | None = None,
) -> tuple[Any, ...]:
    """Return a non-secret identity for setup-status cache invalidation."""
    path = _readable_state_path(state_path or default_state_path())
    try:
        stat = path.stat()
    except FileNotFoundError:
        return ("missing",)
    except OSError:
        return ("unavailable",)
    try:
        state, error = _read_setup_state(path)
    except SetupExecutionError:
        return ("unavailable", stat.st_mtime_ns, stat.st_size)
    if state is None:
        return ("invalid", stat.st_mtime_ns, stat.st_size, bool(error))
    return (
        "configured",
        stat.st_mtime_ns,
        stat.st_size,
        state["installation_id"],
        state["profile"],
        tuple(state["model_groups"]),
    )


def _backend_instance_path(state_path: Path) -> Path:
    return state_path.with_name(BACKEND_INSTANCE_FILENAME)


def _read_backend_instance(state_path: Path) -> tuple[str | None, str]:
    path = _backend_instance_path(state_path)
    if not path.is_file():
        return None, "absent"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        instance_id = str(payload.get("backend_instance_id", ""))
    except (OSError, AttributeError, json.JSONDecodeError) as exc:
        raise SetupExecutionError(f"Backend instance state is corrupt: {exc}") from exc
    if not re.fullmatch(r"[0-9a-f]{32}", instance_id):
        raise SetupExecutionError("Backend instance state is corrupt: invalid identity.")
    return instance_id, "valid"


def persist_backend_instance(
    backend_instance_id: str,
    *,
    state_path: Path | None = None,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> None:
    """Persist the nonce owned by this desktop backend lifespan."""
    if not re.fullmatch(r"[0-9a-f]{32}", backend_instance_id):
        raise SetupValidationError("Invalid backend instance identity.")
    path = state_path or default_state_path()
    with SetupLock(path, timeout_seconds=lock_timeout_seconds):
        try:
            _atomic_write_json(
                _backend_instance_path(path),
                {"backend_instance_id": backend_instance_id},
            )
        except OSError as exc:
            raise SetupExecutionError(f"Could not persist backend instance: {exc}") from exc


def acknowledge_backend_startup(
    backend_instance_id: str,
    *,
    state_path: Path | None = None,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> bool:
    """Acknowledge only when this lifespan nonce differs from the install-time nonce."""
    if not re.fullmatch(r"[0-9a-f]{32}", backend_instance_id):
        raise SetupValidationError("Invalid backend instance identity.")
    path = state_path or default_state_path()
    with SetupLock(path, timeout_seconds=lock_timeout_seconds):
        persisted_instance_id, status = _read_backend_instance(path)
        if status != "valid" or persisted_instance_id != backend_instance_id:
            return False
        state, error = _read_setup_state(path)
        if error is not None or state is None:
            return False
        if not state["restart_required"]:
            return True
        installed_instance_id = state["installed_during_backend_instance_id"]
        if installed_instance_id == backend_instance_id:
            return False
        if installed_instance_id is None and not state["installed_without_backend_instance"]:
            return False
        return _compare_and_swap_restart_ack(
            path,
            expected_installation_id=state["installation_id"],
        )


def _compare_and_swap_restart_ack(
    state_path: Path,
    *,
    expected_installation_id: str,
) -> bool:
    """Update only the installation state that was evaluated by the caller."""
    current, error = _read_setup_state(state_path)
    if error is not None or current is None:
        return False
    if current["installation_id"] != expected_installation_id:
        return False
    if not current["restart_required"]:
        return True
    current["restart_required"] = False
    try:
        _atomic_write_json(state_path, current)
    except OSError as exc:
        raise SetupExecutionError(f"Could not acknowledge backend restart: {exc}") from exc
    return True
