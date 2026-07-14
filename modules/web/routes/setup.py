"""Read-only runtime setup status for the desktop onboarding flow."""

from __future__ import annotations

import copy
from importlib import metadata
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Request
from huggingface_hub import scan_cache_dir
from huggingface_hub.errors import CacheNotFound
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from modules.runtime_profiles import get_runtime_profile, runtime_profiles
from modules.app.services.runtime_capability_service import RuntimeCapabilityService
from scripts import setup_runtime
from scripts.download_models import ModelReceiptVerifier, resolve_model_groups


router = APIRouter()

VersionResolver = Callable[[str], str]
StatusBuilder = Callable[[RuntimeCapabilityService], dict[str, Any]]


class RuntimeSetupStatusCache:
    """Small app-owned single-flight cache for expensive local readiness probes."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 4.0,
        clock: Callable[[], float] = time.monotonic,
        probe_service: RuntimeCapabilityService | None = None,
        receipt_verifier: ModelReceiptVerifier | None = None,
    ) -> None:
        self._ttl_seconds = max(0.1, min(float(ttl_seconds), 5.0))
        self._clock = clock
        self._probe_service = probe_service or RuntimeCapabilityService(
            cache_ttl_seconds=self._ttl_seconds,
            timeout_seconds=15.0,
        )
        self.receipt_verifier = receipt_verifier or ModelReceiptVerifier()
        self._condition = threading.Condition()
        self._refreshing = False
        self._key: tuple[Any, ...] | None = None
        self._expires_at = 0.0
        self._response: dict[str, Any] | None = None

    def get(
        self,
        key: tuple[Any, ...],
        builder: StatusBuilder,
    ) -> dict[str, Any]:
        while True:
            with self._condition:
                now = self._clock()
                if (
                    self._response is not None
                    and self._key == key
                    and now < self._expires_at
                ):
                    return copy.deepcopy(self._response)
                if not self._refreshing:
                    self._refreshing = True
                    break
                self._condition.wait()
        try:
            response = builder(self._probe_service)
        except Exception:
            with self._condition:
                self._refreshing = False
                self._condition.notify_all()
            raise
        with self._condition:
            self._key = key
            self._response = copy.deepcopy(response)
            self._expires_at = self._clock() + self._ttl_seconds
            self._refreshing = False
            self._condition.notify_all()
        return copy.deepcopy(response)

    def invalidate(self) -> None:
        """Allow the desktop setup runner to force an immediate next refresh."""
        with self._condition:
            self._key = None
            self._response = None
            self._expires_at = 0.0
            self.receipt_verifier.invalidate()
            self._condition.notify_all()


def _setup_state_fingerprint() -> tuple[Any, ...]:
    return setup_runtime.get_setup_state_cache_key()


def _manifest_requirements(
    manifest_path: Path,
    *,
    requirements_root: Path | None = None,
    visited: set[Path] | None = None,
) -> list[Requirement]:
    """Resolve applicable PEP 508 requirements from fixed recursive manifests."""
    path = manifest_path.resolve()
    root = (requirements_root or path.parent).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Requirements include escapes the manifest directory.") from exc

    seen = visited if visited is not None else set()
    if path in seen:
        return []
    seen.add(path)

    requirements: list[Requirement] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.split(r"\s+#", line, maxsplit=1)[0].strip()
        include_match = re.fullmatch(r"(?:-r|--requirement)\s+(.+)", line)
        if include_match:
            include_path = (path.parent / include_match.group(1).strip()).resolve()
            requirements.extend(
                _manifest_requirements(
                    include_path,
                    requirements_root=root,
                    visited=seen,
                )
            )
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement as exc:
            raise ValueError("Requirements manifest contains an invalid entry.") from exc
        if requirement.marker is None or requirement.marker.evaluate():
            requirements.append(requirement)
    return requirements


def _expected_requirement(requirement: Requirement) -> str:
    if requirement.specifier:
        return str(requirement.specifier)
    if requirement.url:
        filename = unquote(Path(urlsplit(requirement.url).path).name)
        try:
            _, version, _, _ = parse_wheel_filename(filename)
        except ValueError:
            return "direct artifact"
        return f"=={version}"
    return "installed"


def _safe_installed_version(value: str) -> str:
    normalized = str(value).strip()
    if len(normalized) <= 80 and re.fullmatch(r"[A-Za-z0-9.!+_-]+", normalized):
        return normalized
    return "unknown"


def _version_matches(requirement: Requirement, installed: str) -> bool:
    if requirement.specifier:
        return requirement.specifier.contains(installed, prereleases=True)
    if requirement.url:
        expected = _expected_requirement(requirement)
        if not expected.startswith("=="):
            return True
        try:
            return Version(installed) == Version(expected[2:])
        except InvalidVersion:
            return False
    return True


def _check_manifest_packages(
    manifest_path: Path,
    *,
    version_resolver: VersionResolver = metadata.version,
) -> dict[str, Any]:
    missing: list[dict[str, str | None]] = []
    incompatible: list[dict[str, str | None]] = []
    for requirement in _manifest_requirements(manifest_path):
        name = canonicalize_name(requirement.name)
        expected = _expected_requirement(requirement)
        try:
            installed_raw = version_resolver(name)
        except (metadata.PackageNotFoundError, KeyError):
            missing.append({"name": name, "expected": expected, "installed": None})
            continue
        installed = _safe_installed_version(installed_raw)
        if not _version_matches(requirement, installed_raw):
            incompatible.append(
                {"name": name, "expected": expected, "installed": installed}
            )
    return {
        "ready": not missing and not incompatible,
        "missing": missing,
        "incompatible": incompatible,
    }


def _check_selected_profile_packages(profile_id: str | None) -> dict[str, Any]:
    """Freshly validate every applicable package in the selected manifest."""
    if not profile_id:
        return {"ready": False, "missing": [], "incompatible": []}
    profile = get_runtime_profile(profile_id)
    return _check_manifest_packages(profile.requirements_file)


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


def _profile_payload(profile_id: str, *, accepted_profile: str | None) -> dict[str, Any]:
    profile = get_runtime_profile(profile_id)
    risk = None
    if profile.id == "cuda":
        risk = {
            "id": setup_runtime.PYTORCH_RISK_ID,
            "summary": setup_runtime.PYTORCH_RISK_SUMMARY,
            "accepted": accepted_profile == "cuda",
        }
    return {
        "id": profile.id,
        "label": profile.label,
        "description": profile.description,
        "compatible": profile.is_compatible(),
        "reason": profile.incompatibility_reason(),
        "model_groups": list(profile.model_groups),
        "risk": risk,
    }


def _empty_status(*, request: Request, state_error: str | None) -> dict[str, Any]:
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
        "profiles": [
            _profile_payload(profile.id, accepted_profile=None)
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
        return _empty_status(request=request, state_error=error)

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
    ready = (
        bool(status.get("ready"))
        and packages["ready"]
        and assets_ready
        and not restart_required
    )
    if restart_required:
        state = "restart_required"
    elif ready:
        state = "ready"
    else:
        state = "unavailable"

    risk = None
    if selected_profile == "cuda":
        risk = {
            "id": setup_runtime.PYTORCH_RISK_ID,
            "summary": setup_runtime.PYTORCH_RISK_SUMMARY,
            "accepted": True,
        }

    return {
        "schema_version": 1,
        "configured": True,
        "state": state,
        "state_error": None,
        "selected_profile": selected_profile,
        "selected_model_groups": selected_groups,
        "packages": packages,
        "probe": {"ready": probe_ready, "checks": probe_checks},
        "ready": ready,
        "restart_required": restart_required,
        "profiles": [
            _profile_payload(item.id, accepted_profile=selected_profile)
            for item in runtime_profiles()
        ],
        "risk": risk,
        "backend": {
            "instance_id": str(request.app.state.backend_instance_id),
            "status": "running",
        },
    }


def build_setup_status_response(request: Request) -> dict[str, Any]:
    cache = getattr(request.app.state, "runtime_setup_status_cache", None)
    if not isinstance(cache, RuntimeSetupStatusCache):
        cache = RuntimeSetupStatusCache()
        request.app.state.runtime_setup_status_cache = cache
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
