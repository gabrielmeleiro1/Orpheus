#!/usr/bin/env python3
"""Install one allowlisted Orpheus runtime profile and optional model groups."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
import uuid


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.app.services.runtime_capability_service import (  # noqa: E402
    RuntimeCapabilityService,
)
from modules.runtime_profiles import (  # noqa: E402
    RuntimeProfile,
    get_runtime_profile,
    runtime_profiles,
)


STATE_SCHEMA_VERSION = 3
STATE_FILENAME = "runtime-setup.json"
BACKEND_INSTANCE_FILENAME = "backend-instance.json"
LOCK_STALE_SECONDS = 300.0
DEFAULT_LOCK_TIMEOUT_SECONDS = 2.0
DEFAULT_PIP_TIMEOUT_SECONDS = 900.0
DEFAULT_MODEL_TIMEOUT_SECONDS = 3600.0
DIAGNOSTIC_MAX_CHARS = 2000
TAURI_SUPERVISED_ENV = "ORPHEUS_TAURI_SETUP_SUPERVISED"
SETUP_PLAN_PERCENT = 5
SETUP_DEPENDENCY_START_PERCENT = 10
SETUP_DEPENDENCY_COMPLETE_PERCENT = 50
SETUP_MODEL_START_PERCENT = 55
SETUP_MODEL_COMPLETE_PERCENT = 80
SETUP_STATE_START_PERCENT = 90
# 100 is reserved for the authoritative terminal success event emitted by
# main() after run_setup() returns successfully.
SETUP_STATE_COMPLETE_PERCENT = SETUP_STATE_START_PERCENT
SETUP_COMPLETE_PERCENT = 100
PYTORCH_RISK_ID = "PYSEC-2026-139/CVE-2026-4538,CVE-2025-3000"
PYTORCH_RISK_SUMMARY = (
    "PyTorch 2.10 has unresolved local deserialization and memory-corruption "
    "advisories with no fixed upstream release. Use trusted model artifacts only."
)


class SetupValidationError(ValueError):
    """Raised before mutation when a requested setup is not allowlisted."""


class SetupExecutionError(RuntimeError):
    """Raised when a fixed setup subprocess fails."""


class _SetupArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise SetupValidationError(message)


class _ProbeService(Protocol):
    def probe_mlx_tts_runtime(self) -> Mapping[str, Any]: ...

    def probe_package_tts_runtime(self) -> Mapping[str, Any]: ...

    def probe_cleaner_backend(self, backend: str) -> Mapping[str, Any]: ...


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
EventEmitter = Callable[[Mapping[str, Any]], None]


@dataclass(frozen=True)
class InstallPlan:
    """Fully validated commands for one setup run."""

    profile: RuntimeProfile
    model_groups: tuple[str, ...]
    pip_command: tuple[str, ...]
    model_command: tuple[str, ...] | None
    accepted_risks: tuple[str, ...]

    @property
    def commands(self) -> tuple[tuple[str, ...], ...]:
        if self.model_command is None:
            return (self.pip_command,)
        return (self.pip_command, self.model_command)


class SetupLock:
    """Cross-platform lock-directory guard shared by setup and acknowledgement."""

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
                self.path.mkdir(parents=True)
                try:
                    _atomic_write_json(
                        self.path / "owner.json",
                        {"pid": os.getpid(), "created_at": time.time(), "token": self.token},
                    )
                except OSError:
                    shutil.rmtree(self.path, ignore_errors=True)
                    raise
                self._owned = True
                return self
            except FileExistsError:
                self._remove_stale_lock()
                if time.monotonic() >= deadline:
                    raise SetupExecutionError("Another runtime setup operation is already running.")
                time.sleep(0.05)
            except OSError as exc:
                raise SetupExecutionError(f"Could not acquire runtime setup lock: {exc}") from exc

    def _remove_stale_lock(self) -> None:
        try:
            metadata = json.loads((self.path / "owner.json").read_text(encoding="utf-8"))
            pid = int(metadata["pid"])
            age = time.time() - float(metadata["created_at"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return
            pid = -1
        if age < 0 or age < LOCK_STALE_SECONDS or (pid > 0 and _pid_is_running(pid)):
            return
        stale_path = self.path.with_name(f"{self.path.name}.stale-{uuid.uuid4().hex}")
        try:
            os.replace(self.path, stale_path)
        except OSError:
            return
        shutil.rmtree(stale_path, ignore_errors=True)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if not self._owned:
            return
        try:
            metadata = json.loads((self.path / "owner.json").read_text(encoding="utf-8"))
            if metadata.get("token") != self.token:
                return
            shutil.rmtree(self.path)
        except (OSError, json.JSONDecodeError):
            return
        finally:
            self._owned = False


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _normalize_identifier(value: str, *, kind: str) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        raise SetupValidationError(f"{kind} must not be empty.")
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in normalized):
        raise SetupValidationError(f"Invalid {kind} identifier: {value!r}.")
    return normalized


def _validated_profile(profile_id: str) -> RuntimeProfile:
    normalized = _normalize_identifier(profile_id, kind="profile")
    try:
        profile = get_runtime_profile(normalized)
    except ValueError as exc:
        raise SetupValidationError(str(exc)) from exc
    if not profile.is_compatible():
        reason = profile.incompatibility_reason() or "profile is unavailable"
        raise SetupValidationError(f"Runtime profile {profile.id!r} is unavailable: {reason}")
    return profile


def _validated_model_groups(
    profile: RuntimeProfile,
    model_groups: Iterable[str],
) -> tuple[str, ...]:
    normalized_groups = tuple(
        dict.fromkeys(
            _normalize_identifier(group_id, kind="model group") for group_id in model_groups
        )
    )
    unavailable = [
        group_id for group_id in normalized_groups if group_id not in profile.model_groups
    ]
    if unavailable:
        available = ", ".join(profile.model_groups) or "none"
        raise SetupValidationError(
            f"Model group {unavailable[0]!r} is not available for profile "
            f"{profile.id!r}. Allowed groups: {available}."
        )
    return normalized_groups


def _fixed_requirements_path(profile: RuntimeProfile) -> Path:
    requirements_root = (REPO_ROOT / "requirements").resolve()
    path = profile.requirements_file.resolve()
    if path.parent != requirements_root or path.name != f"{profile.id}.txt":
        raise SetupValidationError(
            f"Profile {profile.id!r} does not reference its fixed requirements manifest."
        )
    if not path.is_file():
        raise SetupValidationError(f"Requirements manifest is missing: {path.name}.")
    return path


def build_install_plan(
    profile_id: str,
    model_groups: Iterable[str] = (),
    *,
    accept_pytorch_risk: bool = False,
) -> InstallPlan:
    """Validate identifiers and return fixed, shell-free argument arrays."""
    profile = _validated_profile(profile_id)
    groups = _validated_model_groups(profile, model_groups)
    if profile.id == "cuda" and not accept_pytorch_risk:
        raise SetupValidationError(
            "CUDA setup requires explicit acknowledgement with "
            "--accept-pytorch-risk. " + PYTORCH_RISK_SUMMARY
        )

    executable = sys.executable
    pip_command = (
        executable,
        "-m",
        "pip",
        "install",
        "-r",
        str(_fixed_requirements_path(profile)),
    )
    model_command: tuple[str, ...] | None = None
    if groups:
        downloader = (REPO_ROOT / "scripts" / "download_models.py").resolve()
        if not downloader.is_file():
            raise SetupValidationError("The fixed model downloader is missing.")
        arguments: list[str] = [executable, str(downloader)]
        for group_id in groups:
            arguments.extend(("--group", group_id))
        arguments.append("--json")
        model_command = tuple(arguments)

    accepted_risks = (PYTORCH_RISK_ID,) if profile.id == "cuda" else ()
    return InstallPlan(
        profile=profile,
        model_groups=groups,
        pip_command=pip_command,
        model_command=model_command,
        accepted_risks=accepted_risks,
    )


def default_state_path(*, environ: Mapping[str, str] | None = None) -> Path:
    """Resolve setup state beside the desktop app's existing data directory."""
    env = os.environ if environ is None else environ
    configured_data_dir = str(env.get("AUDIOBOOK_DATA_DIR", "")).strip()
    if configured_data_dir:
        return Path(configured_data_dir).expanduser().resolve().parent / STATE_FILENAME

    home = Path.home()
    if sys.platform == "darwin":
        root = home / "Library" / "Application Support" / "Orpheus"
    elif os.name == "nt":
        root = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local") / "Orpheus"
    else:
        root = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "orpheus"
    return root / STATE_FILENAME


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def write_setup_state(
    state_path: Path,
    *,
    profile_id: str,
    model_groups: Iterable[str],
    accepted_risks: Iterable[str],
    installation_id: str | None = None,
    backend_instance_id: str | None = None,
    restart_required: bool = True,
) -> dict[str, Any]:
    """Atomically write the deliberately small, non-secret setup state.

    Installer callers keep ``restart_required=True`` (the backend must reload
    to pick up freshly installed packages). The adopt flow passes ``False``
    because the already-running backend has the runtime loaded already.
    """
    profile = get_runtime_profile(profile_id)
    groups = _validated_model_groups(profile, model_groups)
    risks = tuple(accepted_risks)
    allowed_risks = (PYTORCH_RISK_ID,) if profile.id == "cuda" else ()
    if any(risk not in allowed_risks for risk in risks):
        raise SetupValidationError("Setup state contains an unknown risk acknowledgement.")
    if profile.id == "cuda" and PYTORCH_RISK_ID not in risks:
        raise SetupValidationError("CUDA setup state requires PyTorch risk acknowledgement.")
    installed_at = datetime.now(timezone.utc)
    if backend_instance_id is not None and not re.fullmatch(r"[0-9a-f]{32}", backend_instance_id):
        raise SetupValidationError("Invalid backend instance identity.")
    payload: dict[str, Any] = {
        "accepted_risks": list(risks),
        "installed_at": installed_at.isoformat(),
        "installation_id": installation_id or uuid.uuid4().hex,
        "installed_during_backend_instance_id": backend_instance_id,
        "installed_without_backend_instance": backend_instance_id is None,
        "model_groups": list(groups),
        "profile": profile.id,
        "restart_required": bool(restart_required),
        "schema_version": STATE_SCHEMA_VERSION,
    }
    try:
        _atomic_write_json(state_path, payload)
    except OSError as exc:
        raise SetupExecutionError(f"Could not write runtime setup state: {exc}") from exc
    return payload


