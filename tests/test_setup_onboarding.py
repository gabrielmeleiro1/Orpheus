"""Capability-driven onboarding: installed-runtime detection, adopt, uninstall."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from huggingface_hub import scan_cache_dir

from modules.web.server import create_app


def _client() -> TestClient:
    return TestClient(create_app(controller=object(), desktop_mode=False))


def _all_profiles_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.runtime_profiles import RuntimeProfile

    monkeypatch.setattr(RuntimeProfile, "is_compatible", lambda self, **kwargs: True)


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
    monkeypatch.setattr(setup_route, "_detect_installed_runtime", lambda *a, **k: None)

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
        "model_groups": [
            {"id": "tts-mlx", "ready": True},
            {"id": "cleaner-mlx", "ready": True},
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


def test_installed_runtime_null_when_state_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.web.routes import setup as setup_route

    monkeypatch.setattr(
        setup_route.setup_runtime,
        "get_setup_status",
        lambda **kwargs: {
            "configured": True,
            "profile": "base",
            "model_groups": [],
            "ready": True,
            "restart_required": False,
            "probe": {"available": True, "checks": []},
            "state_error": None,
        },
    )
    monkeypatch.setattr(
        setup_route,
        "_check_selected_profile_packages",
        lambda profile_id: {"ready": True, "missing": [], "incompatible": []},
    )

    def _detector_must_not_run(*args, **kwargs):
        raise AssertionError("installed_runtime detection must not run when configured")

    monkeypatch.setattr(setup_route, "_detect_installed_runtime", _detector_must_not_run)

    with _client() as client:
        payload = client.get("/api/setup/status").json()

    assert payload["configured"] is True
    assert payload["installed_runtime"] is None
    assert payload["recommended_profile"] in {"base", "mlx", "cuda"}


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


def test_adopt_cuda_requires_pytorch_risk_ack(
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

    with _client() as client:
        response = client.post(
            "/api/setup/adopt",
            json={
                "profile": "cuda",
                "model_groups": ["tts-cpu-cuda"],
                "accept_pytorch_risk": False,
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "pytorch_risk_required"
    assert not _state_path(tmp_path).exists()


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
