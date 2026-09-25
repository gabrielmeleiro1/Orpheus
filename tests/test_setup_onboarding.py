"""Capability-driven onboarding: installed-runtime detection, adopt, uninstall."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading

import pytest
from fastapi.testclient import TestClient
from huggingface_hub import scan_cache_dir

from modules.web.server import create_app


def _client() -> TestClient:
    return TestClient(create_app(controller=object(), desktop_mode=False))


@pytest.mark.parametrize("imports_work", [True, False])
def test_cpu_narration_status_without_cuda(monkeypatch, imports_work):
    from modules.app.services.runtime_capability_service import RuntimeCapabilityService
    from modules.web.routes import setup as setup_route
    from modules.runtime_profiles import CUDA_CLEANER_UNAVAILABLE_REASON

    monkeypatch.setattr("modules.runtime_profiles.platform.system", lambda: "Windows")
    monkeypatch.setattr(setup_route.setup_runtime, "get_setup_status", _unconfigured_absent)
    monkeypatch.setattr(setup_route, "cuda_hardware_error", lambda: CUDA_CLEANER_UNAVAILABLE_REASON)
    monkeypatch.setattr(setup_route, "_check_selected_profile_packages", lambda _: {"ready": True})
    monkeypatch.setattr(setup_route, "_check_model_group_assets", lambda groups, **_: {
        group: {"ready": True, "reason": None} for group in groups
    })
    monkeypatch.setattr(RuntimeCapabilityService, "probe_package_tts_runtime", lambda _: {
        "available": imports_work, "device": "cpu", "error": None if imports_work else "broken import",
    })
    monkeypatch.setattr(RuntimeCapabilityService, "probe_cleaner_backend", lambda *args: {
        "available": False, "error": CUDA_CLEANER_UNAVAILABLE_REASON,
    })
    with _client() as client:
        payload = client.get("/api/setup/status").json()
    profile = next(item for item in payload["profiles"] if item["id"] == "cuda")
    assert payload["recommended_profile"] == "cuda"
    assert profile["compatible"] is True
    assert profile["device"] == "cpu"
    assert profile["group_availability"]["tts-cpu-cuda"]["available"] is True
    for group in ("cleaner-cuda", "cleaner-cuda-large"):
        assert profile["group_availability"][group] == {
            "available": False, "unavailable_reason": CUDA_CLEANER_UNAVAILABLE_REASON,
        }
    assert payload["installed_runtime"]["runtime_available"] is imports_work
    assert payload["installed_runtime"]["ready"] is imports_work
    assert [group["ready"] for group in payload["installed_runtime"]["model_groups"]] == [imports_work, False, False]


def _all_profiles_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.runtime_profiles import RuntimeProfile
    from modules.web.routes import setup as setup_route

    monkeypatch.setattr(RuntimeProfile, "is_compatible", lambda self, **kwargs: True)
    monkeypatch.setattr(setup_route, "cuda_hardware_error", lambda: None)


def _probe(available: bool):
    def _fake_probe(groups, service):
        del service
        return {
            "available": available,
            "checks": [
                {"id": group_id, "available": available, "error": None}
                for group_id in groups
            ],
        }

    return _fake_probe


# --------------------------------------------------------------------------- #
# recommended_profile top-level field                                         #
# --------------------------------------------------------------------------- #


def test_status_reports_recommended_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.web.routes import setup as setup_route

    monkeypatch.setattr(
        setup_route,
        "recommended_profile_id",
        lambda: "cuda",
    )
    monkeypatch.setattr(
        setup_route.setup_runtime,
        "get_setup_status",
        lambda **kwargs: {
            "configured": False,
            "profile": None,
            "model_groups": [],
            "ready": False,
            "restart_required": False,
            "probe": {"available": False, "checks": []},
            "profiles": [],
            "risk": None,
            "state_error": None,
        },
    )
    # Absent state → detection runs; keep it cheap and null.
    monkeypatch.setattr(
        setup_route, "_detect_installed_runtime", lambda *_args, **_kwargs: None
    )

    with _client() as client:
        payload = client.get("/api/setup/status").json()

    assert payload["recommended_profile"] == "cuda"
    assert payload["installed_runtime"] is None


# --------------------------------------------------------------------------- #
# installed_runtime detection for a baked/packaged runtime                     #
# --------------------------------------------------------------------------- #


def _unconfigured_absent(**kwargs):
    del kwargs
    return {
        "configured": False,
        "profile": None,
        "model_groups": [],
        "ready": False,
        "restart_required": False,
        "probe": {"available": False, "checks": []},
        "profiles": [],
        "risk": None,
        "state_error": None,
    }


def test_setup_detection_checks_assets_and_runtime_concurrently(monkeypatch):
    from modules.web.routes import setup as setup_route

    _all_profiles_compatible(monkeypatch)
    monkeypatch.setattr(setup_route, "_check_selected_profile_packages", lambda _: {"ready": True})
    both_started = threading.Barrier(2)

    def assets(groups, **kwargs):
        both_started.wait(timeout=5)
        return {group: {"ready": group.startswith("tts")} for group in groups}

    def probe(groups, service):
        both_started.wait(timeout=5)
        return _probe(True)(groups, service)

    monkeypatch.setattr(setup_route, "_check_model_group_assets", assets)
    monkeypatch.setattr(setup_route.setup_runtime, "_probe_profile", probe)
    result = setup_route._detect_installed_runtime(object(), object())
    assert result is not None
    assert result["ready"] is True
    assert result["model_groups"][0]["ready"] is True
    assert result["model_groups"][1]["ready"] is False


def test_installed_runtime_detected_ready_for_baked_mlx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.web.routes import setup as setup_route

    _all_profiles_compatible(monkeypatch)
    monkeypatch.setattr(setup_route.setup_runtime, "get_setup_status", _unconfigured_absent)
    monkeypatch.setattr(
        setup_route,
        "_check_selected_profile_packages",
        lambda profile_id: {"ready": True, "missing": [], "incompatible": []},
    )
    monkeypatch.setattr(setup_route.setup_runtime, "_probe_profile", _probe(True))
    monkeypatch.setattr(
        setup_route,
        "_check_model_group_assets",
        lambda group_ids, **kwargs: {
            group_id: {"ready": True, "reason": None} for group_id in group_ids
        },
    )

    with _client() as client:
        payload = client.get("/api/setup/status").json()

    assert payload["configured"] is False
    assert payload["installed_runtime"] == {
        "profile": "mlx",
        "ready": True,
        "runtime_available": True,
        "model_groups": [
            {"id": "tts-mlx", "runtime_ready": True, "assets_ready": True, "ready": True},
            {"id": "cleaner-mlx", "runtime_ready": True, "assets_ready": True, "ready": True},
            {"id": "cleaner-mlx-small", "runtime_ready": True, "assets_ready": True, "ready": True},
        ],
    }


def test_installed_runtime_not_ready_when_probe_reports_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.web.routes import setup as setup_route

    _all_profiles_compatible(monkeypatch)
    monkeypatch.setattr(setup_route.setup_runtime, "get_setup_status", _unconfigured_absent)
    monkeypatch.setattr(
        setup_route,
        "_check_selected_profile_packages",
        lambda profile_id: {"ready": True, "missing": [], "incompatible": []},
    )
    monkeypatch.setattr(setup_route.setup_runtime, "_probe_profile", _probe(False))
    monkeypatch.setattr(
        setup_route,
        "_check_model_group_assets",
        lambda group_ids, **kwargs: {
            group_id: {"ready": True, "reason": None} for group_id in group_ids
        },
    )

    with _client() as client:
        payload = client.get("/api/setup/status").json()

    assert payload["installed_runtime"]["profile"] == "mlx"
    assert payload["installed_runtime"]["ready"] is False
    assert all(
        group["ready"] is False for group in payload["installed_runtime"]["model_groups"]
    )


def test_installed_runtime_absent_when_packages_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.web.routes import setup as setup_route

    _all_profiles_compatible(monkeypatch)
    monkeypatch.setattr(setup_route.setup_runtime, "get_setup_status", _unconfigured_absent)
    monkeypatch.setattr(
        setup_route,
        "_check_selected_profile_packages",
        lambda profile_id: {"ready": False, "missing": [], "incompatible": []},
    )

    def _must_not_probe(groups, service):
        raise AssertionError("probe must not run when packages are not ready")

    monkeypatch.setattr(setup_route.setup_runtime, "_probe_profile", _must_not_probe)

    with _client() as client:
        payload = client.get("/api/setup/status").json()

    assert payload["installed_runtime"] is None


def test_setup_status_survives_missing_requirements_manifests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: a packaged sidecar built without the requirements manifests
    turned every /api/setup/status call into a FileNotFoundError 500."""
    import dataclasses

    from modules.web.routes import setup as setup_route

    _all_profiles_compatible(monkeypatch)
    monkeypatch.setattr(setup_route.setup_runtime, "get_setup_status", _unconfigured_absent)
    relocated = {
        profile.id: dataclasses.replace(
            profile, requirements_file=tmp_path / "absent" / f"{profile.id}.txt"
        )
        for profile in setup_route.runtime_profiles()
    }
    monkeypatch.setattr(
        setup_route, "runtime_profiles", lambda: tuple(relocated.values())
    )
    monkeypatch.setattr(setup_route, "get_runtime_profile", lambda pid: relocated[pid])

    def _must_not_probe(groups, service):
        raise AssertionError("probe must not run when manifests are unreadable")

    monkeypatch.setattr(setup_route.setup_runtime, "_probe_profile", _must_not_probe)

    with _client() as client:
        response = client.get("/api/setup/status")

    assert response.status_code == 200
    assert response.json()["installed_runtime"] is None