def _read_setup_state(state_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not state_path.is_file():
        return None, None
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != STATE_SCHEMA_VERSION:
            raise ValueError("unsupported setup state schema")
        profile = get_runtime_profile(str(raw.get("profile", "")))
        groups = _validated_model_groups(profile, raw.get("model_groups", ()))
        risks = tuple(str(value) for value in raw.get("accepted_risks", ()))
        allowed_risks = (PYTORCH_RISK_ID,) if profile.id == "cuda" else ()
        if any(risk not in allowed_risks for risk in risks):
            raise ValueError("unknown risk acknowledgement")
        if profile.id == "cuda" and PYTORCH_RISK_ID not in risks:
            raise ValueError("missing PyTorch risk acknowledgement")
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
            "accepted_risks": list(risks),
            "installed_at": str(raw.get("installed_at", "")),
            "installation_id": installation_id,
            "installed_during_backend_instance_id": installed_backend_id,
            "installed_without_backend_instance": installed_without_backend,
            "model_groups": list(groups),
            "profile": profile.id,
            "restart_required": bool(raw.get("restart_required", False)),
            "schema_version": STATE_SCHEMA_VERSION,
        }, None
    except OSError as exc:
        raise SetupExecutionError(f"Could not read runtime setup state: {exc}") from exc
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
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
    path = state_path or default_state_path()
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


