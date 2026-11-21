"""Probe selected runtime capabilities and report setup status."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Protocol, Sequence

from modules.app.services.runtime_capability_service import RuntimeCapabilityService
from modules.runtime_profiles import runtime_profiles
from modules.runtime_setup.diagnostics import _sanitize_diagnostic
from modules.runtime_setup.errors import SetupExecutionError, SetupValidationError
from modules.runtime_setup.selection import require_voice_model
from modules.runtime_setup.state import _read_setup_state, default_state_path


REPO_ROOT = Path(__file__).resolve().parents[2]


class _ProbeService(Protocol):
    def probe_mlx_tts_runtime(self) -> Mapping[str, Any]: ...

    def probe_package_tts_runtime(self) -> Mapping[str, Any]: ...

    def probe_cleaner_backend(
        self, backend: str, *, cleaner_cuda_quantization: str | None = None,
    ) -> Mapping[str, Any]: ...


class _SetupRuntimeCapabilityService(RuntimeCapabilityService):
    def _run_probe(self, script: str, timeout_seconds: float) -> dict[str, Any]:
        if not getattr(sys, "frozen", False):
            script = f"import sys\nsys.path.insert(0, {str(REPO_ROOT)!r})\n" + script
        return super()._run_probe(script, timeout_seconds)


def _combine_probe_checks(checks: Sequence[tuple[str, Mapping[str, Any]]]) -> dict[str, Any]:
    sanitized = [
        {
            "id": check_id,
            **({"device": result["device"]} if result.get("device") in {"cpu", "cuda"} else {}),
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


def _probe_profile(
    model_groups: Iterable[str], service: _ProbeService,
    *, cleaner_cuda_quantization: str | None = None,
) -> dict[str, Any]:
    checks: list[tuple[str, Mapping[str, Any]]] = []
    cleaner_results: dict[str, Mapping[str, Any]] = {}
    model_groups = tuple(model_groups)
    try:
        package_tts = None
        shares_cuda_probe = (
            isinstance(service, RuntimeCapabilityService)
            # Custom probe adapters must retain their independent semantics
            # unless they offer the shared check themselves.
            and getattr(service.probe_package_tts_runtime, "__func__", None)
            is RuntimeCapabilityService.probe_package_tts_runtime
            and getattr(service.probe_cleaner_backend, "__func__", None)
            is RuntimeCapabilityService.probe_cleaner_backend
        ) or (
            not isinstance(service, RuntimeCapabilityService)
            and callable(getattr(service, "probe_package_and_cuda_cleaner_runtime", None))
        )
        if (
            shares_cuda_probe
            and "tts-cpu-cuda" in model_groups
            and any(group.startswith("cleaner-cuda") for group in model_groups)
        ):
            package_tts, cleaner_results["cuda"] = service.probe_package_and_cuda_cleaner_runtime(
                cleaner_cuda_quantization=cleaner_cuda_quantization,
            )
        for group_id in model_groups:
            if group_id == "tts-mlx":
                checks.append((group_id, service.probe_mlx_tts_runtime()))
            elif group_id == "tts-cpu-cuda":
                checks.append((group_id, package_tts if package_tts is not None else service.probe_package_tts_runtime()))
            elif group_id.startswith(("cleaner-mlx", "cleaner-cuda")):
                backend = "mlx" if group_id.startswith("cleaner-mlx") else "cuda"
                if backend not in cleaner_results:
                    if backend == "cuda" and cleaner_cuda_quantization is not None:
                        cleaner_results[backend] = service.probe_cleaner_backend(
                            backend, cleaner_cuda_quantization=cleaner_cuda_quantization,
                        )
                    else:
                        cleaner_results[backend] = service.probe_cleaner_backend(backend)
                checks.append((group_id, cleaner_results[backend]))
    except Exception as exc:
        raise SetupExecutionError(f"Runtime probe failed: {exc}") from exc
    combined = _combine_probe_checks(checks)
    for check, (_, result) in zip(combined["checks"], checks):
        if result.get("timed_out"):
            check["state"] = "timeout"
    return combined


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
            "state_error": state_error,
        }

    service = probe_service or _SetupRuntimeCapabilityService(
        cache_ttl_seconds=0.0,
        timeout_seconds=15.0,
    )
    probe = _probe_profile(
        state["model_groups"], service,
        cleaner_cuda_quantization=state["cleaner_cuda_quantization"],
    )
    selection_error = None
    try:
        require_voice_model(state["profile"], state["model_groups"])
    except SetupValidationError as exc:
        selection_error = str(exc)
    return {
        "configured": True,
        "profile": state["profile"],
        "model_groups": state["model_groups"],
        "cleaner_cuda_quantization": state["cleaner_cuda_quantization"],
        "ready": bool(probe["available"]) and selection_error is None,
        "restart_required": state["restart_required"],
        "probe": probe,
        "profiles": profiles,
        "state_error": selection_error,
    }