def test_installed_runtime_null_on_probe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.web.routes import setup as setup_route

    _all_profiles_compatible(monkeypatch)
    monkeypatch.setattr(setup_route.setup_runtime, "get_setup_status", _unconfigured_absent)
    monkeypatch.setattr(
        setup_route,
        "_check_selected_profile_packages",
        lambda profile_id: {"ready": True, "missing": [], "incompatible": []},
    )

    def _broken_probe(groups, service):
        raise setup_route.setup_runtime.SetupExecutionError("probe unavailable")

    monkeypatch.setattr(setup_route.setup_runtime, "_probe_profile", _broken_probe)

    with _client() as client:
        payload = client.get("/api/setup/status").json()

    assert payload["installed_runtime"] is None


def _configured_status(profile: str, model_groups: list[str]):
    def _get_setup_status(**kwargs):
        del kwargs
        return {
            "configured": True,
            "profile": profile,
            "model_groups": model_groups,
            "ready": True,
            "restart_required": False,
            "probe": {
                "available": True,
                "checks": [
                    {"id": group_id, "available": True, "error": None}
                    for group_id in model_groups
                ],
            },
            "state_error": None,
        }

    return _get_setup_status


def test_installed_runtime_includes_optional_models_when_local_profile_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.web.routes import setup as setup_route

    _all_profiles_compatible(monkeypatch)
    monkeypatch.setattr(
        setup_route.setup_runtime,
        "get_setup_status",
        _configured_status("mlx", ["tts-mlx"]),
    )
    monkeypatch.setattr(
        setup_route,
        "_check_selected_profile_packages",
        lambda profile_id: {"ready": True, "missing": [], "incompatible": []},
    )
    monkeypatch.setattr(
        setup_route,
        "_check_model_group_assets",
        lambda group_ids, **kwargs: {
            group_id: {"ready": True, "reason": None} for group_id in group_ids
        },
    )

    monkeypatch.setattr(
        setup_route.setup_runtime,
        "_probe_profile",
        lambda groups, service: {
            "checks": [{"id": group, "available": True} for group in groups]
        },
    )

    with _client() as client:
        payload = client.get("/api/setup/status").json()

    assert payload["configured"] is True
    assert payload["installed_runtime"]["ready"] is True
    assert [group["id"] for group in payload["selected_model_groups"]] == ["tts-mlx"]
    assert {group["id"] for group in payload["installed_runtime"]["model_groups"]} == {
        "tts-mlx", "cleaner-mlx", "cleaner-mlx-small"
    }
    assert payload["recommended_profile"] in {"base", "mlx", "cuda"}


