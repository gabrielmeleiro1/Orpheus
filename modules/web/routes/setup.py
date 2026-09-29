"""Runtime setup status and managed model storage for the desktop app."""

from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import re
import os
import shutil
import sys
import threading
from typing import Any, Callable, Mapping

from fastapi import APIRouter, HTTPException, Request
from huggingface_hub import scan_cache_dir
from huggingface_hub import constants as hf_constants
from huggingface_hub.errors import CacheNotFound
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
from modules.app.services.runtime_capability_service import RuntimeCapabilityService
from modules.web.deps import get_controller_from_app
from modules.app.services.library_service import _default_folder_opener
from modules.storage.maintenance_paths import workspace_root_for_db
from modules.web.setup_manifest import (
    VersionResolver,
    _check_manifest_packages,
    _expected_requirement,
    _manifest_requirements,
    _safe_installed_version,
    _version_matches,
)
from modules.web.setup_status_cache import RuntimeSetupStatusCache, StatusBuilder
from scripts import setup_runtime
from scripts.download_models import MODEL_GROUPS, ModelReceiptVerifier, resolve_model_groups


router = APIRouter()


@router.get("/api/system/storage")
def get_storage_folders(request: Request) -> dict[str, str]:
    config = get_controller_from_app(request.app).config
    return {
        "output_path": str(Path(config.output_dir).resolve()),
        "app_data_path": str(workspace_root_for_db(Path(config.db_path))),
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

# Re-exported so existing importers of this module keep resolving unchanged.
__all__ = [
    "AdoptRuntimeRequest",
    "ModelDownloadRequest",
    "RuntimeSetupStatusCache",
    "StatusBuilder",
    "VersionResolver",
    "_check_manifest_packages",
    "_expected_requirement",
    "_manifest_requirements",
    "_safe_installed_version",
    "_version_matches",
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


def _repo_has_complete_revision(
    repo: Any,
    *,
    model_id: str,
    receipt_verifier: ModelReceiptVerifier,
) -> bool:
    for revision in getattr(repo, "revisions", ()):
        snapshot_path = getattr(revision, "snapshot_path", None)
        commit_hash = getattr(revision, "commit_hash", None)
        if (
            isinstance(snapshot_path, Path)
            and isinstance(commit_hash, str)
            and receipt_verifier.verify(
                snapshot_path,
                model_id=model_id,
                revision=commit_hash,
            )
        ):
            return True
    return False


def _check_model_group_assets(
    group_ids: tuple[str, ...] | list[str],
    *,
    cache_scanner: Callable[[], Any] = scan_cache_dir,
    receipt_verifier: ModelReceiptVerifier | None = None,
) -> dict[str, dict[str, Any]]:
    """Inspect the local HF cache without network access or path disclosure."""
    if not group_ids:
        return {}
    try:
        cache = cache_scanner()
    except CacheNotFound:
        return {
            group_id: {"ready": False, "reason": "missing"}
            for group_id in group_ids
        }
    except Exception:
        return {
            group_id: {"ready": False, "reason": "unavailable"}
            for group_id in group_ids
        }
    repos = {
        str(repo.repo_id): repo
        for repo in getattr(cache, "repos", ())
        if getattr(repo, "repo_type", "model") == "model"
    }
    result: dict[str, dict[str, Any]] = {}
    verifier = receipt_verifier or ModelReceiptVerifier()
    for group_id in group_ids:
        model_ids = resolve_model_groups((group_id,))
        missing = any(model_id not in repos for model_id in model_ids)
        incomplete = any(
            model_id in repos
            and not _repo_has_complete_revision(
                repos[model_id],
                model_id=model_id,
                receipt_verifier=verifier,
            )
            for model_id in model_ids
        )
        reason = "missing" if missing else "incomplete" if incomplete else None
        result[group_id] = {"ready": reason is None, "reason": reason}
    return result


def _profile_payload(
    profile_id: str,
    *,
    accepted_profile: str | None,
    installed_runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = get_runtime_profile(profile_id)
    del accepted_profile
    reason = profile.incompatibility_reason()
    capabilities = {}
    if reason is None and profile.id == "cuda":
        detected = (
            installed_runtime
            if installed_runtime and installed_runtime["profile"] == "cuda" else {}
        )
        device = detected.get("device") or ("cpu" if cuda_hardware_error() else "cuda")
        capabilities = {
            "device": device,
            "group_availability": {
                group: {
                    "available": not (group.startswith("cleaner-cuda") and device == "cpu"),
                    "unavailable_reason": CUDA_CLEANER_UNAVAILABLE_REASON
                    if group.startswith("cleaner-cuda") and device == "cpu" else None,
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
        "risk": None,
    }


def _profile_tts_groups(profile_id: str) -> tuple[str, ...]:
    """Return the TTS model groups declared by a profile."""
    profile = get_runtime_profile(profile_id)
    return tuple(group for group in profile.model_groups if group.startswith("tts"))


def _detect_installed_runtime(
    probe_service: RuntimeCapabilityService,
    receipt_verifier: ModelReceiptVerifier,
    preferred_profile: str | None = None,
) -> dict[str, Any] | None:
    """Detect installed runtimes, including models outside the saved selection.

    Runs the cheap installed-packages check first and only probes the real
    capability path when packages are ready. Returns ``None`` on any probe
    error rather than guessing readiness.
    """
    for profile in runtime_profiles():
        if preferred_profile is not None and profile.id != preferred_profile:
            continue
        if profile.id == "base" or not profile.is_compatible():
            continue
        packages = _check_selected_profile_packages(profile.id)
        if not packages["ready"]:
            continue
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                assets = executor.submit(
                    _check_model_group_assets,
                    profile.model_groups,
                    receipt_verifier=receipt_verifier,
                )
                probe = setup_runtime._probe_profile(profile.model_groups, probe_service)
                asset_status = assets.result()
        except Exception:
            return None
        readiness_by_group = {
            str(check.get("id")): bool(check.get("available"))
            for check in probe.get("checks", [])
            if isinstance(check, Mapping)
        }
        model_groups = [
            {
                "id": group_id,
                "runtime_ready": readiness_by_group.get(group_id, False),
                "assets_ready": bool(asset_status.get(group_id, {}).get("ready")),
                "ready": bool(asset_status.get(group_id, {}).get("ready"))
                and readiness_by_group.get(group_id, False),
            }
            for group_id in profile.model_groups
        ]
        tts_groups = _profile_tts_groups(profile.id)
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
            "model_groups": model_groups,
        }
        if profile.id == "cuda":
            result["device"] = next(
                (check["device"] for check in probe.get("checks", []) if check.get("device")),
                "cpu" if cuda_hardware_error() else "cuda",
            )
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
        "probe": {"ready": False, "checks": []},
        "ready": False,
        "restart_required": False,
        "recommended_profile": recommended_profile_id(),
        "installed_runtime": installed_runtime,
        "profiles": [
            _profile_payload(profile.id, accepted_profile=None, installed_runtime=installed_runtime)
            for profile in runtime_profiles()
        ],
        "risk": None,
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
    try:
        status = setup_runtime.get_setup_status(probe_service=probe_service)
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
    for check in raw_checks:
        if not isinstance(check, Mapping):
            continue
        group_id = str(check.get("id") or "")
        if group_id in selected_group_ids:
            readiness_by_group[group_id] = bool(check.get("available"))

    probe_checks = [
        {"id": group_id, "ready": readiness_by_group.get(group_id, False)}
        for group_id in selected_group_ids
    ]
    probe_ready = bool(raw_probe.get("available")) if isinstance(raw_probe, Mapping) else False
    packages = _check_selected_profile_packages(selected_profile)
    asset_status = _check_model_group_assets(
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
    else:
        state = "unavailable"

    risk = None

    installed_runtime = _detect_installed_runtime(
        probe_service, receipt_verifier,
        preferred_profile=selected_profile if selected_profile != "base" else None,
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
        "ready": ready,
        "restart_required": restart_required,
        "recommended_profile": recommended_profile_id(),
        # Settings must also expose optional models outside the saved selection.
        "installed_runtime": installed_runtime,
        "profiles": [
            _profile_payload(item.id, accepted_profile=selected_profile, installed_runtime=installed_runtime)
            for item in runtime_profiles()
        ],
        "risk": risk,
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
        )
        request.app.state.runtime_setup_status_cache = cache
        return cache


def build_setup_status_response(request: Request) -> dict[str, Any]:
    cache = _get_status_cache(request)
    key = _setup_state_fingerprint()
    return cache.get(
        key,
        lambda service: _build_uncached_setup_status_response(
            request,
            service,
            cache.receipt_verifier,
        ),
    )


@router.get("/api/setup/status")
def get_setup_status(request: Request) -> dict[str, Any]:
    return build_setup_status_response(request)


class AdoptRuntimeRequest(BaseModel):
    """Adopt an already-operational runtime (or library-only) without installing."""

    model_config = ConfigDict(extra="forbid")

    profile: str
    model_groups: list[str] = Field(default_factory=list)
    accept_pytorch_risk: bool = False
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
        profile = setup_runtime._validated_profile(payload.profile)
        groups = setup_runtime._validated_model_groups(profile, payload.model_groups)
        setup_runtime.validate_model_selection(groups)
        setup_runtime.require_voice_model(profile.id, groups)
    except setup_runtime.SetupValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_request", "message": str(exc)},
        ) from exc

    if profile.id != "base":
        # Every readiness check below folds over `groups`, so an empty selection
        # would pass all of them vacuously and persist a "ready" state for a
        # profile with no TTS model installed. post_model_download guards this
        # the same way.
        if not groups:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_request",
                    "message": "At least one model group is required.",
                },
            )
        packages = _check_selected_profile_packages(profile.id)
        if not packages["ready"]:
            raise HTTPException(
                status_code=409,
                detail={"error": "packages_not_ready", "packages": packages},
            )
        try:
            probe = setup_runtime._probe_profile(groups, cache.probe_service)
        except Exception as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "probe_unavailable"},
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
            raise HTTPException(
                status_code=409,
                detail={"error": "probe_failed", "probe": {"checks": probe_checks}},
            )
        asset_status = _check_model_group_assets(
            groups,
            receipt_verifier=cache.receipt_verifier,
        )
        missing_assets = [
            {"id": group_id, "reason": asset_status[group_id]["reason"]}
            for group_id in groups
            if not asset_status[group_id]["ready"]
        ]
        if missing_assets:
            raise HTTPException(
                status_code=409,
                detail={"error": "assets_missing", "model_groups": missing_assets},
            )

    accepted_risks: tuple[str, ...] = ()
    state_path = setup_runtime.default_state_path()
    try:
        with setup_runtime.SetupLock(state_path):
            saved_state = setup_runtime.write_setup_state(
                state_path,
                profile_id=profile.id,
                model_groups=groups,
                accepted_risks=accepted_risks,
                cleaner_cuda_quantization=payload.cleaner_cuda_quantization,
                backend_instance_id=_adopt_backend_instance_id(request),
                restart_required=False,
            )
            config = getattr(get_controller_from_app(request.app), "config", None)
            apply_preferences = getattr(config, "apply_setup_preferences", None)
            if callable(apply_preferences):
                apply_preferences(state=saved_state)
    except setup_runtime.SetupExecutionError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "setup_in_progress", "message": str(exc)},
        ) from exc
    except setup_runtime.SetupValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_request", "message": str(exc)},
        ) from exc

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
    probe/asset/risk validation in ``POST /api/setup/adopt``.
    """
    try:
        profile = setup_runtime._validated_profile(payload.profile)
        groups = setup_runtime._validated_model_groups(profile, payload.model_groups)
        if payload.configure_runtime:
            setup_runtime.validate_model_selection(groups)
            setup_runtime.require_voice_model(profile.id, groups)
    except setup_runtime.SetupValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_request", "message": str(exc)},
        ) from exc
    if not groups:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_request",
                "message": "At least one model group is required.",
            },
        )

    service = _get_download_service(request)
    if any(group.startswith("cleaner-cuda") for group in groups):
        reason = cuda_hardware_error()
        if reason:
            raise HTTPException(422, detail={"error": "invalid_request", "message": reason})
    try:
        return service.start(profile_id=profile.id, model_groups=groups, configure_runtime=payload.configure_runtime, cleaner_cuda_quantization=payload.cleaner_cuda_quantization)
    except ModelDownloadBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "download_in_progress"},
        ) from exc
    except setup_runtime.SetupExecutionError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "setup_in_progress", "message": str(exc)},
        ) from exc


@router.post("/api/setup/models/download/cancel")
def post_model_download_cancel(request: Request) -> dict[str, Any]:
    """Cooperatively cancel a packaged-runtime model download."""
    return _get_download_service(request).request_cancel()


def _managed_model_repo_ids() -> dict[str, str]:
    """Map each managed HF repo id to the model group that owns it."""
    owners: dict[str, str] = {}
    for group_id in MODEL_GROUPS:
        for model_id in resolve_model_groups((group_id,)):
            owners.setdefault(str(model_id), group_id)
    return owners


def _managed_cache_paths(cache: Any, *, cache_root: Path | None = None) -> dict[str, Path]:
    # Only construct names from our fixed model catalog, never request paths or warnings.
    roots = {Path(repo.repo_path).parent for repo in getattr(cache, "repos", ())}
    if cache_root is not None:
        roots = {cache_root}
    elif not roots:
        roots = {Path(hf_constants.HF_HUB_CACHE)}
    return {
        model_id: root / ("models--" + model_id.replace("/", "--"))
        for root in roots for model_id in _managed_model_repo_ids()
    }


def _cache_bytes(path: Path) -> int:
    if path.is_symlink() or path.is_junction():
        return 0
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink() and not (Path(root) / name).is_junction()]
        for name in files:
            file = Path(root) / name
            if not file.is_symlink():
                try:
                    total += file.stat().st_size
                except FileNotFoundError:
                    pass
    return total


@contextmanager
def _stage_runtime_models(group_id: str):
    try:
        cache = scan_cache_dir()
    except CacheNotFound:
        cache = None
    staged: dict[str, Path] = {}
    originals: dict[str, Path] = {}
    try:
        for model_id, path in _managed_cache_paths(cache).items():
            if model_id not in resolve_model_groups((group_id,)):
                continue
            if not path.exists() or path.is_symlink() or path.is_junction():
                continue
            destination = path.with_name(f".orpheus-delete-{uuid4().hex}")
            path.rename(destination)
            originals[model_id] = path
            staged[model_id] = destination
        yield staged
    finally:
        # Roll back on a state-write failure; restore residuals for later retries.
        for model_id, path in staged.items():
            if path.exists():
                path.rename(originals[model_id])


def _delete_runtime_models(
    *,
    cache_scanner: Callable[[], Any] | None = None,
    model_groups: tuple[str, ...] | None = None,
    cache_root: Path | None = None,
    model_paths: dict[str, Path] | None = None,
) -> tuple[list[str], int]:
    """Delete only HF cache repos owned by our model groups; leave others intact."""
    if model_paths is None:
        try:
            cache = (cache_scanner or scan_cache_dir)()
        except CacheNotFound:
            return [], 0
        if cache_scanner is not None and not cache.repos and cache_root is None:
            return [], 0
        model_paths = _managed_cache_paths(cache, cache_root=cache_root)
    owners = _managed_model_repo_ids()
    deleted_groups: set[str] = set()
    freed = 0
    for model_id, path in model_paths.items():
        group_id = owners[model_id]
        if model_groups is not None and group_id not in model_groups:
            continue
        if not path.exists() or path.is_symlink() or path.is_junction():
            continue
        before = _cache_bytes(path)
        try:
            shutil.rmtree(path)
        except OSError:
            pass  # The rescan below reports partial deletion, including locked files.
        freed += max(0, before - _cache_bytes(path))
        if not path.exists():
            deleted_groups.add(group_id)
    ordered = [group_id for group_id in MODEL_GROUPS if group_id in deleted_groups]
    return ordered, freed


@router.get("/api/setup/models")
def get_managed_models() -> dict[str, Any]:
    """List managed cache usage without disclosing filesystem paths."""
    try:
        cache = scan_cache_dir()
        repos = cache.repos
    except CacheNotFound:
        cache = None
        repos = ()
    except OSError as exc:
        raise HTTPException(503, detail={"error": "cache_unavailable", "message": "Model storage could not be read. Try again."}) from exc
    by_id = {repo.repo_id: repo for repo in repos if repo.repo_type == "model"}
    paths = _managed_cache_paths(cache)
    return {"models": [
        {
            "id": group,
            "downloaded": any(paths[model_id].exists() for model_id in resolve_model_groups((group,))),
            "size_bytes": sum(_cache_bytes(paths[model_id]) for model_id in resolve_model_groups((group,))),
            **({"damaged": True} if any(paths[model_id].exists() and model_id not in by_id for model_id in resolve_model_groups((group,))) else {}),
        }
        for group in MODEL_GROUPS
    ]}


@router.delete("/api/setup/models/{group_id}")
def delete_managed_model(group_id: str, request: Request) -> dict[str, Any]:
    """Remove exactly one managed model group, preserving other models and settings."""
    if group_id not in MODEL_GROUPS:
        raise HTTPException(422, detail={"error": "invalid_model", "message": "Unknown managed model."})
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
                raise HTTPException(409, detail={"error": "conversion_in_progress", "message": "Wait for the current conversion to finish before deleting a model."}) from exc
        with setup_runtime.SetupLock(state_path, timeout_seconds=0.5):
            # Read the state first so an unreadable configuration cannot be
            # discovered only after its model files have already been removed.
            state, _ = setup_runtime._read_setup_state(state_path)
            if state and group_id in state["model_groups"] and not group_id.startswith("tts-"):
                remaining = tuple(group for group in state["model_groups"] if group != group_id)
                with _stage_runtime_models(group_id) as staged:
                    try:
                        saved_state = setup_runtime.write_setup_state(
                            state_path,
                            profile_id=state["profile"] if remaining else "base",
                            model_groups=remaining,
                            accepted_risks=tuple(state["accepted_risks"]),
                            cleaner_cuda_quantization=state["cleaner_cuda_quantization"],
                            backend_instance_id=_adopt_backend_instance_id(request),
                            restart_required=False,
                        )
                    except (setup_runtime.SetupExecutionError, OSError) as exc:
                        raise HTTPException(503, detail={"error": "setup_state_write_failed", "message": "Model settings could not be saved. Try again."}) from exc
                    config = getattr(controller, "config", None)
                    apply_preferences = getattr(config, "apply_setup_preferences", None)
                    if callable(apply_preferences):
                        apply_preferences(state=saved_state)
                    deleted, freed = _delete_runtime_models(model_groups=(group_id,), model_paths=staged)
            else:
                deleted, freed = _delete_runtime_models(model_groups=(group_id,))
            residual = [model["id"] for model in get_managed_models()["models"] if model["id"] == group_id and model["downloaded"]]
    except setup_runtime.SetupExecutionError as exc:
        raise HTTPException(409, detail={"error": "setup_in_progress", "message": str(exc)}) from exc
    except OSError as exc:
        raise HTTPException(409, detail={"error": "model_delete_failed", "message": "The model could not be deleted. Close any app using it and try again."}) from exc
    finally:
        cache.receipt_verifier.invalidate()
        cache.invalidate()
        if acquired and callable(end):
            end()
    return {"deleted_model_groups": deleted, "freed_bytes": freed, "state_deleted": False, **_residual_notice(residual)}


def _residual_notice(groups: list[str]) -> dict[str, Any]:
    if not groups:
        return {}
    return {"residual_model_groups": groups, "message": "Couldn't delete some files — they may be in use. Close other apps and retry."}


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
            raise HTTPException(
                status_code=409,
                detail={"error": "conversion_in_progress", "message": str(exc)},
            ) from exc
    try:
        with setup_runtime.SetupLock(state_path):
            deleted_groups, freed_bytes = _delete_runtime_models()
            residual = [model["id"] for model in get_managed_models()["models"] if model["downloaded"]]
            state_deleted = state_path.exists()
            if not residual:
                state_path.unlink(missing_ok=True)
            else:
                state_deleted = False
    except setup_runtime.SetupExecutionError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "setup_in_progress", "message": str(exc)},
        ) from exc
    finally:
        if guard_acquired and callable(end_cleanup):
            end_cleanup()
    cache.invalidate()
    return {
        "deleted_model_groups": deleted_groups,
        "freed_bytes": freed_bytes,
        "state_deleted": state_deleted,
        **_residual_notice(residual),
    }
