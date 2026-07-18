from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest
from fastapi.testclient import TestClient

from modules.web.server import create_app


TOKEN = "setup-route-test-token"


def _client(*, desktop_mode: bool = False) -> TestClient:
    return TestClient(
        create_app(
            controller=object(),
            desktop_mode=desktop_mode,
            desktop_api_token=TOKEN if desktop_mode else None,
        )
    )


def _contains_forbidden_key(value: object) -> bool:
    forbidden = {
        "api_key",
        "authorization",
        "command",
        "commands",
        "desktop_api_token",
        "diagnostics",
        "environ",
        "environment",
        "executable",
        "lock_owner",
        "path",
        "pid",
        "token",
    }
    if isinstance(value, dict):
        return any(
            str(key).lower() in forbidden or _contains_forbidden_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def test_setup_status_returns_allowlisted_readiness_contract(
    monkeypatch,
) -> None:
    from modules.web.routes import setup as setup_route

    monkeypatch.setattr(
        setup_route.setup_runtime,
        "get_setup_status",
        lambda **kwargs: {
            "configured": True,
            "profile": "mlx",
            "model_groups": ["tts-mlx", "cleaner-mlx"],
            "ready": False,
            "restart_required": True,
            "probe": {
                "available": False,
                "checks": [
                    {"id": "tts-mlx", "available": True, "error": None},
                    {
                        "id": "cleaner-mlx",
                        "available": False,
                        "error": "/Users/name/private child diagnostic",
                    },
                ],
                "diagnostics": {"environment": {"OPENAI_API_KEY": "secret"}},
            },
            "profiles": [
                {
                    "id": "base",
                    "compatible": True,
                    "reason": None,
                    "model_groups": [],
                    "risk": None,
                },
                {
                    "id": "mlx",
                    "compatible": True,
                    "reason": None,
                    "model_groups": ["tts-mlx", "cleaner-mlx"],
                    "risk": None,
                },
                {
                    "id": "cuda",
                    "compatible": False,
                    "reason": "CUDA requires Linux or Windows.",
                    "model_groups": ["tts-cpu-cuda", "cleaner-cuda"],
                    "risk": {"id": "forged-risk", "summary": "forged summary"},
                },
            ],
            "risk": None,
            "state_error": None,
            "commands": [["python", "-m", "pip"]],
            "desktop_api_token": "secret",
            "environment": {"OPENAI_API_KEY": "secret"},
            "lock_owner": {"pid": 123},
        },
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
            "tts-mlx": {"ready": True, "reason": None},
            "cleaner-mlx": {"ready": False, "reason": "incomplete"},
        },
    )

    with _client() as client:
        response = client.get("/api/setup/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == 1
    assert payload["selected_profile"] == "mlx"
    assert payload["packages"] == {"ready": True, "missing": [], "incompatible": []}
    assert payload["probe"] == {
        "ready": False,
        "checks": [
            {"id": "tts-mlx", "ready": True},
            {"id": "cleaner-mlx", "ready": False},
        ],
    }
    assert payload["selected_model_groups"] == [
        {
            "id": "tts-mlx",
            "assets_ready": True,
            "assets_reason": None,
            "runtime_ready": True,
            "ready": True,
        },
        {
            "id": "cleaner-mlx",
            "assets_ready": False,
            "assets_reason": "incomplete",
            "runtime_ready": False,
            "ready": False,
        },
    ]
    assert payload["restart_required"] is True
    assert payload["state"] == "restart_required"
    assert payload["backend"]["status"] == "running"
    assert len(payload["backend"]["instance_id"]) == 32
    assert {profile["id"] for profile in payload["profiles"]} == {"base", "mlx", "cuda"}
    cuda = next(profile for profile in payload["profiles"] if profile["id"] == "cuda")
    assert cuda["compatible"] is False
    assert isinstance(cuda["reason"], str) and cuda["reason"]
    assert cuda["risk"] == {
        "id": setup_route.setup_runtime.PYTORCH_RISK_ID,
        "summary": setup_route.setup_runtime.PYTORCH_RISK_SUMMARY,
        "accepted": False,
    }
    assert _contains_forbidden_key(payload) is False


def test_setup_status_recovers_from_incomplete_or_corrupt_state(monkeypatch) -> None:
    from modules.web.routes import setup as setup_route

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
            "state_error": "Setup state is invalid: arbitrary local detail",
        },
    )

    with _client() as client:
        response = client.get("/api/setup/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["recommended_profile"] in {"base", "mlx", "cuda"}
    assert payload["installed_runtime"] is None
    assert payload | {"profiles": []} == {
        "schema_version": 1,
        "configured": False,
        "state": "error",
        "state_error": "invalid",
        "selected_profile": None,
        "selected_model_groups": [],
        "packages": {"ready": False, "missing": [], "incompatible": []},
        "probe": {"ready": False, "checks": []},
        "ready": False,
        "restart_required": False,
        "recommended_profile": payload["recommended_profile"],
        "installed_runtime": None,
        "profiles": [],
        "risk": None,
        "backend": {
            "instance_id": payload["backend"]["instance_id"],
            "status": "running",
        },
    }
    assert {profile["id"] for profile in payload["profiles"]} == {"base", "mlx", "cuda"}


def test_setup_status_preserves_missing_invalid_and_unavailable_state_errors(
    monkeypatch,
) -> None:
    from modules.web.routes import setup as setup_route

    base_status = {
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
    with _client() as client:
        fingerprints = iter((("state", 1), ("state", 2), ("state", 3)))
        monkeypatch.setattr(setup_route, "_setup_state_fingerprint", lambda: next(fingerprints))
        monkeypatch.setattr(
            setup_route.setup_runtime,
            "get_setup_status",
            lambda **kwargs: base_status,
        )
        missing = client.get("/api/setup/status").json()

        monkeypatch.setattr(
            setup_route.setup_runtime,
            "get_setup_status",
            lambda **kwargs: {
                **base_status,
                "state_error": "Setup state is invalid: bad schema",
            },
        )
        invalid = client.get("/api/setup/status").json()

        def unavailable(**kwargs):
            del kwargs
            raise OSError("/private/path cannot be read")

        monkeypatch.setattr(setup_route.setup_runtime, "get_setup_status", unavailable)
        unavailable_payload = client.get("/api/setup/status").json()

    assert (missing["state"], missing["state_error"]) == ("unconfigured", None)
    assert (invalid["state"], invalid["state_error"]) == ("error", "invalid")
    assert (unavailable_payload["state"], unavailable_payload["state_error"]) == (
        "error",
        "unavailable",
    )


def test_package_readiness_checks_recursive_base_and_missing_dependency(tmp_path: Path) -> None:
    from modules.web.routes import setup as setup_route

    (tmp_path / "base.txt").write_text("shared-package==1.0\n", encoding="utf-8")
    profile = tmp_path / "mlx.txt"
    profile.write_text("-r base.txt\nprofile-package==2.0\n", encoding="utf-8")

    status = setup_route._check_manifest_packages(
        profile,
        version_resolver=lambda name: {"profile-package": "2.0"}[name],
    )

    assert status == {
        "ready": False,
        "missing": [{"name": "shared-package", "expected": "==1.0", "installed": None}],
        "incompatible": [],
    }


def test_package_readiness_degrades_when_manifest_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Packaged sidecars without bundled manifests must report not-ready, not raise."""
    from modules.web.routes import setup as setup_route

    monkeypatch.setattr(
        setup_route,
        "get_runtime_profile",
        lambda profile_id: SimpleNamespace(
            requirements_file=tmp_path / "absent" / f"{profile_id}.txt"
        ),
    )

    status = setup_route._check_selected_profile_packages("mlx")

    assert status == {"ready": False, "missing": [], "incompatible": []}


def test_package_readiness_reports_wrong_pinned_version(tmp_path: Path) -> None:
    from modules.web.routes import setup as setup_route

    manifest = tmp_path / "base.txt"
    manifest.write_text("shared-package==1.0\n", encoding="utf-8")

    status = setup_route._check_manifest_packages(
        manifest,
        version_resolver=lambda name: "2.0",
    )

    assert status == {
        "ready": False,
        "missing": [],
        "incompatible": [
            {"name": "shared-package", "expected": "==1.0", "installed": "2.0"}
        ],
    }


def test_package_readiness_excludes_false_markers(tmp_path: Path) -> None:
    from modules.web.routes import setup as setup_route

    manifest = tmp_path / "base.txt"
    manifest.write_text(
        'never-installed==9.0; python_version < "2"\npackaging==26.0\n',
        encoding="utf-8",
    )

    status = setup_route._check_manifest_packages(
        manifest,
        version_resolver=lambda name: "26.0",
    )

    assert status == {"ready": True, "missing": [], "incompatible": []}


def test_package_readiness_accepts_complete_recursive_manifest_and_named_wheel(
    tmp_path: Path,
) -> None:
    from modules.web.routes import setup as setup_route

    (tmp_path / "base.txt").write_text(
        "shared-package>=1,<2\n"
        "en-core-web-sm @ https://example.invalid/en_core_web_sm-3.8.0-py3-none-any.whl\n",
        encoding="utf-8",
    )
    profile = tmp_path / "mlx.txt"
    profile.write_text("-r base.txt\nprofile-package==2.0\n", encoding="utf-8")
    installed = {
        "shared-package": "1.5",
        "en-core-web-sm": "3.8.0",
        "profile-package": "2.0",
    }

    status = setup_route._check_manifest_packages(
        profile,
        version_resolver=installed.__getitem__,
    )

    assert status == {"ready": True, "missing": [], "incompatible": []}


def _cache_repo(
    repo_id: str,
    file_paths: list[Path],
    *,
    snapshot_path: Path | None = None,
    commit_hash: str = "a" * 40,
) -> SimpleNamespace:
    files = [SimpleNamespace(file_path=path) for path in file_paths]
    revision = SimpleNamespace(
        files=files,
        snapshot_path=snapshot_path or file_paths[0].parent,
        commit_hash=commit_hash,
    )
    return SimpleNamespace(repo_id=repo_id, repo_type="model", revisions=[revision])


def test_model_assets_require_complete_existing_cached_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from modules.web.routes import setup as setup_route

    existing = tmp_path / "model.safetensors"
    existing.write_bytes(b"weights")
    deleted = tmp_path / "deleted.json"
    monkeypatch.setattr(
        setup_route,
        "resolve_model_groups",
        lambda groups: ("org/complete", "org/deleted", "org/partial", "org/missing"),
    )
    cache = SimpleNamespace(
        repos=[
                _cache_repo("org/complete", [existing]),
                _cache_repo("org/deleted", [deleted]),
                SimpleNamespace(
                    repo_id="org/partial",
                    repo_type="model",
                    revisions=[SimpleNamespace(files=[])],
                ),
            ],
        warnings=(),
    )

    status = setup_route._check_model_group_assets(
        ("group",),
        cache_scanner=lambda: cache,
    )

    assert status == {"group": {"ready": False, "reason": "missing"}}


def test_model_assets_treat_deleted_cached_file_as_incomplete(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from modules.web.routes import setup as setup_route

    deleted = tmp_path / "deleted.safetensors"
    monkeypatch.setattr(setup_route, "resolve_model_groups", lambda groups: ("org/model",))
    cache = SimpleNamespace(
        repos=[_cache_repo("org/model", [deleted])],
        warnings=(),
    )

    assert setup_route._check_model_group_assets(
        ("group",),
        cache_scanner=lambda: cache,
    ) == {"group": {"ready": False, "reason": "incomplete"}}


def test_model_assets_reject_config_only_revision_without_receipt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from modules.web.routes import setup as setup_route

    first = tmp_path / "config.json"
    second = tmp_path / "model.safetensors"
    first.write_text("{}", encoding="utf-8")
    second.write_bytes(b"weights")
    monkeypatch.setattr(
        setup_route,
        "resolve_model_groups",
        lambda groups: ("org/model",),
    )
    cache = SimpleNamespace(
        repos=[_cache_repo("org/model", [first, second])],
        warnings=(),
    )

    assert setup_route._check_model_group_assets(
        ("group",),
        cache_scanner=lambda: cache,
    ) == {"group": {"ready": False, "reason": "incomplete"}}
    assert setup_route._check_model_group_assets((), cache_scanner=lambda: cache) == {}


def test_model_assets_accept_revision_with_valid_matching_receipt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from modules.web.routes import setup as setup_route
    from scripts.download_models import write_download_receipt

    model_id = "org/model"
    revision = "e" * 40
    snapshot = tmp_path / "models--org--model" / "snapshots" / revision
    snapshot.mkdir(parents=True)
    config = snapshot / "config.json"
    weights = snapshot / "model.safetensors"
    config.write_text("{}", encoding="utf-8")
    weights.write_bytes(b"weights")
    write_download_receipt(snapshot, model_id=model_id, revision=revision)
    repo = _cache_repo(
        model_id,
        [config, weights],
        snapshot_path=snapshot,
        commit_hash=revision,
    )
    monkeypatch.setattr(setup_route, "resolve_model_groups", lambda groups: (model_id,))
    cache = SimpleNamespace(
        repos=[repo],
        warnings=(RuntimeError("unrelated dataset cache warning"),),
    )

    assert setup_route._check_model_group_assets(
        ("group",),
        cache_scanner=lambda: cache,
    ) == {"group": {"ready": True, "reason": None}}


def test_model_assets_accept_any_valid_matching_cached_revision(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from modules.web.routes import setup as setup_route
    from scripts.download_models import write_download_receipt

    model_id = "org/model"
    repo_root = tmp_path / "models--org--model" / "snapshots"
    incomplete_revision = "1" * 40
    complete_revision = "2" * 40
    incomplete = repo_root / incomplete_revision
    complete = repo_root / complete_revision
    incomplete.mkdir(parents=True)
    complete.mkdir(parents=True)
    incomplete_file = incomplete / "config.json"
    complete_file = complete / "model.bin"
    incomplete_file.write_text("{}", encoding="utf-8")
    complete_file.write_bytes(b"model")
    write_download_receipt(
        complete,
        model_id=model_id,
        revision=complete_revision,
    )
    invalid_repo = _cache_repo(
        model_id,
        [incomplete_file],
        snapshot_path=incomplete,
        commit_hash=incomplete_revision,
    )
    valid_repo = _cache_repo(
        model_id,
        [complete_file],
        snapshot_path=complete,
        commit_hash=complete_revision,
    )
    repo = SimpleNamespace(
        repo_id=model_id,
        repo_type="model",
        revisions=[*invalid_repo.revisions, *valid_repo.revisions],
    )
    monkeypatch.setattr(setup_route, "resolve_model_groups", lambda groups: (model_id,))

    assert setup_route._check_model_group_assets(
        ("group",),
        cache_scanner=lambda: SimpleNamespace(repos=[repo], warnings=()),
    ) == {"group": {"ready": True, "reason": None}}


@pytest.mark.parametrize(
    ("repos", "expected_reason"),
    [
        ([], "missing"),
        (
            [
                SimpleNamespace(
                    repo_id="org/model",
                    repo_type="model",
                    revisions=[SimpleNamespace(files=[])],
                )
            ],
            "incomplete",
        ),
    ],
)
def test_model_assets_distinguish_missing_and_partial_cache(
    monkeypatch,
    repos: list[SimpleNamespace],
    expected_reason: str,
) -> None:
    from modules.web.routes import setup as setup_route

    monkeypatch.setattr(setup_route, "resolve_model_groups", lambda groups: ("org/model",))
    cache = SimpleNamespace(repos=repos, warnings=())

    assert setup_route._check_model_group_assets(
        ("group",),
        cache_scanner=lambda: cache,
    ) == {"group": {"ready": False, "reason": expected_reason}}


def test_model_assets_treat_corrupt_or_inaccessible_cache_as_unavailable(monkeypatch) -> None:
    from modules.web.routes import setup as setup_route

    monkeypatch.setattr(setup_route, "resolve_model_groups", lambda groups: ("org/model",))

    def inaccessible():
        raise PermissionError("/private/cache")

    assert setup_route._check_model_group_assets(
        ("group",),
        cache_scanner=inaccessible,
    ) == {"group": {"ready": False, "reason": "unavailable"}}


def test_model_assets_treat_absent_cache_as_missing(monkeypatch, tmp_path: Path) -> None:
    from huggingface_hub.errors import CacheNotFound
    from modules.web.routes import setup as setup_route

    monkeypatch.setattr(setup_route, "resolve_model_groups", lambda groups: ("org/model",))

    def absent():
        raise CacheNotFound("cache does not exist", cache_dir=tmp_path)

    assert setup_route._check_model_group_assets(
        ("group",),
        cache_scanner=absent,
    ) == {"group": {"ready": False, "reason": "missing"}}


def test_desktop_setup_status_requires_existing_bearer_auth() -> None:
    with _client(desktop_mode=True) as client:
        assert client.get("/api/setup/status").status_code == 401
        response = client.get(
            "/api/setup/status",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 200


def test_status_cache_reuses_probe_and_asset_refresh_within_ttl(monkeypatch) -> None:
    from modules.web.routes import setup as setup_route

    calls = {"status": 0, "packages": 0, "assets": 0}
    services: list[object] = []

    def status(*, probe_service=None):
        calls["status"] += 1
        services.append(probe_service)
        return {
            "configured": True,
            "profile": "mlx",
            "model_groups": ["tts-mlx"],
            "ready": True,
            "restart_required": False,
            "probe": {
                "available": True,
                "checks": [{"id": "tts-mlx", "available": True}],
            },
            "state_error": None,
        }

    def packages(profile_id):
        calls["packages"] += 1
        return {"ready": True, "missing": [], "incompatible": []}

    def assets(group_ids, **kwargs):
        del kwargs
        calls["assets"] += 1
        return {"tts-mlx": {"ready": True, "reason": None}}

    monkeypatch.setattr(setup_route.setup_runtime, "get_setup_status", status)
    monkeypatch.setattr(setup_route, "_check_selected_profile_packages", packages)
    monkeypatch.setattr(setup_route, "_check_model_group_assets", assets)
    monkeypatch.setattr(setup_route, "_setup_state_fingerprint", lambda: ("state", 1))

    with _client() as client:
        first = client.get("/api/setup/status")
        second = client.get("/api/setup/status")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert calls == {"status": 1, "packages": 1, "assets": 1}
    assert len(services) == 1 and services[0] is not None


def test_status_cache_is_single_flight_for_concurrent_refreshes() -> None:
    from modules.web.routes.setup import RuntimeSetupStatusCache

    cache = RuntimeSetupStatusCache(ttl_seconds=4.0)
    calls = 0
    calls_lock = threading.Lock()

    def build(service):
        nonlocal calls
        del service
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return {"ready": True}

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: cache.get(("state", 1), build), range(8)))

    assert calls == 1
    assert results == [{"ready": True}] * 8


def test_status_cache_invalidates_on_state_change_and_ttl_expiry() -> None:
    from modules.web.routes.setup import RuntimeSetupStatusCache

    now = [0.0]
    cache = RuntimeSetupStatusCache(ttl_seconds=4.0, clock=lambda: now[0])
    calls = 0

    def build(service):
        nonlocal calls
        del service
        calls += 1
        return {"generation": calls}

    assert cache.get(("state", 1, "base", ()), build) == {"generation": 1}
    assert cache.get(("state", 1, "base", ()), build) == {"generation": 1}
    assert cache.get(("state", 2, "mlx", ("tts-mlx",)), build) == {"generation": 2}
    now[0] = 5.0
    assert cache.get(("state", 2, "mlx", ("tts-mlx",)), build) == {"generation": 3}


def test_status_cache_is_app_scoped_without_global_stale_state() -> None:
    from modules.web.routes.setup import RuntimeSetupStatusCache

    first = RuntimeSetupStatusCache(ttl_seconds=4.0)
    second = RuntimeSetupStatusCache(ttl_seconds=4.0)
    calls = 0

    def build(service):
        nonlocal calls
        del service
        calls += 1
        return {"generation": calls}

    assert first.get(("state",), build) == {"generation": 1}
    assert second.get(("state",), build) == {"generation": 2}


def test_outer_ttl_refresh_reuses_app_scoped_integrity_hash(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from modules.web.routes.setup import RuntimeSetupStatusCache
    from scripts import download_models

    model_id = "owner/model"
    revision = "b" * 40
    snapshot = tmp_path / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"model")
    download_models.write_download_receipt(
        snapshot,
        model_id=model_id,
        revision=revision,
    )
    real_hash = download_models._file_size_and_sha256
    hash_calls = 0

    def counted_hash(path: Path) -> tuple[int, str]:
        nonlocal hash_calls
        hash_calls += 1
        return real_hash(path)

    monkeypatch.setattr(download_models, "_file_size_and_sha256", counted_hash)
    now = [0.0]
    cache = RuntimeSetupStatusCache(ttl_seconds=0.1, clock=lambda: now[0])

    def build(service):
        del service
        return {
            "ready": cache.receipt_verifier.verify(
                snapshot,
                model_id=model_id,
                revision=revision,
            )
        }

    assert cache.get(("state",), build) == {"ready": True}
    now[0] = 1.0
    assert cache.get(("state",), build) == {"ready": True}
    assert hash_calls == 1