def test_installed_runtime_detected_for_configured_library_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Library-only users keep seeing the baked runtime so they can upgrade."""
    from modules.web.routes import setup as setup_route

    monkeypatch.setattr(
        setup_route.setup_runtime,
        "get_setup_status",
        _configured_status("base", []),
    )
    monkeypatch.setattr(
        setup_route,
        "_check_selected_profile_packages",
        lambda profile_id: {"ready": True, "missing": [], "incompatible": []},
    )
    detected = {
        "profile": "mlx",
        "ready": False,
        "runtime_available": True,
        "model_groups": [
            {"id": "tts-mlx", "runtime_ready": True, "assets_ready": False, "ready": False},
        ],
    }
    monkeypatch.setattr(
        setup_route, "_detect_installed_runtime", lambda *args, **kwargs: detected
    )

    with _client() as client:
        payload = client.get("/api/setup/status").json()

    assert payload["configured"] is True
    assert payload["installed_runtime"] == detected


def test_installed_runtime_splits_runtime_and_asset_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A baked runtime with missing models must advertise runtime_available."""
    from modules.web.routes import setup as setup_route

    _all_profiles_compatible(monkeypatch)
    monkeypatch.setattr(setup_route.setup_runtime, "get_setup_status", _unconfigured_absent)
    monkeypatch.setattr(
        setup_route,
        "_check_selected_profile_packages",
        lambda profile_id: {"ready": True, "missing": [], "incompatible": []},
    )
    monkeypatch.setattr(setup_route.setup_runtime, "_probe_profile", _probe(True))
    monkeypatch.setattr(
        setup_route,
        "_check_model_group_assets",
        lambda group_ids, **kwargs: {
            group_id: {"ready": False, "reason": "missing"} for group_id in group_ids
        },
    )

    with _client() as client:
        payload = client.get("/api/setup/status").json()

    assert payload["installed_runtime"] == {
        "profile": "mlx",
        "ready": False,
        "runtime_available": True,
        "model_groups": [
            {"id": "tts-mlx", "runtime_ready": True, "assets_ready": False, "ready": False},
            {"id": "cleaner-mlx", "runtime_ready": True, "assets_ready": False, "ready": False},
            {"id": "cleaner-mlx-small", "runtime_ready": True, "assets_ready": False, "ready": False},
        ],
    }


