"""Runtime setup status and managed model storage for the desktop app."""

from __future__ import annotations

from contextlib import contextmanager
from functools import partial
from uuid import uuid4
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import re
import sys
import threading
from typing import Any, Iterator, Mapping

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from modules.runtime_profiles import (
    get_runtime_profile,
    cuda_hardware_error,
    CUDA_CLEANER_UNAVAILABLE_REASON,
    recommended_profile_id,
    runtime_profiles,
)
from modules.app.services.model_download_service import (
    ModelDownloadBusyError,
    ModelDownloadService,
)
from modules.app.services import model_storage_service
from modules.app.services.runtime_capability_service import RuntimeCapabilityService
from modules.contracts.durable_files import atomic_write_bytes, sync_directory
from modules.contracts.public_errors import PublicError
from modules.web.deps import public_http_error, get_controller_from_app
from modules.app.services.library_service import _default_folder_opener
from modules.storage.maintenance_paths import workspace_root_for_db
from modules.web.setup_manifest import _check_manifest_packages
from modules.web.setup_status_cache import RuntimeSetupStatusCache
from modules.runtime_setup import hub_cache
from modules.runtime_setup import selection as setup_selection
from modules.runtime_setup import state as setup_runtime
from modules.runtime_setup import status as setup_status
from modules.runtime_setup.catalog import MODEL_GROUPS, resolve_model_groups
from modules.runtime_setup.receipts import ModelReceiptVerifier


router = APIRouter()


@router.get("/api/system/storage")
def get_storage_folders(request: Request) -> dict[str, str | None]:
    config = get_controller_from_app(request.app).config
    # The desktop shell writes this log; a backend started any other way has
    # none, so report the path only when the file exists.
    data_dir = getattr(config, "data_dir", None) or Path(config.db_path).parent
    log_path = Path(data_dir).resolve() / "logs" / "backend.log"
    return {
        "output_path": str(Path(config.output_dir).resolve()),
        "app_data_path": str(workspace_root_for_db(Path(config.db_path))),
        "log_path": str(log_path) if log_path.is_file() else None,
    }


@router.post("/api/system/storage/{folder}/open")
def open_storage_folder(folder: str, request: Request) -> dict[str, str]:
    if folder not in {"output", "app_data"}:
        raise HTTPException(422, detail="Unknown storage folder.")
    path = Path(get_storage_folders(request)[f"{folder}_path"])
    try:
        path.mkdir(parents=True, exist_ok=True)
        _default_folder_opener(path)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(409, detail="The folder could not be opened. Try opening the displayed path manually.") from exc
    return {"folder_path": str(path)}

_RUNTIME_SETUP_STATE_FALLBACK_LOCK = threading.RLock()

__all__ = [
    "AdoptRuntimeRequest",
    "ModelDownloadRequest",
    "RuntimeSetupStatusCache",
    "build_setup_status_response",
    "delete_setup_runtime",
    "get_model_download_status",
    "get_setup_status",
    "post_model_download",
    "post_model_download_cancel",
    "post_setup_adopt",
    "router",
]


def _setup_state_fingerprint() -> tuple[Any, ...]:
    return setup_runtime.get_setup_state_cache_key()


def _check_selected_profile_packages(profile_id: str | None) -> dict[str, Any]:
    """Freshly validate every applicable package in the selected manifest."""
    if not profile_id:
        return {"ready": False, "missing": [], "incompatible": []}
    profile = get_runtime_profile(profile_id)
    if getattr(sys, "frozen", False):
        # Frozen bundles are immutable and validated at build time, and their
        # importlib metadata reflects the build environment rather than the
        # manifests; the runtime probe is the meaningful readiness gate there.
        return {"ready": True, "missing": [], "incompatible": []}
    try:
        return _check_manifest_packages(profile.requirements_file)
    except (OSError, ValueError):
        # A missing or unreadable manifest (e.g. a packaged build shipped
        # without the requirements files) must degrade to not-ready instead of
        # turning every /api/setup/status call into a 500.
        return {"ready": False, "missing": [], "incompatible": []}


def _check_model_group_assets(*args: Any, **kwargs: Any) -> dict[str, dict[str, Any]]:
    return hub_cache._check_model_group_assets(*args, **kwargs)