def _combine_probe_checks(checks: Sequence[tuple[str, Mapping[str, Any]]]) -> dict[str, Any]:
    sanitized = [
        {
            "id": check_id,
            "available": bool(result.get("available", False)),
            "error": _sanitize_diagnostic(str(result.get("error")))
            if result.get("error")
            else None,
        }
        for check_id, result in checks
    ]
    return {
        "available": all(check["available"] for check in sanitized),
        "checks": sanitized,
    }


def _probe_profile(model_groups: Iterable[str], service: _ProbeService) -> dict[str, Any]:
    checks: list[tuple[str, Mapping[str, Any]]] = []
    try:
        for group_id in model_groups:
            if group_id == "tts-mlx":
                checks.append((group_id, service.probe_mlx_tts_runtime()))
            elif group_id == "tts-cpu-cuda":
                checks.append((group_id, service.probe_package_tts_runtime()))
            elif group_id == "cleaner-mlx":
                checks.append((group_id, service.probe_cleaner_backend("mlx")))
            elif group_id == "cleaner-cuda":
                checks.append((group_id, service.probe_cleaner_backend("cuda")))
    except Exception as exc:
        raise SetupExecutionError(f"Runtime probe failed: {exc}") from exc
    return _combine_probe_checks(checks)


def get_setup_status(
    *,
    state_path: Path | None = None,
    probe_service: _ProbeService | None = None,
) -> dict[str, Any]:
    """Read setup state and independently re-probe the selected runtime."""
    path = state_path or default_state_path()
    state, state_error = _read_setup_state(path)
    profiles = [
        {
            "id": profile.id,
            "compatible": profile.is_compatible(),
            "reason": profile.incompatibility_reason(),
            "model_groups": list(profile.model_groups),
            "risk": (
                {"id": PYTORCH_RISK_ID, "summary": PYTORCH_RISK_SUMMARY}
                if profile.id == "cuda"
                else None
            ),
        }
        for profile in runtime_profiles()
    ]
    if state is None:
        return {
            "configured": False,
            "profile": None,
            "model_groups": [],
            "ready": False,
            "restart_required": False,
            "probe": {"available": False, "checks": []},
            "profiles": profiles,
            "risk": None,
            "state_error": state_error,
        }

    service = probe_service or RuntimeCapabilityService(
        cache_ttl_seconds=0.0,
        timeout_seconds=15.0,
    )
    probe = _probe_profile(state["model_groups"], service)
    risk = (
        {"id": PYTORCH_RISK_ID, "summary": PYTORCH_RISK_SUMMARY, "accepted": True}
        if state["profile"] == "cuda"
        else None
    )
    return {
        "configured": True,
        "profile": state["profile"],
        "model_groups": state["model_groups"],
        "ready": bool(probe["available"]),
        "restart_required": state["restart_required"],
        "probe": probe,
        "profiles": profiles,
        "risk": risk,
        "state_error": None,
    }