def test_frozen_bundle_reports_manifest_packages_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frozen bundles carry build-env metadata; the probe is the real gate."""
    from modules.web.routes import setup as setup_route

    monkeypatch.setattr(setup_route.sys, "frozen", True, raising=False)

    status = setup_route._check_selected_profile_packages("mlx")

    assert status == {"ready": True, "missing": [], "incompatible": []}


# --------------------------------------------------------------------------- #
# POST /api/setup/adopt                                                        #
# --------------------------------------------------------------------------- #


def _state_path(tmp_path: Path) -> Path:
    return tmp_path / "runtime-setup.json"


def test_adopt_library_only_persists_ready_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from modules.web.routes import setup as setup_route

    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        setup_route,
        "_check_selected_profile_packages",
        lambda profile_id: {"ready": True, "missing": [], "incompatible": []},
    )

    with _client() as client:
        response = client.post(
            "/api/setup/adopt",
            json={"profile": "base", "model_groups": []},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["configured"] is True
    assert payload["state"] == "ready"
    assert payload["ready"] is True
    assert payload["restart_required"] is False
    assert payload["selected_profile"] == "base"

    state = json.loads(_state_path(tmp_path).read_text(encoding="utf-8"))
    assert state["profile"] == "base"
    assert state["restart_required"] is False


def test_adopt_operational_mlx_persists_without_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from modules.web.routes import setup as setup_route

    _all_profiles_compatible(monkeypatch)
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        setup_route,
        "_check_selected_profile_packages",
        lambda profile_id: {"ready": True, "missing": [], "incompatible": []},
    )
    monkeypatch.setattr(setup_route.setup_runtime, "_probe_profile", _probe(True))
    monkeypatch.setattr(
        setup_route,
        "_check_model_group_assets",
        lambda group_ids, **kwargs: {
            group_id: {"ready": True, "reason": None} for group_id in group_ids
        },
    )

    with _client() as client:
        response = client.post(
            "/api/setup/adopt",
            json={"profile": "mlx", "model_groups": ["tts-mlx"]},
        )

    assert response.status_code == 200
    payload = response.json()
    assert (payload["configured"], payload["state"], payload["ready"]) == (
        True,
        "ready",
        True,
    )
    assert payload["restart_required"] is False
    assert payload["selected_profile"] == "mlx"
    assert payload["selected_model_groups"][0] == {
        "id": "tts-mlx",
        "assets_ready": True,
        "assets_reason": None,
        "runtime_ready": True,
        "ready": True,
    }

    state = json.loads(_state_path(tmp_path).read_text(encoding="utf-8"))
    assert state["profile"] == "mlx"
    assert state["model_groups"] == ["tts-mlx"]
    assert state["restart_required"] is False


def test_adopt_rejects_unknown_profile_with_422(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    with _client() as client:
        response = client.post("/api/setup/adopt", json={"profile": "nope"})

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_request"
    assert not _state_path(tmp_path).exists()


def test_adopt_rejects_unknown_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    with _client() as client:
        response = client.post(
            "/api/setup/adopt",
            json={"profile": "base", "model_groups": [], "sneaky": True},
        )

    assert response.status_code == 422


def test_adopt_cuda_no_longer_requires_resolved_pytorch_risk_ack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from modules.web.routes import setup as setup_route

    _all_profiles_compatible(monkeypatch)
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        setup_route,
        "_check_selected_profile_packages",
        lambda profile_id: {"ready": True, "missing": [], "incompatible": []},
    )
    monkeypatch.setattr(
        setup_route,
        "_check_model_group_assets",
        lambda groups, **kwargs: {
            group: {"ready": True, "reason": None} for group in groups
        },
    )
    monkeypatch.setattr(
        setup_route.setup_runtime,
        "_probe_profile",
        lambda groups, service: {"available": True, "checks": []},
    )

    with _client() as client:
        response = client.post(
            "/api/setup/adopt",
            json={
                "profile": "cuda",
                "model_groups": ["tts-cpu-cuda"],
                "accept_pytorch_risk": False,
            },
        )

    assert response.status_code == 200
    assert response.json()["risk"] is None
    assert _state_path(tmp_path).exists()


def test_adopt_conflicts_when_packages_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from modules.web.routes import setup as setup_route

    _all_profiles_compatible(monkeypatch)
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        setup_route,
        "_check_selected_profile_packages",
        lambda profile_id: {
            "ready": False,
            "missing": [{"name": "mlx", "expected": "==1.0", "installed": None}],
            "incompatible": [],
        },
    )

    with _client() as client:
        response = client.post(
            "/api/setup/adopt",
            json={"profile": "mlx", "model_groups": ["tts-mlx"]},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "packages_not_ready"
    assert not _state_path(tmp_path).exists()


def test_adopt_conflicts_when_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from modules.web.routes import setup as setup_route

    _all_profiles_compatible(monkeypatch)
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        setup_route,
        "_check_selected_profile_packages",
        lambda profile_id: {"ready": True, "missing": [], "incompatible": []},
    )
    monkeypatch.setattr(setup_route.setup_runtime, "_probe_profile", _probe(False))

    with _client() as client:
        response = client.post(
            "/api/setup/adopt",
            json={"profile": "mlx", "model_groups": ["tts-mlx"]},
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "probe_failed"
    assert detail["probe"]["checks"] == [{"id": "tts-mlx", "ready": False}]
    assert not _state_path(tmp_path).exists()


def test_adopt_conflicts_when_assets_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from modules.web.routes import setup as setup_route

    _all_profiles_compatible(monkeypatch)
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        setup_route,
        "_check_selected_profile_packages",
        lambda profile_id: {"ready": True, "missing": [], "incompatible": []},
    )
    monkeypatch.setattr(setup_route.setup_runtime, "_probe_profile", _probe(True))
    monkeypatch.setattr(
        setup_route,
        "_check_model_group_assets",
        lambda group_ids, **kwargs: {
            group_id: {"ready": False, "reason": "missing"} for group_id in group_ids
        },
    )

    with _client() as client:
        response = client.post(
            "/api/setup/adopt",
            json={"profile": "mlx", "model_groups": ["tts-mlx"]},
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "assets_missing"
    assert detail["model_groups"] == [{"id": "tts-mlx", "reason": "missing"}]
    assert not _state_path(tmp_path).exists()


def test_adopt_conflicts_when_setup_in_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from modules.web.routes import setup as setup_route

    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        setup_route,
        "_check_selected_profile_packages",
        lambda profile_id: {"ready": True, "missing": [], "incompatible": []},
    )

    class _RaisingLock:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            raise setup_route.setup_runtime.SetupExecutionError(
                "Another runtime setup operation is already running."
            )

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(setup_route.setup_runtime, "SetupLock", _RaisingLock)

    with _client() as client:
        response = client.post("/api/setup/adopt", json={"profile": "base"})

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "setup_in_progress"


# --------------------------------------------------------------------------- #
# DELETE /api/setup/runtime                                                    #
# --------------------------------------------------------------------------- #


def _make_hf_repo(cache: Path, repo_id: str, commit: str, content: bytes) -> None:
    repo = cache / f"models--{repo_id.replace('/', '--')}"
    (repo / "refs").mkdir(parents=True, exist_ok=True)
    (repo / "refs" / "main").write_text(commit, encoding="utf-8")
    blobs = repo / "blobs"
    blobs.mkdir(exist_ok=True)
    blob = blobs / f"blob-{len(content)}"
    blob.write_bytes(content)
    snapshot = repo / "snapshots" / commit
    snapshot.mkdir(parents=True, exist_ok=True)
    link = snapshot / "config.json"
    try:
        os.symlink(blob, link)
    except OSError:
        link.write_bytes(content)


def test_delete_runtime_models_only_removes_managed_repos(tmp_path: Path) -> None:
    from modules.web.routes import setup as setup_route
    from scripts.download_models import resolve_model_groups

    managed_repo_id = resolve_model_groups(("tts-mlx",))[0]
    _make_hf_repo(tmp_path, managed_repo_id, "a" * 40, b"managed-weights")
    _make_hf_repo(tmp_path, "someone/unrelated", "b" * 40, b"unrelated-weights")

    deleted_groups, freed_bytes = setup_route._delete_runtime_models(
        cache_scanner=lambda: scan_cache_dir(cache_dir=tmp_path),
    )

    assert deleted_groups == ["tts-mlx"]
    assert freed_bytes > 0
    managed_dir = tmp_path / f"models--{managed_repo_id.replace('/', '--')}"
    assert not (managed_dir / "snapshots" / ("a" * 40)).exists()
    assert (tmp_path / "models--someone--unrelated" / "snapshots" / ("b" * 40)).exists()


def test_delete_runtime_models_handles_empty_temp_cache(tmp_path: Path) -> None:
    from modules.web.routes import setup as setup_route

    # Real scan against an isolated temp cache dir; never the user's real cache.
    empty_cache = tmp_path / "hf-empty"
    empty_cache.mkdir()
    deleted_groups, freed_bytes = setup_route._delete_runtime_models(
        cache_scanner=lambda: scan_cache_dir(cache_dir=empty_cache),
    )
    assert deleted_groups == []
    assert freed_bytes == 0


def test_delete_runtime_endpoint_removes_state_and_reports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from modules.web.routes import setup as setup_route
    import scripts.setup_runtime as setup_runtime

    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    state_path = _state_path(tmp_path)
    setup_runtime.write_setup_state(
        state_path,
        profile_id="base",
        model_groups=(),
        accepted_risks=(),
    )
    monkeypatch.setattr(
        setup_route,
        "_delete_runtime_models",
        lambda **kwargs: (["tts-mlx"], 12345),
    )

    monkeypatch.setattr(setup_route, "get_managed_models", lambda: {"models": []})
    with _client() as client:
        response = client.request("DELETE", "/api/setup/runtime")

    assert response.status_code == 200
    assert response.json() == {
        "deleted_model_groups": ["tts-mlx"],
        "freed_bytes": 12345,
        "state_deleted": True,
    }
    assert not state_path.exists()


def test_delete_runtime_endpoint_reports_absent_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from modules.web.routes import setup as setup_route

    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        setup_route,
        "_delete_runtime_models",
        lambda **kwargs: ([], 0),
    )

    with _client() as client:
        response = client.request("DELETE", "/api/setup/runtime")

    assert response.status_code == 200
    assert response.json()["state_deleted"] is False


def test_delete_runtime_endpoint_conflicts_when_setup_in_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from modules.web.routes import setup as setup_route

    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))

    class _RaisingLock:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            raise setup_route.setup_runtime.SetupExecutionError(
                "Another runtime setup operation is already running."
            )

        def __exit__(self, *args):
            return False

    def _must_not_delete(**kwargs):
        raise AssertionError("must not delete while setup is in progress")

    monkeypatch.setattr(setup_route.setup_runtime, "SetupLock", _RaisingLock)
    monkeypatch.setattr(setup_route, "_delete_runtime_models", _must_not_delete)

    with _client() as client:
        response = client.request("DELETE", "/api/setup/runtime")

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "setup_in_progress"


# --------------------------------------------------------------------------- #
# Backend model downloads (packaged builds)                                    #
# --------------------------------------------------------------------------- #


def _wait_for_terminal(service, timeout_seconds: float = 5.0) -> dict:
    import time

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        snapshot = service.status()
        if snapshot["lifecycle"] in {"complete", "failed"}:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"download never finished: {service.status()}")


class _RecordingLock:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def __enter__(self):
        self._events.append("lock")
        return self

    def __exit__(self, *args):
        self._events.append("unlock")
        return False


def test_model_download_service_downloads_groups_in_order_under_lock() -> None:
    from modules.app.services.model_download_service import ModelDownloadService

    events: list[str] = []
    downloaded: list[str] = []
    catalog = {"tts-mlx": ("kokoro-mlx",), "cleaner-mlx": ("qwen-a", "qwen-b")}
    service = ModelDownloadService(
        downloader=lambda model_id: downloaded.append(model_id),
        group_resolver=lambda groups: catalog[next(iter(groups))],
        lock_factory=lambda: _RecordingLock(events),
        on_terminal=lambda: events.append("terminal"),
    )

    snapshot = service.start(profile_id="mlx", model_groups=["tts-mlx", "cleaner-mlx"])

    # The worker may already have finished for an instant downloader; the
    # start snapshot only guarantees the run was accepted and sized.
    assert snapshot["lifecycle"] in {"running", "complete"}
    assert snapshot["total_models"] == 3
    final = _wait_for_terminal(service)
    assert final["lifecycle"] == "complete"
    assert final["completed_models"] == 3
    assert final["error"] is None
    assert downloaded == ["kokoro-mlx", "qwen-a", "qwen-b"]
    assert events == ["lock", "unlock", "terminal"]


def test_model_download_service_rejects_concurrent_starts() -> None:
    import threading

    from modules.app.services.model_download_service import (
        ModelDownloadBusyError,
        ModelDownloadService,
    )

    release = threading.Event()
    started = threading.Event()

    def _blocking_download(model_id: str) -> None:
        del model_id
        started.set()
        assert release.wait(timeout=5.0)

    service = ModelDownloadService(
        downloader=_blocking_download,
        group_resolver=lambda groups: ("model",),
        lock_factory=lambda: _RecordingLock([]),
    )
    service.start(profile_id="mlx", model_groups=["tts-mlx"])
    assert started.wait(timeout=5.0)
    with pytest.raises(ModelDownloadBusyError):
        service.start(profile_id="mlx", model_groups=["tts-mlx"])
    release.set()
    assert _wait_for_terminal(service)["lifecycle"] == "complete"


def test_model_download_service_reports_sanitized_failure_and_releases_lock() -> None:
    from modules.app.services.model_download_service import ModelDownloadService

    events: list[str] = []

    def _broken_download(model_id: str) -> None:
        raise RuntimeError("boom\x00" + "x" * 500)

    service = ModelDownloadService(
        downloader=_broken_download,
        group_resolver=lambda groups: ("model",),
        lock_factory=lambda: _RecordingLock(events),
        on_terminal=lambda: events.append("terminal"),
    )
    service.start(profile_id="mlx", model_groups=["tts-mlx"])

    final = _wait_for_terminal(service)
    assert final["lifecycle"] == "failed"
    assert final["error"].startswith("Downloading model failed: boom")
    assert "\x00" not in final["error"]
    assert len(final["error"]) <= 260
    assert events == ["lock", "unlock", "terminal"]
    # A failed run must not leave the service stuck busy.
    retry = service.start(profile_id="mlx", model_groups=["tts-mlx"])
    assert retry["lifecycle"] in {"running", "failed"}
    _wait_for_terminal(service)


def test_model_download_service_releases_lock_when_worker_exits_abruptly() -> None:
    from modules.app.services.model_download_service import ModelDownloadService

    events: list[str] = []

    def _exit_download(model_id: str) -> None:
        del model_id
        raise SystemExit("worker exited")

    service = ModelDownloadService(
        downloader=_exit_download,
        group_resolver=lambda groups: ("model",),
        lock_factory=lambda: _RecordingLock(events),
    )
    service.start(profile_id="mlx", model_groups=["tts-mlx"])

    final = _wait_for_terminal(service)
    assert final["lifecycle"] == "failed"
    assert "worker exited" in final["error"]
    assert events == ["lock", "unlock"]


@pytest.mark.parametrize(
    ("raiser", "expected_fragment"),
    [
        (
            lambda: (_ for _ in ()).throw(__import__("requests").ConnectionError("dns")),
            "Check your internet connection",
        ),
        (
            lambda: (_ for _ in ()).throw(
                OSError(__import__("errno").ENOSPC, "No space left on device")
            ),
            "Not enough disk space",
        ),
    ],
)
def test_model_download_service_maps_common_failures_to_actionable_errors(
    raiser, expected_fragment
) -> None:
    from modules.app.services.model_download_service import ModelDownloadService

    def _broken_download(model_id: str) -> None:
        next(raiser())

    service = ModelDownloadService(
        downloader=_broken_download,
        group_resolver=lambda groups: ("mlx-community/Kokoro-82M-bf16",),
        lock_factory=lambda: _RecordingLock([]),
    )
    service.start(profile_id="mlx", model_groups=["tts-mlx"])

    final = _wait_for_terminal(service)
    assert final["lifecycle"] == "failed"
    assert expected_fragment in final["error"]


def test_model_download_service_names_missing_repositories() -> None:
    from huggingface_hub.errors import RepositoryNotFoundError

    from modules.app.services.model_download_service import ModelDownloadService

    def _broken_download(model_id: str) -> None:
        import requests

        response = requests.Response()
        response.status_code = 401
        raise RepositoryNotFoundError("401 Client Error", response=response)

    service = ModelDownloadService(
        downloader=_broken_download,
        group_resolver=lambda groups: ("mlx-community/Kokoro-82M-bf16",),
        lock_factory=lambda: _RecordingLock([]),
    )
    service.start(profile_id="mlx", model_groups=["tts-mlx"])

    final = _wait_for_terminal(service)
    assert final["lifecycle"] == "failed"
    assert "mlx-community/Kokoro-82M-bf16 is not available" in final["error"]


def test_model_download_service_surfaces_setup_lock_contention() -> None:
    from scripts.setup_runtime import SetupExecutionError

    from modules.app.services.model_download_service import ModelDownloadService

    class _HeldLock:
        def __enter__(self):
            raise SetupExecutionError("Another runtime setup operation is already running.")

        def __exit__(self, *args):
            return False

    service = ModelDownloadService(
        downloader=lambda model_id: None,
        group_resolver=lambda groups: ("model",),
        lock_factory=_HeldLock,
    )
    with pytest.raises(SetupExecutionError):
        service.start(profile_id="mlx", model_groups=["tts-mlx"])
    assert service.status()["lifecycle"] == "idle"


def test_model_download_service_releases_lock_when_worker_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.app.services import model_download_service as service_module

    events: list[str] = []

    class _BrokenThread:
        def __init__(self, **kwargs):
            del kwargs

        def start(self):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(service_module.threading, "Thread", _BrokenThread)
    service = service_module.ModelDownloadService(
        downloader=lambda model_id: None,
        group_resolver=lambda groups: ("model",),
        lock_factory=lambda: _RecordingLock(events),
    )

    with pytest.raises(RuntimeError, match="thread unavailable"):
        service.start(profile_id="mlx", model_groups=["tts-mlx"])

    assert service.status()["lifecycle"] == "idle"
    assert events == ["lock", "unlock"]


def test_model_download_endpoints_validate_start_and_report_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.web.routes import setup as setup_route
    from modules.app.services.model_download_service import ModelDownloadBusyError

    _all_profiles_compatible(monkeypatch)

    class _FakeService:
        def __init__(self, **kwargs):
            del kwargs
            self.calls: list[tuple[str, list[str]]] = []
            self.busy = False

        def status(self):
            return {
                "lifecycle": "idle",
                "profile": None,
                "model_groups": [],
                "total_models": 0,
                "completed_models": 0,
                "current_model_group": None,
                "error": None,
            }

        def start(self, *, profile_id, model_groups, configure_runtime=True, cleaner_cuda_quantization="4bit"):
            if self.busy:
                raise ModelDownloadBusyError("busy")
            self.calls.append((profile_id, list(model_groups)))
            return dict(self.status(), lifecycle="running", profile=profile_id)

        def request_cancel(self):
            return dict(self.status(), lifecycle="stopping")

    monkeypatch.setattr(setup_route, "ModelDownloadService", _FakeService)

    with _client() as client:
        idle = client.get("/api/setup/models/download")
        assert idle.status_code == 200
        assert idle.json()["lifecycle"] == "idle"

        unknown_group = client.post(
            "/api/setup/models/download",
            json={"profile": "mlx", "model_groups": ["not-a-group"]},
        )
        assert unknown_group.status_code == 422

        empty = client.post(
            "/api/setup/models/download",
            json={"profile": "mlx", "model_groups": []},
        )
        assert empty.status_code == 422

        extra_field = client.post(
            "/api/setup/models/download",
            json={"profile": "mlx", "model_groups": ["tts-mlx"], "path": "/x"},
        )
        assert extra_field.status_code == 422

        started = client.post(
            "/api/setup/models/download",
            json={"profile": "mlx", "model_groups": ["tts-mlx"]},
        )
        assert started.status_code == 200
        assert started.json()["lifecycle"] == "running"

        cancelled = client.post("/api/setup/models/download/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["lifecycle"] == "stopping"

        service = client.app.state.model_download_service
        assert service.calls == [("mlx", ["tts-mlx"])]

        service.busy = True
        busy = client.post(
            "/api/setup/models/download",
            json={"profile": "mlx", "model_groups": ["tts-mlx"]},
        )
        assert busy.status_code == 409
        assert busy.json()["detail"]["error"] == "download_in_progress"


def test_model_download_start_conflicts_when_setup_lock_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.web.routes import setup as setup_route

    _all_profiles_compatible(monkeypatch)

    class _LockedService:
        def __init__(self, **kwargs):
            del kwargs

        def start(self, **kwargs):
            raise setup_route.setup_runtime.SetupExecutionError(
                "Another runtime setup operation is already running."
            )

    monkeypatch.setattr(setup_route, "ModelDownloadService", _LockedService)

    with _client() as client:
        response = client.post(
            "/api/setup/models/download",
            json={"profile": "mlx", "model_groups": ["tts-mlx"]},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "setup_in_progress"


def test_model_download_service_shutdown_cancels_and_joins_worker() -> None:
    import threading

    from modules.app.services.model_download_service import ModelDownloadService

    release = threading.Event()
    started = threading.Event()
    events: list[str] = []

    def _cooperative_download(model_id: str) -> None:
        del model_id
        started.set()
        while not release.is_set():
            if service.cancel_requested:
                return
            release.wait(0.01)

    service = ModelDownloadService(
        downloader=_cooperative_download,
        group_resolver=lambda groups: ("model",),
        lock_factory=lambda: _RecordingLock(events),
    )
    service.start(profile_id="mlx", model_groups=["tts-mlx"])
    assert started.wait(timeout=2)
    assert service.shutdown(timeout_seconds=1.0) is True
    assert service.status()["lifecycle"] == "cancelled"
    assert events == ["lock", "unlock"]


def test_model_download_service_rejects_restart_while_old_worker_is_stopping() -> None:
    import threading

    from modules.app.services.model_download_service import (
        ModelDownloadBusyError,
        ModelDownloadService,
    )

    release = threading.Event()
    started = threading.Event()

    def _blocking_download(model_id: str) -> None:
        del model_id
        started.set()
        assert release.wait(timeout=5.0)

    service = ModelDownloadService(
        downloader=_blocking_download,
        group_resolver=lambda groups: ("model",),
        lock_factory=lambda: _RecordingLock([]),
    )
    service.start(profile_id="mlx", model_groups=["tts-mlx"])
    assert started.wait(timeout=2.0)
    assert service.shutdown(timeout_seconds=0.0) is False
    assert service.status()["lifecycle"] == "stopping"

    with pytest.raises(ModelDownloadBusyError):
        service.start(profile_id="mlx", model_groups=["tts-mlx"])

    release.set()
    deadline = __import__("time").monotonic() + 2.0
    while service.status()["lifecycle"] == "stopping":
        assert __import__("time").monotonic() < deadline
        __import__("time").sleep(0.01)
    assert service.status()["lifecycle"] == "cancelled"


def test_delete_runtime_rejects_active_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from modules.web.routes import setup as setup_route

    class _Controller:
        _conversion_service = SimpleNamespace(
            begin_storage_cleanup=lambda: (_ for _ in ()).throw(
                RuntimeError("Cannot clean up storage while a conversion is queued or running")
            )
        )

    monkeypatch.setattr(setup_route, "_delete_runtime_models", lambda: ([], 0))
    client = TestClient(create_app(controller=_Controller()))
    response = client.request("DELETE", "/api/setup/runtime")
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "conversion_in_progress"


def test_adopt_rejects_non_base_profile_with_no_model_groups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A TTS profile with nothing selected cannot be 'ready' -- no model exists.

    Every readiness check folds over the selected groups, so an empty selection
    made each one vacuously true and onboarding hid itself with no TTS runtime.
    """
    from modules.web.routes import setup as setup_route

    _all_profiles_compatible(monkeypatch)
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        setup_route,
        "_check_selected_profile_packages",
        lambda profile_id: {"ready": True, "missing": [], "incompatible": []},
    )
    monkeypatch.setattr(setup_route.setup_runtime, "_probe_profile", _probe(True))

    with _client() as client:
        response = client.post(
            "/api/setup/adopt",
            json={"profile": "mlx", "model_groups": []},
        )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_request"
    assert not _state_path(tmp_path).exists()


def test_setup_status_is_not_ready_when_no_model_group_is_selected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Readiness must be judged against the profile's TTS groups, not the selection."""
    from modules.web.routes import setup as setup_route

    _all_profiles_compatible(monkeypatch)
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        setup_route,
        "_check_selected_profile_packages",
        lambda profile_id: {"ready": True, "missing": [], "incompatible": []},
    )
    monkeypatch.setattr(setup_route.setup_runtime, "_probe_profile", _probe(True))

    state_path = _state_path(tmp_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"profile": "mlx", "model_groups": [], "restart_required": False}),
        encoding="utf-8",
    )

    with _client() as client:
        response = client.get("/api/setup/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is False
    assert payload["state"] != "ready"
