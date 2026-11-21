#!/usr/bin/env python3
"""Install one allowlisted Orpheus runtime profile and optional model groups."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.app.services.runtime_capability_service import (  # noqa: E402
    RuntimeCapabilityService as RuntimeCapabilityService,
)
from modules.runtime_profiles import (  # noqa: E402
    RuntimeProfile,
    cuda_hardware_error,
    get_runtime_profile as get_runtime_profile,
    runtime_profiles as runtime_profiles,
)
from modules.runtime_setup.errors import (  # noqa: E402
    SetupValidationError as SetupValidationError,
    SetupExecutionError as SetupExecutionError,
)
from modules.runtime_setup.selection import (  # noqa: E402
    _normalize_identifier as _normalize_identifier,
    _validated_profile as _validated_profile,
    _validated_model_groups as _validated_model_groups,
    require_voice_model as require_voice_model,
    validate_model_selection as validate_model_selection,
)
from modules.runtime_setup.state import (  # noqa: E402
    STATE_SCHEMA_VERSION as STATE_SCHEMA_VERSION,
    STATE_FILENAME as STATE_FILENAME,
    BACKEND_INSTANCE_FILENAME as BACKEND_INSTANCE_FILENAME,
    LOCK_STALE_SECONDS as LOCK_STALE_SECONDS,
    DEFAULT_LOCK_TIMEOUT_SECONDS as DEFAULT_LOCK_TIMEOUT_SECONDS,
    SetupLock as SetupLock,
    _pid_is_running as _pid_is_running,
    _windows_pid_is_running as _windows_pid_is_running,
    default_state_path as default_state_path,
    _atomic_write_json as _atomic_write_json,
    pending_model_deletions as pending_model_deletions,
    write_setup_state as write_setup_state,
    _read_setup_state as _read_setup_state,
    read_setup_state as read_setup_state,
    get_setup_state_cache_key as get_setup_state_cache_key,
    _backend_instance_path as _backend_instance_path,
    _read_backend_instance as _read_backend_instance,
    persist_backend_instance as persist_backend_instance,
    acknowledge_backend_startup as acknowledge_backend_startup,
    _compare_and_swap_restart_ack as _compare_and_swap_restart_ack,
)
from modules.runtime_setup.diagnostics import (  # noqa: E402
    DIAGNOSTIC_MAX_CHARS as DIAGNOSTIC_MAX_CHARS,
    _sanitize_diagnostic as _sanitize_diagnostic,
)
from modules.runtime_setup.status import (  # noqa: E402
    _ProbeService as _ProbeService,
    _SetupRuntimeCapabilityService as _SetupRuntimeCapabilityService,
    _combine_probe_checks as _combine_probe_checks,
    _probe_profile as _probe_profile,
    get_setup_status as get_setup_status,
)
from modules.subprocess_utils import background_process_kwargs  # noqa: E402


DEFAULT_PIP_TIMEOUT_SECONDS = 900.0
DEFAULT_MODEL_TIMEOUT_SECONDS = 3600.0
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


class _SetupArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise SetupValidationError(message)


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
EventEmitter = Callable[[Mapping[str, Any]], None]


@dataclass(frozen=True)
class InstallPlan:
    """Fully validated commands for one setup run."""

    profile: RuntimeProfile
    model_groups: tuple[str, ...]
    pip_command: tuple[str, ...]
    model_command: tuple[str, ...] | None

    @property
    def commands(self) -> tuple[tuple[str, ...], ...]:
        if self.model_command is None:
            return (self.pip_command,)
        return (self.pip_command, self.model_command)


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


def build_install_plan(profile_id: str, model_groups: Iterable[str] = ()) -> InstallPlan:
    """Validate identifiers and return fixed, shell-free argument arrays."""
    profile = _validated_profile(profile_id)
    groups = _validated_model_groups(profile, model_groups)

    executable = sys.executable
    pip_command = (
        executable,
        "-m",
        "pip",
        "install",
        "-r",
        str(_fixed_requirements_path(profile)),
        "--upgrade",
    )
    if profile.id == "cuda":
        # A plain torch==X pin also accepts X+cpu, which pip keeps installed.
        # Require the CUDA wheel explicitly, using the manifest's pinned version.
        manifest = _fixed_requirements_path(profile).read_text(encoding="utf-8")
        torch_pin = re.search(r"^torch==([0-9.]+)\s*$", manifest, re.MULTILINE)
        if torch_pin is None:
            raise SetupValidationError("CUDA manifest is missing its fixed PyTorch version.")
        pip_command += (
            "--extra-index-url", "https://download.pytorch.org/whl/cu126",
            f"torch=={torch_pin.group(1)}+cu126",
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

    return InstallPlan(
        profile=profile,
        model_groups=groups,
        pip_command=pip_command,
        model_command=model_command,
    )


def _validate_stage_timeout(stage: str, timeout_seconds: float) -> None:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise SetupValidationError(f"{stage} timeout must be a finite positive number.")


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
    _validate_stage_timeout(stage, timeout_seconds)
    emit(
        {
            "event": "stage_started",
            "stage": stage,
            "command": list(command),
            "percent": start_percent,
        }
    )
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
                **background_process_kwargs(),
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
        return background_process_kwargs()
    if os.name == "posix":
        return {"start_new_session": True}
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {}


def run_setup(
    *,
    profile_id: str,
    model_groups: Iterable[str] = (),
    cleaner_cuda_quantization: str = "4bit",
    dry_run: bool = False,
    state_path: Path | None = None,
    command_runner: CommandRunner | None = None,
    emit: EventEmitter | None = None,
    machine_output: bool = False,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    pip_timeout_seconds: float = DEFAULT_PIP_TIMEOUT_SECONDS,
    model_timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS,
    probe_service: _ProbeService | None = None,
) -> dict[str, Any]:
    """Validate, install dependencies/models, then atomically record state."""
    if cleaner_cuda_quantization not in ("4bit", "8bit", "fp16"):
        raise SetupValidationError("Unsupported cleaner quantization.")
    plan = build_install_plan(profile_id, model_groups)
    validate_model_selection(plan.model_groups)
    require_voice_model(plan.profile.id, plan.model_groups)
    _validate_stage_timeout("Dependency installation", pip_timeout_seconds)
    if plan.model_command is not None:
        _validate_stage_timeout("Model download", model_timeout_seconds)
    result: dict[str, Any] = {
        "profile": plan.profile.id,
        "model_groups": list(plan.model_groups),
        "commands": [list(command) for command in plan.commands],
        "dry_run": dry_run,
        "restart_required": not dry_run,
    }
    event_emitter = emit or (lambda event: None)
    event_emitter({"event": "plan", "percent": SETUP_PLAN_PERCENT, **result})
    if dry_run:
        return result

    if any(group.startswith("cleaner-cuda") for group in plan.model_groups):
        reason = cuda_hardware_error()
        if reason:
            raise SetupValidationError(reason)

    setup_state_path = state_path or default_state_path()
    with SetupLock(setup_state_path, timeout_seconds=lock_timeout_seconds):
        pending_model_deletions(setup_state_path)
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
                "message": "Checking the installed runtime before saving setup.",
                "percent": SETUP_STATE_START_PERCENT,
            }
        )
        probe = _probe_profile(
            plan.model_groups,
            probe_service or _SetupRuntimeCapabilityService(cache_ttl_seconds=0, timeout_seconds=30),
            cleaner_cuda_quantization=cleaner_cuda_quantization,
        )
        if not probe["available"]:
            details = "; ".join(
                f"{check['id']}: {check['error'] or 'runtime unavailable'}"
                for check in probe["checks"] if not check["available"]
            )
            raise SetupExecutionError(f"Runtime verification failed: {details}")
        write_setup_state(
            setup_state_path,
            profile_id=plan.profile.id,
            model_groups=plan.model_groups,
            cleaner_cuda_quantization=cleaner_cuda_quantization,
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
    parser.add_argument("--cleaner-cuda-quantization", choices=("4bit", "8bit", "fp16"), default="4bit")
    parser.add_argument(
        "--models",
        "--model-group",
        "--group",
        action="append",
        dest="model_groups",
        default=[],
        help="Allowlisted model group id. May be repeated.",
    )
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
            cleaner_cuda_quantization=args.cleaner_cuda_quantization,
            dry_run=args.dry_run,
            emit=emit,
            machine_output=machine_output,
            lock_timeout_seconds=args.lock_timeout,
            pip_timeout_seconds=args.pip_timeout,
            model_timeout_seconds=args.model_timeout,
        )
        if args.progress_json:
            _print_event(
                {
                    "event": "complete",
                    "profile": result["profile"],
                    "restart_required": result["restart_required"],
                    "percent": SETUP_COMPLETE_PERCENT,
                    "terminal": True,
                }
            )
        elif args.json:
            _print_event({"event": "plan", **result, "terminal": True})
        else:
            print(f"Runtime profile: {result['profile']}")
            for command in result["commands"]:
                print("Command: " + " ".join(command))
            if result["restart_required"]:
                print("Setup complete. Restart the backend to load the new runtime.")
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