def _run_fixed_command(
    command: Sequence[str],
    *,
    stage: str,
    command_runner: CommandRunner | None,
    machine_output: bool,
    emit: EventEmitter,
    timeout_seconds: float,
    start_percent: int,
    complete_percent: int,
) -> None:
    emit(
        {
            "event": "stage_started",
            "stage": stage,
            "command": list(command),
            "percent": start_percent,
        }
    )
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise SetupValidationError(f"{stage} timeout must be a finite positive number.")
    if command_runner is None:
        _supervise_command(command, stage=stage, timeout_seconds=timeout_seconds)
        emit({"event": "stage_complete", "stage": stage, "percent": complete_percent})
        return
    try:
        result = command_runner(
            list(command),
            shell=False,
            check=False,
            text=True,
            capture_output=machine_output,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise SetupExecutionError(f"{stage} timed out after {timeout_seconds:g} seconds.") from exc
    except OSError as exc:
        raise SetupExecutionError(f"{stage} could not start: {exc}") from exc
    if result.returncode != 0:
        detail = _sanitize_diagnostic(result.stderr or "")
        suffix = f" Details: {detail}" if detail else ""
        raise SetupExecutionError(f"{stage} failed with exit code {result.returncode}.{suffix}")
    emit({"event": "stage_complete", "stage": stage, "percent": complete_percent})


def _sanitize_diagnostic(value: str, *, max_chars: int = DIAGNOSTIC_MAX_CHARS) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value))
    text = re.sub(r"(?i)\b(sk-[a-z0-9_-]{8,})\b", "[redacted]", text)
    text = re.sub(
        r"(?i)\b(api[_-]?key|token|authorization|password)\s*[:=]\s*[^\s,;]+",
        lambda match: f"{match.group(1)}=[redacted]",
        text,
    )
    text = re.sub(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@", r"\1[redacted]@", text)
    home = str(Path.home())
    if home:
        text = text.replace(home, "<home>")
    text = re.sub(r"(?<![A-Za-z0-9:])/(?:[^/\s]+/)*[^/\s]*", "<path>", text)
    text = re.sub(r"(?i)\b[A-Z]:\\(?:[^\\\s]+\\)*[^\\\s]*", "<path>", text)
    text = text.strip()
    if len(text) > max_chars:
        text = "…" + text[-max_chars:]
    return text


def _bounded_file_tail(path: Path, *, max_bytes: int = 8192) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                shell=False,
                check=False,
                capture_output=True,
                timeout=5,
            )
            process.wait(timeout=3)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=3)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return