def _profile_payload(
    profile_id: str,
    *,
    installed_runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = get_runtime_profile(profile_id)
    reason = profile.incompatibility_reason()
    capabilities = {}
    if reason is None and profile.id == "cuda":
        detected = (
            installed_runtime
            if installed_runtime and installed_runtime["profile"] == "cuda" else {}
        )
        hardware_error = cuda_hardware_error()
        device = detected.get("device") or ("cpu" if hardware_error else "cuda")
        # The cleaner has its own driver requirement, so a GPU that runs
        # narration can still be unable to run a local cleaner.
        probed = detected.get("group_availability") or {}
        capabilities = {
            "device": device,
            "group_availability": {
                group: probed.get(group) or {
                    "available": not (group.startswith("cleaner-cuda") and hardware_error),
                    "unavailable_reason": CUDA_CLEANER_UNAVAILABLE_REASON
                    if group.startswith("cleaner-cuda") and hardware_error else None,
                }
                for group in profile.model_groups
            },
        }
    return {
        **capabilities,
        "id": profile.id,
        "label": profile.label,
        "description": profile.description,
        "compatible": profile.is_compatible() and reason is None,
        "reason": reason,
        "model_groups": list(profile.model_groups),
    }


def _profile_tts_groups(profile_id: str) -> tuple[str, ...]:
    """Return the TTS model groups declared by a profile."""
    profile = get_runtime_profile(profile_id)
    return tuple(group for group in profile.model_groups if group.startswith("tts"))


def _shares_cuda_probe(service: Any) -> bool:
    """Mirror ``_probe_profile``'s rule for using the combined CUDA probe."""
    if isinstance(service, RuntimeCapabilityService):
        return (
            getattr(service.probe_package_tts_runtime, "__func__", None)
            is RuntimeCapabilityService.probe_package_tts_runtime
            and getattr(service.probe_cleaner_backend, "__func__", None)
            is RuntimeCapabilityService.probe_cleaner_backend
        )
    return callable(getattr(service, "probe_package_and_cuda_cleaner_runtime", None))


class _StatusBuildProbes:
    """Answer every runtime probe at most once while one status response is built.

    The selected-runtime status and installed-runtime detection both probe the
    selected profile. The service cache normally serves the repeat, but it never
    keeps timed-out results, so without this a timed-out probe would run again.
    """

    def __init__(self, service: Any) -> None:
        self._service = service
        self._results: dict[tuple[Any, ...], Any] = {}
        # Without the shared check, _probe_profile keeps probing separately.
        self.probe_package_and_cuda_cleaner_runtime = (
            self._probe_package_and_cuda_cleaner if _shares_cuda_probe(service) else None
        )

    def _once(self, key: tuple[Any, ...], probe: Any) -> Any:
        if key not in self._results:
            self._results[key] = probe()
        return self._results[key]

    def probe_mlx_tts_runtime(self) -> Mapping[str, Any]:
        return self._once(("tts",), self._service.probe_mlx_tts_runtime)

    def probe_package_tts_runtime(self) -> Mapping[str, Any]:
        return self._once(("tts-package",), self._service.probe_package_tts_runtime)

    def probe_cleaner_backend(
        self, backend: str, *, cleaner_cuda_quantization: str | None = None,
    ) -> Mapping[str, Any]:
        kwargs = {"cleaner_cuda_quantization": cleaner_cuda_quantization} if cleaner_cuda_quantization is not None else {}
        return self._once(
            ("cleaner", backend, cleaner_cuda_quantization),
            partial(self._service.probe_cleaner_backend, backend, **kwargs),
        )

    def _probe_package_and_cuda_cleaner(
        self, *, cleaner_cuda_quantization: str | None = None,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        tts_key, cleaner_key = ("tts-package",), ("cleaner", "cuda", cleaner_cuda_quantization)
        if tts_key not in self._results and cleaner_key not in self._results:
            self._results[tts_key], self._results[cleaner_key] = (
                self._service.probe_package_and_cuda_cleaner_runtime(
                    cleaner_cuda_quantization=cleaner_cuda_quantization,
                )
            )
        # Like the service, reuse whichever half is known and probe the other.
        return (
            self.probe_package_tts_runtime(),
            self.probe_cleaner_backend("cuda", cleaner_cuda_quantization=cleaner_cuda_quantization),
        )


def _detect_installed_runtime(
    probe_service: RuntimeCapabilityService,
    receipt_verifier: ModelReceiptVerifier,
    preferred_profile: str | None = None,
    cleaner_cuda_quantization: str | None = None,
    *,
    known_packages: dict[str, Any] | None = None,
    known_assets: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Detect installed runtimes, including models outside the saved selection.

    Runs the cheap installed-packages check first and only probes the real
    capability path when packages are ready. Returns ``None`` on any probe
    error rather than guessing readiness. ``known_packages`` and
    ``known_assets`` are results the caller just computed for
    ``preferred_profile`` and are reused instead of checked again.
    """
    for profile in runtime_profiles():
        if preferred_profile is not None and profile.id != preferred_profile:
            continue
        if profile.id == "base" or not profile.is_compatible():
            continue
        reuse = preferred_profile is not None
        packages = (
            known_packages if reuse and known_packages is not None
            else _check_selected_profile_packages(profile.id)
        )
        if not packages["ready"]:
            continue
        reused_assets = {
            group_id: known_assets[group_id]
            for group_id in profile.model_groups
            if reuse and known_assets is not None and group_id in known_assets
        }
        unchecked_groups = [group_id for group_id in profile.model_groups if group_id not in reused_assets]
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                assets = executor.submit(
                    hub_cache._check_model_group_assets,
                    unchecked_groups,
                    receipt_verifier=receipt_verifier,
                ) if unchecked_groups else None
                probe = setup_status._probe_profile(
                    profile.model_groups, probe_service,
                    **({"cleaner_cuda_quantization": cleaner_cuda_quantization}
                       if profile.id == "cuda" and cleaner_cuda_quantization is not None else {}),
                )
                asset_status = {**reused_assets, **(assets.result() if assets is not None else {})}
        except Exception:
            return None
        checks_by_group = {
            str(check.get("id")): check for check in probe.get("checks", [])
            if isinstance(check, Mapping)
        }
        tts_groups = _profile_tts_groups(profile.id)
        checking = any(
            checks_by_group.get(group_id, {}).get("state") in {"pending", "timeout"}
            for group_id in tts_groups
        )
        readiness_by_group = {
            str(check.get("id")): bool(check.get("available"))
            for check in probe.get("checks", [])
            if isinstance(check, Mapping)
        }
        model_groups = [
            {
                "id": group_id,
                "runtime_ready": readiness_by_group.get(group_id, False),
                **({
                    "runtime_state": checks_by_group.get(group_id, {}).get("state") or "unavailable",
                    "runtime_reason": None if checks_by_group.get(group_id, {}).get("state") in {"pending", "timeout"}
                    else checks_by_group.get(group_id, {}).get("error"),
                } if not readiness_by_group.get(group_id, False) else {}),
                "assets_ready": bool(asset_status.get(group_id, {}).get("ready")),
                "ready": bool(asset_status.get(group_id, {}).get("ready"))
                and readiness_by_group.get(group_id, False),
            }
            for group_id in profile.model_groups
        ]
        # The runtime can be present (baked into a packaged build) while its
        # model assets are not downloaded yet; the frontend offers an in-app
        # download only in that split state.
        runtime_available = bool(tts_groups) and all(
            readiness_by_group.get(group_id, False) for group_id in tts_groups
        )
        ready = runtime_available and all(
            bool(asset_status.get(group_id, {}).get("ready"))
            for group_id in tts_groups
        )
        result = {
            "profile": profile.id,
            "ready": ready,
            "runtime_available": runtime_available,
            **({"checking": True} if checking else {}),
            "model_groups": model_groups,
        }
        if profile.id == "cuda":
            result["device"] = next(
                (check["device"] for check in probe.get("checks", []) if check.get("device")),
                "cpu" if cuda_hardware_error() else "cuda",
            )
            result["group_availability"] = {
                group_id: {
                    "available": bool(check.get("available")),
                    "unavailable_reason": None if check.get("available") or check.get("state") in {"pending", "timeout"}
                    else check.get("error") or "Cleaner runtime unavailable",
                    **({"state": check["state"]} if check.get("state") else {}),
                }
                for group_id, check in checks_by_group.items() if group_id.startswith("cleaner-")
            }
        return result
    return None


def _empty_status(
    *,
    request: Request,
    state_error: str | None,
    installed_runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "configured": False,
        "state": "error" if state_error else "unconfigured",
        "state_error": state_error,
        "selected_profile": None,
        "selected_model_groups": [],
        "packages": {"ready": False, "missing": [], "incompatible": []},
        "probe": {"ready": False, "checks": [
            {"id": group["id"], "ready": group["runtime_ready"], "state": group["runtime_state"]}
            for group in (installed_runtime or {}).get("model_groups", [])
            if group.get("runtime_state") in {"pending", "timeout"}
        ]},
        **({"checking": True} if (installed_runtime or {}).get("checking") else {}),
        "ready": False,
        "restart_required": False,
        "recommended_profile": recommended_profile_id(),
        "installed_runtime": installed_runtime,
        "profiles": [
            _profile_payload(profile.id, installed_runtime=installed_runtime)
            for profile in runtime_profiles()
        ],
        "backend": {
            "instance_id": str(request.app.state.backend_instance_id),
            "status": "running",
        },
    }


def _build_uncached_setup_status_response(
    request: Request,
    probe_service: RuntimeCapabilityService,
    receipt_verifier: ModelReceiptVerifier,
) -> dict[str, Any]:
    """Project internal setup state onto a stable, non-secret API contract."""
    probe_service = _StatusBuildProbes(probe_service)
    try:
        status = setup_status.get_setup_status(probe_service=probe_service)
    except Exception:
        return _empty_status(request=request, state_error="unavailable")

    if not bool(status.get("configured")):
        error = "invalid" if status.get("state_error") else None
        installed_runtime = (
            _detect_installed_runtime(probe_service, receipt_verifier)
            if error is None
            else None
        )
        return _empty_status(
            request=request,
            state_error=error,
            installed_runtime=installed_runtime,
        )

    selected_profile = str(status.get("profile") or "")
    try:
        profile = get_runtime_profile(selected_profile)
    except ValueError:
        return _empty_status(request=request, state_error="invalid")

    allowed_groups = frozenset(profile.model_groups)
    selected_group_ids = [
        group_id
        for value in status.get("model_groups", [])
        if (group_id := str(value)) in allowed_groups
    ]
    raw_probe = status.get("probe")
    raw_checks = raw_probe.get("checks", []) if isinstance(raw_probe, Mapping) else []
    readiness_by_group: dict[str, bool] = {}
    states_by_group: dict[str, str] = {}
    for check in raw_checks:
        if not isinstance(check, Mapping):
            continue
        group_id = str(check.get("id") or "")
        if group_id in selected_group_ids:
            readiness_by_group[group_id] = bool(check.get("available"))
            if check.get("state") in {"pending", "timeout"}:
                states_by_group[group_id] = check["state"]

    probe_checks = [
        {
            "id": group_id, "ready": readiness_by_group.get(group_id, False),
            **({"state": states_by_group[group_id]} if group_id in states_by_group else {}),
        }
        for group_id in selected_group_ids
    ]
    probe_ready = bool(raw_probe.get("available")) if isinstance(raw_probe, Mapping) else False
    packages = _check_selected_profile_packages(selected_profile)
    asset_status = hub_cache._check_model_group_assets(
        selected_group_ids,
        receipt_verifier=receipt_verifier,
    )
    selected_groups = [
        {
            "id": group_id,
            "assets_ready": bool(asset_status[group_id]["ready"]),
            "assets_reason": asset_status[group_id]["reason"],
            "runtime_ready": readiness_by_group.get(group_id, False),
            "ready": bool(asset_status[group_id]["ready"])
            and readiness_by_group.get(group_id, False),
        }
        for group_id in selected_group_ids
    ]
    restart_required = bool(status.get("restart_required"))
    assets_ready = all(group["assets_ready"] for group in selected_groups)
    voice_ready = selected_profile == "base" or any(
        group["id"].startswith("tts-") and group["assets_ready"] for group in selected_groups
    )
    ready = (
        bool(status.get("ready"))
        and packages["ready"]
        and assets_ready
        and voice_ready
        and not restart_required
    )
    if restart_required:
        state = "restart_required"
    elif ready:
        state = "ready"
    elif any(check.get("state") in {"pending", "timeout"} for check in probe_checks):
        state = "checking"
    else:
        state = "unavailable"

    installed_runtime = _detect_installed_runtime(
        probe_service, receipt_verifier,
        preferred_profile=selected_profile if selected_profile != "base" else None,
        cleaner_cuda_quantization=status.get("cleaner_cuda_quantization"),
        known_packages=packages,
        known_assets=asset_status,
    )

    return {
        "schema_version": 1,
        "configured": True,
        "state": state,
        "state_error": None if voice_ready else "The Kokoro voice model is missing. Install it to create audio.",
        "selected_profile": selected_profile,
        "selected_model_groups": selected_groups,
        "cleaner_cuda_quantization": status.get("cleaner_cuda_quantization", "4bit"),
        "packages": packages,
        "probe": {"ready": probe_ready, "checks": probe_checks},
        **({"checking": True} if state == "checking" else {}),
        "ready": ready,
        "restart_required": restart_required,
        "recommended_profile": recommended_profile_id(),
        # Settings must also expose optional models outside the saved selection.
        "installed_runtime": installed_runtime,
        "profiles": [
            _profile_payload(item.id, installed_runtime=installed_runtime)
            for item in runtime_profiles()
        ],
        "backend": {
            "instance_id": str(request.app.state.backend_instance_id),
            "status": "running",
        },
    }


def _get_status_cache(request: Request) -> RuntimeSetupStatusCache:
    cache = getattr(request.app.state, "runtime_setup_status_cache", None)
    if isinstance(cache, RuntimeSetupStatusCache):
        return cache

    # Setup routes are synchronous dependencies executed in worker threads.
    # Guard lazy initialization so concurrent status/adopt/download requests
    # share one cache and one probe service instead of racing two instances.
    lock = getattr(request.app.state, "runtime_setup_state_lock", None)
    if lock is None:
        lock = _RUNTIME_SETUP_STATE_FALLBACK_LOCK
    with lock:
        cache = getattr(request.app.state, "runtime_setup_status_cache", None)
        if isinstance(cache, RuntimeSetupStatusCache):
            return cache
        # Setup readiness and /api/system/capabilities must share one probe cache,
        # so adopt the controller's service whenever it is available.
        shared = getattr(
            get_controller_from_app(request.app), "runtime_capability_service", None
        )
        cache = RuntimeSetupStatusCache(
            probe_service=shared if isinstance(shared, RuntimeCapabilityService) else None,
            receipt_verifier=ModelReceiptVerifier(
                cache_path=setup_runtime.default_state_path().with_name("model-integrity-cache.json")
                if getattr(request.app.state, "desktop_mode", False) else None,
            ),
        )
        request.app.state.runtime_setup_status_cache = cache
        return cache


def _pending_setup_status_response(request: Request) -> dict[str, Any]:
    try:
        state, state_error = setup_runtime._read_setup_state(setup_runtime.default_state_path())
    except setup_runtime.SetupExecutionError:
        return _empty_status(request=request, state_error="unavailable")
    response = _empty_status(request=request, state_error="invalid" if state_error else None)
    if state_error:
        return response
    if state is not None:
        response.update({
            "configured": True,
            "selected_profile": state["profile"],
            "selected_model_groups": [
                {"id": group_id, "assets_ready": False, "assets_reason": None,
                 "runtime_ready": False, "ready": False}
                for group_id in state["model_groups"]
            ],
            "cleaner_cuda_quantization": state["cleaner_cuda_quantization"],
            "restart_required": state["restart_required"],
            "probe": {"ready": False, "checks": [
                {"id": group_id, "ready": False, "state": "pending"}
                for group_id in state["model_groups"]
            ]},
        })
    response["state"] = "checking"
    response["checking"] = True
    return response


def build_setup_status_response(request: Request) -> dict[str, Any]:
    cache = _get_status_cache(request)
    key = _setup_state_fingerprint()
    status = cache.get(
        key,
        lambda service: _build_uncached_setup_status_response(
            request,
            service,
            cache.receipt_verifier,
        ),
        **({
            "pending_builder": lambda: _pending_setup_status_response(request),
            "wait_seconds": 2.0,
        } if getattr(sys, "frozen", False) else {}),
    )
    reset_id = getattr(request.app.state, "setup_reset_id", None) or model_storage_service.read_setup_reset_id()
    return {**status, **({"setup_reset_id": reset_id} if reset_id else {})}


@router.get("/api/setup/status")
def get_setup_status(request: Request) -> dict[str, Any]:
    return build_setup_status_response(request)


class AdoptRuntimeRequest(BaseModel):
    """Adopt an already-operational runtime (or library-only) without installing."""

    model_config = ConfigDict(extra="forbid")

    profile: str
    model_groups: list[str] = Field(default_factory=list)
    cleaner_cuda_quantization: str = Field(default="4bit", pattern="^(4bit|8bit|fp16)$")


def _adopt_backend_instance_id(request: Request) -> str | None:
    """Record the running backend's identity, mirroring the installer flow."""
    instance_id = str(getattr(request.app.state, "backend_instance_id", ""))
    return instance_id if re.fullmatch(r"[0-9a-f]{32}", instance_id) else None


@router.post("/api/setup/adopt")
def post_setup_adopt(payload: AdoptRuntimeRequest, request: Request) -> dict[str, Any]:
    """Persist a chosen runtime after server-side re-validation, no installer run."""
    cache = _get_status_cache(request)
    try:
        profile = setup_selection._validated_profile(payload.profile)
        groups = setup_selection._validated_model_groups(profile, payload.model_groups)
        setup_selection.validate_model_selection(groups)
        setup_selection.require_voice_model(profile.id, groups)
    except setup_runtime.SetupValidationError as exc:
        raise public_http_error(PublicError("invalid_request", str(exc), 422), legacy_error=True) from exc

    if profile.id != "base":
        # Every readiness check below folds over `groups`, so an empty selection
        # would pass all of them vacuously and persist a "ready" state for a
        # profile with no TTS model installed. post_model_download guards this
        # the same way.
        if not groups:
            raise public_http_error(
                PublicError("invalid_request", "At least one model group is required.", 422),
                legacy_error=True,
            )
        packages = _check_selected_profile_packages(profile.id)
        if not packages["ready"]:
            raise public_http_error(
                PublicError(
                    "packages_not_ready",
                    "Runtime dependencies are missing. Run runtime setup to repair them.",
                    409,
                    details={"packages": packages},
                ),
                legacy_error=True,
            )
        try:
            # Results from a just-finished "Check runtime" are reused from the
            # shared probe service's cache.
            probe = setup_status._probe_profile(
                groups, cache.probe_service, cleaner_cuda_quantization=payload.cleaner_cuda_quantization,
            )
        except Exception as exc:
            raise public_http_error(
                PublicError("probe_unavailable", "The runtime check could not run. Check runtime and try again.", 409),
                legacy_error=True,
            ) from exc
        if not probe.get("available"):
            probe_checks = [
                {
                    "id": str(check.get("id")),
                    "ready": bool(check.get("available")),
                }
                for check in probe.get("checks", [])
                if isinstance(check, Mapping)
            ]
            raise public_http_error(
                PublicError(
                    "probe_failed",
                    "The selected runtime is unavailable. Check its dependencies and device support.",
                    409,
                    details={"probe": {"checks": probe_checks}},
                ),
                legacy_error=True,
            )
        asset_status = hub_cache._check_model_group_assets(
            groups,
            receipt_verifier=cache.receipt_verifier,
        )
        missing_assets = [
            {"id": group_id, "reason": asset_status[group_id]["reason"]}
            for group_id in groups
            if not asset_status[group_id]["ready"]
        ]
        if missing_assets:
            raise public_http_error(
                PublicError(
                    "assets_missing",
                    "Required model files are missing. Install or repair the selected models.",
                    409,
                    details={"model_groups": missing_assets},
                ),
                legacy_error=True,
            )

    state_path = setup_runtime.default_state_path()
    cache_root = Path(model_storage_service.hf_constants.HF_HUB_CACHE)
    try:
        # Only staged model journals need the conversion fence; adopting a
        # runtime with nothing to recover must not block conversion starts.
        with _conversion_storage_fence(
            request, attempt=bool(model_storage_service._staged_model_manifests(cache_root)),
        ) as fenced, setup_runtime.SetupLock(state_path):
            if not fenced and model_storage_service._staged_model_manifests(cache_root):
                code = _storage_cleanup_refusal(_conversion_service(request))
                raise public_http_error(
                    PublicError(code, _STORAGE_RECOVERY_BLOCKERS[code], 409), legacy_error=True,
                )
            model_storage_service._recover_staged_models(
                on_uninstall_complete=partial(_complete_runtime_uninstall, request),
            )
            if groups:
                # The shared verifier re-checks every file's identity and only
                # rehashes files that changed since the check above.
                asset_status = hub_cache._check_model_group_assets(groups, receipt_verifier=cache.receipt_verifier)
                missing_assets = [
                    {"id": group_id, "reason": asset_status[group_id]["reason"]}
                    for group_id in groups if not asset_status[group_id]["ready"]
                ]
                if missing_assets:
                    raise public_http_error(
                        PublicError(
                            "assets_missing",
                            "Required model files are missing. Install or repair the selected models.",
                            409,
                            details={"model_groups": missing_assets},
                        ),
                        legacy_error=True,
                    )
            saved_state = setup_runtime.write_setup_state(
                state_path,
                profile_id=profile.id,
                model_groups=groups,
                cleaner_cuda_quantization=payload.cleaner_cuda_quantization,
                backend_instance_id=_adopt_backend_instance_id(request),
                restart_required=False,
            )
            config = getattr(get_controller_from_app(request.app), "config", None)
            apply_preferences = getattr(config, "apply_setup_preferences", None)
            if callable(apply_preferences):
                apply_preferences(state=saved_state)
    except setup_runtime.SetupExecutionError as exc:
        raise public_http_error(PublicError("setup_in_progress", str(exc), 409), legacy_error=True) from exc
    except setup_runtime.SetupValidationError as exc:
        raise public_http_error(PublicError("invalid_request", str(exc), 422), legacy_error=True) from exc

    cache.invalidate()
    return build_setup_status_response(request)


class ModelDownloadRequest(BaseModel):
    """Download the model assets for an already-operational (baked) runtime."""

    model_config = ConfigDict(extra="forbid")
    configure_runtime: bool = True

    profile: str
    model_groups: list[str]
    cleaner_cuda_quantization: str = Field(default="4bit", pattern="^(4bit|8bit|fp16)$")


def _get_download_service(request: Request) -> ModelDownloadService:
    service = getattr(request.app.state, "model_download_service", None)
    if not isinstance(service, ModelDownloadService):
        lock = getattr(request.app.state, "runtime_setup_state_lock", None)
        if lock is None:
            lock = _RUNTIME_SETUP_STATE_FALLBACK_LOCK
        with lock:
            service = getattr(request.app.state, "model_download_service", None)
            if not isinstance(service, ModelDownloadService):
                cache = _get_status_cache(request)
                service = ModelDownloadService(on_terminal=cache.invalidate)
                request.app.state.model_download_service = service
    return service


@router.get("/api/setup/models/download")
def get_model_download_status(request: Request) -> dict[str, Any]:
    return _get_download_service(request).status()


@router.post("/api/setup/models/download")
def post_model_download(payload: ModelDownloadRequest, request: Request) -> dict[str, Any]:
    """Start a background model download for packaged builds (no installer).

    Only fetches files; adopting the runtime afterwards still runs the full
    probe and asset validation in ``POST /api/setup/adopt``.
    """
    try:
        profile = setup_selection._validated_profile(payload.profile)
        groups = setup_selection._validated_model_groups(profile, payload.model_groups)
        if payload.configure_runtime:
            setup_selection.validate_model_selection(groups)
            setup_selection.require_voice_model(profile.id, groups)
    except setup_runtime.SetupValidationError as exc:
        raise public_http_error(PublicError("invalid_request", str(exc), 422), legacy_error=True) from exc
    if not groups:
        raise public_http_error(
            PublicError("invalid_request", "At least one model group is required.", 422),
            legacy_error=True,
        )

    service = _get_download_service(request)
    if any(group.startswith("cleaner-cuda") for group in groups):
        reason = cuda_hardware_error()
        if reason:
            raise public_http_error(PublicError("invalid_request", reason, 422), legacy_error=True)
    try:
        return service.start(profile_id=profile.id, model_groups=groups, configure_runtime=payload.configure_runtime, cleaner_cuda_quantization=payload.cleaner_cuda_quantization)
    except ModelDownloadBusyError as exc:
        raise public_http_error(
            PublicError(
                "download_in_progress",
                "A model download is already running. Wait for it to finish or cancel it.",
                409,
            ),
            legacy_error=True,
        ) from exc
    except setup_runtime.SetupExecutionError as exc:
        raise public_http_error(PublicError("setup_in_progress", str(exc), 409), legacy_error=True) from exc


@router.post("/api/setup/models/download/cancel")
def post_model_download_cancel(request: Request) -> dict[str, Any]:
    """Cooperatively cancel a packaged-runtime model download."""
    return _get_download_service(request).request_cancel()


def _complete_runtime_uninstall(request: Request, reset_id: str | None = None) -> None:
    config = getattr(get_controller_from_app(request.app), "config", None)
    model_storage_service.reset_setup_preferences(config, request.app.state, reset_id)
    cache = _get_status_cache(request)
    cache.receipt_verifier.invalidate()
    cache.invalidate()


@router.get("/api/setup/models")
def get_managed_models(request: Request = None) -> dict[str, Any]:
    """List managed cache usage without disclosing filesystem paths."""
    from huggingface_hub.errors import CacheNotFound

    root = Path(hub_cache.hf_constants.HF_HUB_CACHE)
    try:
        if model_storage_service._staged_model_manifests(root):
            # Recovery deletes model files and evicts loaded models, so it runs
            # only while no conversion can use them; otherwise a later poll
            # retries and the stages are listed below meanwhile.
            with _conversion_storage_fence(request) as fenced:
                if fenced:
                    try:
                        with setup_runtime.SetupLock(setup_runtime.default_state_path(), timeout_seconds=0.05):
                            model_storage_service._recover_staged_models(
                                cache_root=root,
                                on_uninstall_complete=partial(_complete_runtime_uninstall, request) if request else None,
                            )
                    except setup_runtime.SetupExecutionError:
                        pass  # A live setup operation owns its stages; show them below.
        cache = hub_cache.scan_cache_dir()
        repos = cache.repos
        manifests = model_storage_service._staged_model_manifests(root)
    except CacheNotFound:
        cache = None
        repos = ()
        manifests = []
    except OSError as exc:
        raise public_http_error(
            PublicError("cache_unavailable", "Model storage could not be read. Try again.", 503),
            legacy_error=True,
        ) from exc
    by_id = {repo.repo_id: repo for repo in repos if repo.repo_type == "model"}
    paths = hub_cache._managed_cache_paths(cache)
    staged: dict[str, list[Path]] = {}
    for model_id, stage, _, _ in manifests:
        staged.setdefault(model_id, []).append(stage)
    group_paths = {
        group: [(model_id, path) for model_id in resolve_model_groups((group,))
                for path in (paths[model_id], *staged.get(model_id, ()))]
        for group in MODEL_GROUPS
    }
    blocker = _model_delete_blocker(request) if request is not None else None
    models = []
    for group in MODEL_GROUPS:
        downloaded = any(path.exists() for _, path in group_paths[group])
        model = {
            "id": group,
            "downloaded": downloaded,
            "size_bytes": hub_cache._cache_bytes(*(path for _, path in group_paths[group])),
            **({"damaged": True} if any(path.exists() and (model_id not in by_id or path != paths[model_id]) for model_id, path in group_paths[group]) else {}),
        }
        if request is not None:
            # Lets Settings disable Delete with the reason DELETE would give.
            model["deletable"] = downloaded and blocker is None
            model["delete_blocked"] = blocker
        models.append(model)
    return {"models": models}


_MODEL_DELETE_BLOCKERS = {
    "conversion_in_progress": "Wait for the current conversion to finish before deleting a model.",
    "conversion_cleanup_pending": "Wait for the stopped conversion to finish cleaning up before deleting a model.",
    "maintenance_in_progress": "Wait for library maintenance to finish before deleting a model.",
    "download_in_progress": "Wait for the model download to finish before deleting a model.",
    "preview_in_progress": "Wait for the voice preview to finish before deleting a model.",
}


def _model_delete_blocker(request: Request) -> dict[str, str] | None:
    """Why deleting a model would be refused right now, without reserving anything."""
    controller = getattr(request.app.state, "controller", None)
    blocker = getattr(getattr(controller, "_conversion_service", None), "storage_cleanup_blocker", lambda: None)()
    if blocker is None:
        downloads = getattr(request.app.state, "model_download_service", None)
        status = getattr(downloads, "status", None)
        if callable(status) and status().get("lifecycle") in {"running", "stopping"}:
            blocker = "download_in_progress"
    if blocker is None and getattr(model_storage_service, "_preview_users", 0):
        blocker = "preview_in_progress"
    return {"code": blocker, "message": _MODEL_DELETE_BLOCKERS[blocker]} if blocker else None


_STORAGE_RECOVERY_BLOCKERS = {
    "conversion_in_progress": "Wait for the current conversion to finish, then try again.",
    "conversion_cleanup_pending": "Wait for the stopped conversion to finish cleaning up, then try again.",
    "maintenance_in_progress": "Wait for library maintenance to finish, then try again.",
}


def _conversion_service(request: Request | None) -> Any:
    # Read without creating a controller: none means no conversion can run.
    controller = getattr(request.app.state, "controller", None) if request is not None else None
    return getattr(controller, "_conversion_service", None)


@contextmanager
def _conversion_storage_fence(request: Request | None, *, attempt: bool = True) -> Iterator[bool]:
    """Hold the conversion storage-cleanup reservation without waiting.

    Yields False when a conversion, its cleanup or library maintenance owns
    storage; True when the reservation is held or no conversion service exists.
    """
    service = _conversion_service(request)
    begin = getattr(service, "begin_storage_cleanup", None)
    end = getattr(service, "end_storage_cleanup", None)
    if not callable(begin):
        yield True
        return
    if not attempt:
        yield False
        return
    try:
        begin()
    except RuntimeError:
        yield False
        return
    try:
        yield True
    finally:
        if callable(end):
            end()


def _release_resident_models(controller: Any) -> None:
    """Unload models a finished conversion keeps warm; requires the reservation."""
    release = getattr(controller, "release_resident_models", None)
    if callable(release) and not release():
        code = "conversion_cleanup_pending"
        raise public_http_error(PublicError(code, _MODEL_DELETE_BLOCKERS[code], 409), legacy_error=True)


def _storage_cleanup_refusal(conversion_service: Any) -> str:
    """Classify why ``begin_storage_cleanup`` just refused the reservation."""
    blocker = getattr(conversion_service, "storage_cleanup_blocker", lambda: None)()
    # Another cleanup (backup, restore, storage cleanup) holds the slot.
    return blocker or "maintenance_in_progress"


@router.delete("/api/setup/models/{group_id}")
def delete_managed_model(group_id: str, request: Request) -> dict[str, Any]:
    """Remove exactly one managed model group, preserving other models and settings."""
    if group_id not in MODEL_GROUPS:
        raise public_http_error(PublicError("invalid_model", "Unknown managed model.", 422), legacy_error=True)
    cache = _get_status_cache(request)
    state_path = setup_runtime.default_state_path()
    controller = get_controller_from_app(request.app)
    service = getattr(controller, "_conversion_service", None)
    begin = getattr(service, "begin_storage_cleanup", None)
    end = getattr(service, "end_storage_cleanup", None)
    acquired = False
    try:
        if callable(begin):
            try:
                begin()
                acquired = True
            except RuntimeError as exc:
                code = _storage_cleanup_refusal(service)
                raise public_http_error(PublicError(code, _MODEL_DELETE_BLOCKERS[code], 409), legacy_error=True) from exc
            _release_resident_models(controller)
        with setup_runtime.SetupLock(state_path, timeout_seconds=0.5):
            recovered, recovered_bytes = model_storage_service._recover_staged_models(
                model_groups=(group_id,), on_uninstall_complete=partial(_complete_runtime_uninstall, request),
            )
            # Read the state first so an unreadable configuration cannot be
            # discovered only after its model files have already been removed.
            state, _ = setup_runtime._read_setup_state(state_path)
            if state and group_id in state["model_groups"] and not group_id.startswith("tts-"):
                remaining = tuple(group for group in state["model_groups"] if group != group_id)
                installation_id = uuid4().hex
                with model_storage_service._stage_runtime_models(group_id, installation_id=installation_id) as staged:
                    try:
                        saved_state = setup_runtime.write_setup_state(
                            state_path,
                            profile_id=state["profile"] if remaining else "base",
                            model_groups=remaining,
                            cleaner_cuda_quantization=state["cleaner_cuda_quantization"],
                            backend_instance_id=_adopt_backend_instance_id(request),
                            restart_required=False,
                            installation_id=installation_id,
                        )
                    except (setup_runtime.SetupExecutionError, OSError) as exc:
                        raise public_http_error(
                            PublicError(
                                "setup_state_write_failed",
                                "Model settings could not be saved. Try again.",
                                503,
                            ),
                            legacy_error=True,
                        ) from exc
                    model_storage_service._commit_staged_models(staged)
                    config = getattr(controller, "config", None)
                    apply_preferences = getattr(config, "apply_setup_preferences", None)
                    if callable(apply_preferences):
                        apply_preferences(state=saved_state)
                    deleted, freed = model_storage_service._delete_runtime_models(model_groups=(group_id,), model_paths=staged)
            else:
                with model_storage_service._stage_runtime_models(group_id, installation_id=uuid4().hex) as staged:
                    model_storage_service._commit_staged_models(staged)
                    deleted, freed = model_storage_service._delete_runtime_models(model_groups=(group_id,), model_paths=staged)
            deleted = [group for group in MODEL_GROUPS if group in set(deleted) | set(recovered)]
            freed += recovered_bytes
            residual = [model["id"] for model in get_managed_models(request)["models"] if model["id"] == group_id and model["downloaded"]]
    except model_storage_service.PreviewInProgress as exc:
        raise public_http_error(PublicError("preview_in_progress", str(exc), 409), legacy_error=True) from exc
    except setup_runtime.SetupExecutionError as exc:
        raise public_http_error(PublicError("setup_in_progress", str(exc), 409), legacy_error=True) from exc
    except OSError as exc:
        raise public_http_error(
            PublicError(
                "model_delete_failed",
                "The model could not be deleted. Close any app using it and try again.",
                409,
            ),
            legacy_error=True,
        ) from exc
    finally:
        cache.receipt_verifier.invalidate()
        cache.invalidate()
        if acquired and callable(end):
            end()
    return {"deleted_model_groups": deleted, "freed_bytes": freed, "state_deleted": False, **model_storage_service._residual_notice(residual)}


@router.delete("/api/setup/runtime")
def delete_setup_runtime(request: Request) -> dict[str, Any]:
    """Uninstall local runtime data: managed model assets and the setup state."""
    cache = _get_status_cache(request)
    state_path = setup_runtime.default_state_path()
    controller = get_controller_from_app(request.app)
    conversion_service = getattr(controller, "_conversion_service", None)
    begin_cleanup = getattr(conversion_service, "begin_storage_cleanup", None)
    end_cleanup = getattr(conversion_service, "end_storage_cleanup", None)
    guard_acquired = False
    if callable(begin_cleanup):
        try:
            begin_cleanup()
            guard_acquired = True
        except RuntimeError as exc:
            code = _storage_cleanup_refusal(conversion_service)
            raise public_http_error(PublicError(code, str(exc), 409), legacy_error=True) from exc
    try:
        if guard_acquired:
            _release_resident_models(controller)
        with setup_runtime.SetupLock(state_path):
            recovered, recovered_bytes = model_storage_service._recover_staged_models(
                on_uninstall_complete=partial(_complete_runtime_uninstall, request),
            )
            setup_runtime.pending_model_deletions(state_path)
            previous_state, error = setup_runtime._read_setup_state(state_path)
            stored_path = setup_runtime._readable_state_path(state_path)
            state_deleted = stored_path.exists()
            if previous_state is not None and stored_path != state_path:
                # Adopt an earlier build's state: once this reset removes the
                # current file, the legacy fallback would be read again.
                atomic_write_bytes(state_path, stored_path.read_bytes(), permissions=0o600)
            if error or stored_path != state_path:
                # An unreadable state (for example from an older build) is what
                # this reset exists to clear. No journal depends on it, so drop
                # it first and uninstall what is then an unconfigured runtime.
                stored_path.unlink()
                sync_directory(stored_path.parent)
            previous_bytes = state_path.read_bytes() if previous_state is not None else None
            uninstall_id = uuid4().hex
            with model_storage_service._stage_runtime_models(
                None, installation_id=uninstall_id, uninstall_runtime=True,
            ) as staged:
                # State absence commits the whole uninstall, including a kill
                # before flags are written, when a prior state existed. An
                # unconfigured runtime commits at its first durable flag.
                state_path.unlink(missing_ok=True)
                if state_path.parent.exists():
                    sync_directory(state_path.parent)
                model_storage_service._commit_staged_models(staged)
                deleted_groups, freed_bytes = model_storage_service._delete_runtime_models(
                    model_paths=staged, on_uninstall_complete=partial(_complete_runtime_uninstall, request),
                )
                deleted_groups = [group for group in MODEL_GROUPS if group in set(deleted_groups) | set(recovered)]
                freed_bytes += recovered_bytes
                residual = [model["id"] for model in get_managed_models(request)["models"] if model["downloaded"]]
                if not residual:
                    _complete_runtime_uninstall(request, uninstall_id)
                else:
                    # Preserve the existing partial-uninstall response and live
                    # preferences; committed journals still own the retry.
                    if previous_bytes is not None:
                        atomic_write_bytes(state_path, previous_bytes, permissions=0o600)
                        state_deleted = False

    except model_storage_service.PreviewInProgress as exc:
        raise public_http_error(PublicError("preview_in_progress", str(exc), 409), legacy_error=True) from exc
    except setup_runtime.SetupExecutionError as exc:
        raise public_http_error(PublicError("setup_in_progress", str(exc), 409), legacy_error=True) from exc
    except OSError as exc:
        raise public_http_error(
            PublicError("runtime_uninstall_failed", "Runtime files could not be removed. Close any app using them and try again.", 409),
            legacy_error=True,
        ) from exc
    finally:
        # Recovery or a partial uninstall can change files and state before
        # any failure, so cached readiness is never trusted afterwards.
        cache.receipt_verifier.invalidate()
        cache.invalidate()
        if guard_acquired and callable(end_cleanup):
            end_cleanup()
    return {
        "deleted_model_groups": deleted_groups,
        "freed_bytes": freed_bytes,
        "state_deleted": state_deleted,
        **({"setup_reset_id": request.app.state.setup_reset_id} if not residual else {}),
        **model_storage_service._residual_notice(residual),
    }