def _supervise_command(
    command: Sequence[str],
    *,
    stage: str,
    timeout_seconds: float,
) -> None:
    with tempfile.TemporaryDirectory(prefix="orpheus-setup-output-") as temp_dir:
        stdout_path = Path(temp_dir) / "stdout.log"
        stderr_path = Path(temp_dir) / "stderr.log"
        popen_kwargs = _stage_process_group_kwargs()
        try:
            with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
                process = subprocess.Popen(
                    list(command),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    shell=False,
                    **popen_kwargs,
                )
                try:
                    return_code = process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired as exc:
                    _terminate_process_tree(process)
                    detail = _sanitize_diagnostic(_bounded_file_tail(stderr_path))
                    suffix = f" Details: {detail}" if detail else ""
                    raise SetupExecutionError(
                        f"{stage} timed out after {timeout_seconds:g} seconds.{suffix}"
                    ) from exc
        except SetupExecutionError:
            raise
        except OSError as exc:
            raise SetupExecutionError(f"{stage} could not start: {exc}") from exc
        if return_code != 0:
            detail = _sanitize_diagnostic(_bounded_file_tail(stderr_path))
            suffix = f" Details: {detail}" if detail else ""
            raise SetupExecutionError(f"{stage} failed with exit code {return_code}.{suffix}")


def _stage_process_group_kwargs(
    *, environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Keep Tauri-launched stages in its OS-owned containment boundary."""
    env = os.environ if environ is None else environ
    if str(env.get(TAURI_SUPERVISED_ENV, "")).strip() == "1":
        return {}
    if os.name == "posix":
        return {"start_new_session": True}
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {}


def run_setup(
    *,
    profile_id: str,
    model_groups: Iterable[str] = (),
    accept_pytorch_risk: bool = False,
    dry_run: bool = False,
    state_path: Path | None = None,
    command_runner: CommandRunner | None = None,
    emit: EventEmitter | None = None,
    machine_output: bool = False,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    pip_timeout_seconds: float = DEFAULT_PIP_TIMEOUT_SECONDS,
    model_timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Validate, install dependencies/models, then atomically record state."""
    plan = build_install_plan(
        profile_id,
        model_groups,
        accept_pytorch_risk=accept_pytorch_risk,
    )
    result: dict[str, Any] = {
        "profile": plan.profile.id,
        "model_groups": list(plan.model_groups),
        "commands": [list(command) for command in plan.commands],
        "dry_run": dry_run,
        "restart_required": not dry_run,
        "risk": (
            {"id": PYTORCH_RISK_ID, "summary": PYTORCH_RISK_SUMMARY, "accepted": True}
            if plan.profile.id == "cuda"
            else None
        ),
    }
    event_emitter = emit or (lambda event: None)
    event_emitter({"event": "plan", "percent": SETUP_PLAN_PERCENT, **result})
    if dry_run:
        return result

    setup_state_path = state_path or default_state_path()
    with SetupLock(setup_state_path, timeout_seconds=lock_timeout_seconds):
        backend_instance_id, _ = _read_backend_instance(setup_state_path)
        _run_fixed_command(
            plan.pip_command,
            stage="Dependency installation",
            command_runner=command_runner,
            machine_output=machine_output,
            emit=event_emitter,
            timeout_seconds=pip_timeout_seconds,
            start_percent=SETUP_DEPENDENCY_START_PERCENT,
            complete_percent=SETUP_DEPENDENCY_COMPLETE_PERCENT,
        )
        if plan.model_command is not None:
            _run_fixed_command(
                plan.model_command,
                stage="Model download",
                command_runner=command_runner,
                machine_output=machine_output,
                emit=event_emitter,
                timeout_seconds=model_timeout_seconds,
                start_percent=SETUP_MODEL_START_PERCENT,
                complete_percent=SETUP_MODEL_COMPLETE_PERCENT,
            )
        event_emitter(
            {
                "event": "stage_started",
                "stage": "Verification",
                "message": "Verifying setup and saving state.",
                "percent": SETUP_STATE_START_PERCENT,
            }
        )
        write_setup_state(
            setup_state_path,
            profile_id=plan.profile.id,
            model_groups=plan.model_groups,
            accepted_risks=plan.accepted_risks,
            backend_instance_id=backend_instance_id,
        )
        event_emitter(
            {
                "event": "stage_complete",
                "stage": "Verification",
                "message": "Setup state saved.",
                "percent": SETUP_STATE_COMPLETE_PERCENT,
            }
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = _SetupArgumentParser(
        description="Install an allowlisted Orpheus runtime profile.",
    )
    parser.add_argument("--profile", help="Runtime profile: base, mlx, or cuda.")
    parser.add_argument(
        "--models",
        "--model-group",
        "--group",
        action="append",
        dest="model_groups",
        default=[],
        help="Allowlisted model group id. May be repeated.",
    )
    parser.add_argument("--accept-pytorch-risk", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--lock-timeout", type=float, default=DEFAULT_LOCK_TIMEOUT_SECONDS)
    parser.add_argument("--pip-timeout", type=float, default=DEFAULT_PIP_TIMEOUT_SECONDS)
    parser.add_argument("--model-timeout", type=float, default=DEFAULT_MODEL_TIMEOUT_SECONDS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--progress-json", action="store_true")
    return parser


def _print_event(
    event: Mapping[str, Any],
    *,
    stream: Any | None = None,
) -> None:
    print(
        json.dumps(dict(event), sort_keys=True),
        file=stream or sys.stdout,
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    requested_machine_output = any(flag in raw_argv for flag in ("--json", "--progress-json"))
    try:
        args = build_parser().parse_args(raw_argv)
    except SetupValidationError as exc:
        payload = {
            "error": "validation_error",
            "event": "error",
            "message": str(exc),
            "terminal": True,
        }
        if requested_machine_output:
            _print_event(payload, stream=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        return 2
    machine_output = bool(args.json or args.progress_json)

    try:
        if args.status:
            status = get_setup_status()
            if machine_output:
                _print_event({"event": "status", "terminal": True, **status})
            else:
                print(json.dumps(status, indent=2, sort_keys=True))
            return 0

        if not args.profile:
            raise SetupValidationError("--profile is required unless --status is used.")

        def emit(event: Mapping[str, Any]) -> None:
            if args.progress_json:
                _print_event(event)

        result = run_setup(
            profile_id=args.profile,
            model_groups=args.model_groups,
            accept_pytorch_risk=args.accept_pytorch_risk,
            dry_run=args.dry_run,
            emit=emit,
            machine_output=machine_output,
            lock_timeout_seconds=args.lock_timeout,
            pip_timeout_seconds=args.pip_timeout,
            model_timeout_seconds=args.model_timeout,
        )
        plan_event = {"event": "plan", **result}
        if args.progress_json:
            pass
        elif args.json:
            _print_event({**plan_event, "terminal": True})
            return 0
        else:
            print(f"Runtime profile: {result['profile']}")
            for command in result["commands"]:
                print("Command: " + " ".join(command))
            if result["restart_required"]:
                print("Setup complete. Restart the backend to load the new runtime.")
            return 0

        _print_event(
            {
                "event": "complete",
                "profile": result["profile"],
                "restart_required": result["restart_required"],
                "percent": SETUP_COMPLETE_PERCENT,
                "terminal": True,
            }
        )
        return 0
    except SetupValidationError as exc:
        payload = {
            "error": "validation_error",
            "event": "error",
            "message": str(exc),
            "terminal": True,
        }
        if machine_output:
            _print_event(payload, stream=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        return 2
    except SetupExecutionError as exc:
        message = _sanitize_diagnostic(str(exc))
        payload = {
            "error": "setup_failed",
            "event": "error",
            "message": message,
            "terminal": True,
        }
        if machine_output:
            _print_event(payload, stream=sys.stderr)
        else:
            print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
