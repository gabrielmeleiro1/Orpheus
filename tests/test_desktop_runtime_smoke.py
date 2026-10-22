"""Tests for the desktop runtime smoke harness."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import desktop_runtime_smoke


def test_direct_smoke_script_execution_adds_repository_root_to_sys_path(tmp_path: Path) -> None:
    script_path = Path(desktop_runtime_smoke.__file__).resolve()
    probe = "\n".join(
        [
            "import importlib.util",
            "import sys",
            "from pathlib import Path",
            "path = Path(sys.argv[1])",
            "spec = importlib.util.spec_from_file_location('smoke_probe', path)",
            "module = importlib.util.module_from_spec(spec)",
            "sys.modules[spec.name] = module",
            "spec.loader.exec_module(module)",
            "assert str(module.REPO_ROOT) in sys.path",
        ]
    )
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}

    completed = subprocess.run(
        [sys.executable, "-c", probe, str(script_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def _write_executable(path: Path, content: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_parse_args_accepts_opt_in_ffmpeg_runtime_check() -> None:
    args = desktop_runtime_smoke.parse_args(["--check-ffmpeg-runtime"])

    assert args.check_ffmpeg_runtime is True


def test_parse_args_accepts_packaged_app_bundle() -> None:
    args = desktop_runtime_smoke.parse_args(["--backend", "sidecar", "--app-bundle", "/tmp/Orpheus.app"])

    assert args.app_bundle == Path("/tmp/Orpheus.app")


def test_packaged_app_bundle_requires_sidecar_backend() -> None:
    args = desktop_runtime_smoke.parse_args(["--app-bundle", "/tmp/Orpheus.app"])

    with pytest.raises(ValueError, match="sidecar"):
        desktop_runtime_smoke.validate_args(args)

def test_parse_args_accepts_require_local_tts_gate() -> None:
    args = desktop_runtime_smoke.parse_args(["--require-local-tts"])

    assert args.require_local_tts is True
    assert desktop_runtime_smoke.parse_args([]).require_local_tts is False


def test_parse_args_accepts_opt_in_conversion_journey() -> None:
    args = desktop_runtime_smoke.parse_args(
        [
            "--conversion-journey",
            "--conversion-timeout-seconds",
            "90",
            "--probe-wait-seconds",
            "15",
        ]
    )

    assert args.conversion_journey is True
    assert args.conversion_timeout_seconds == 90.0
    assert args.probe_wait_seconds == 15.0


def test_backend_command_uses_python_launcher_by_default(tmp_path: Path) -> None:
    command = desktop_runtime_smoke.build_backend_command(
        backend="python",
        sidecar_path=None,
        python_executable=sys.executable,
    )

    assert command == [
        sys.executable,
        "-m",
        "apps.launchers.desktop_backend",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
    ]


def test_backend_command_requires_existing_sidecar(tmp_path: Path) -> None:
    missing_sidecar = tmp_path / "missing-sidecar"

    try:
        desktop_runtime_smoke.build_backend_command(
            backend="sidecar",
            sidecar_path=missing_sidecar,
            python_executable=sys.executable,
        )
    except FileNotFoundError as exc:
        assert str(missing_sidecar) in str(exc)
    else:
        raise AssertionError("missing sidecar path should fail")


def test_packaged_app_sidecar_resolver_uses_embedded_resource_not_staging(tmp_path: Path) -> None:
    app_bundle = tmp_path / "Orpheus.app"
    embedded = app_bundle / "Contents/Resources/binaries/orpheus-desktop-backend/orpheus-desktop-backend"
    staging = tmp_path / "staging/orpheus-desktop-backend"
    _write_executable(embedded)
    _write_executable(staging)

    assert desktop_runtime_smoke.resolve_packaged_app_sidecar(app_bundle) == embedded
    command = desktop_runtime_smoke.build_backend_command(
        backend="sidecar",
        sidecar_path=staging,
        app_bundle=app_bundle,
        python_executable=sys.executable,
    )
    assert command[0] == str(embedded)


def test_packaged_runtime_profile_resolver_reads_confined_marker(tmp_path: Path) -> None:
    app_bundle = tmp_path / "Orpheus.app"
    marker = (
        app_bundle
        / "Contents/Resources/binaries/orpheus-desktop-backend/runtime-profile.json"
    )
    marker.parent.mkdir(parents=True)
    marker.write_text('{"profile": "base"}\n', encoding="utf-8")

    assert desktop_runtime_smoke.resolve_packaged_runtime_profile(app_bundle) == "base"


def test_packaged_runtime_profile_resolver_rejects_invalid_or_escaping_marker(
    tmp_path: Path,
) -> None:
    invalid_bundle = tmp_path / "Invalid.app"
    invalid_marker = (
        invalid_bundle
        / "Contents/Resources/binaries/orpheus-desktop-backend/runtime-profile.json"
    )
    invalid_marker.parent.mkdir(parents=True)
    invalid_marker.write_text('{"profile": "development"}\n', encoding="utf-8")
    with pytest.raises(desktop_runtime_smoke.SmokeFailure, match="runtime profile"):
        desktop_runtime_smoke.resolve_packaged_runtime_profile(invalid_bundle)

    escaping_bundle = tmp_path / "EscapingProfile.app"
    escaping_parent = (
        escaping_bundle
        / "Contents/Resources/binaries/orpheus-desktop-backend"
    )
    escaping_parent.mkdir(parents=True)
    outside = tmp_path / "outside/runtime-profile.json"
    outside.parent.mkdir()
    outside.write_text('{"profile": "base"}\n', encoding="utf-8")
    escaping_parent.joinpath("runtime-profile.json").symlink_to(outside)
    with pytest.raises(ValueError, match="inside app bundle"):
        desktop_runtime_smoke.resolve_packaged_runtime_profile(escaping_bundle)


@pytest.mark.parametrize("app_path", [Path("not-an-app"), Path("Orpheus.app/Contents")])
def test_packaged_app_sidecar_resolver_rejects_non_app_bundle(tmp_path: Path, app_path: Path) -> None:
    with pytest.raises(ValueError, match=r"\.app"):
        desktop_runtime_smoke.resolve_packaged_app_sidecar(tmp_path / app_path)


def test_packaged_app_sidecar_resolver_rejects_missing_or_escaping_resource(tmp_path: Path) -> None:
    missing = tmp_path / "Missing.app"
    missing.mkdir()
    with pytest.raises(FileNotFoundError):
        desktop_runtime_smoke.resolve_packaged_app_sidecar(missing)

    escaping = tmp_path / "Escaping.app"
    embedded_parent = escaping / "Contents/Resources/binaries/orpheus-desktop-backend"
    embedded_parent.mkdir(parents=True)
    outside = _write_executable(tmp_path / "outside/orpheus-desktop-backend")
    embedded = embedded_parent / "orpheus-desktop-backend"
    embedded.symlink_to(outside)
    with pytest.raises(ValueError, match="inside app bundle"):
        desktop_runtime_smoke.resolve_packaged_app_sidecar(escaping)


def test_packaged_app_ffmpeg_check_uses_same_embedded_sidecar_resource(tmp_path: Path) -> None:
    app_bundle = tmp_path / "Orpheus.app"
    sidecar = _write_executable(
        app_bundle / "Contents/Resources/binaries/orpheus-desktop-backend/orpheus-desktop-backend"
    )
    ffmpeg = _write_executable(sidecar.parent / "bin/ffmpeg")
    ffprobe = _write_executable(sidecar.parent / "bin/ffprobe")

    assert desktop_runtime_smoke.resolve_ffmpeg_runtime_binaries(
        backend="sidecar",
        sidecar_path=tmp_path / "staging/orpheus-desktop-backend",
        app_bundle=app_bundle,
    ) == {"ffmpeg": ffmpeg, "ffprobe": ffprobe}


def test_packaged_app_ffmpeg_check_rejects_resources_outside_bundle(tmp_path: Path) -> None:
    app_bundle = tmp_path / "Orpheus.app"
    sidecar = _write_executable(
        app_bundle / "Contents/Resources/binaries/orpheus-desktop-backend/orpheus-desktop-backend"
    )
    outside_ffmpeg = _write_executable(tmp_path / "outside/ffmpeg")
    _write_executable(sidecar.parent / "bin/ffprobe")
    sidecar.parent.joinpath("bin/ffmpeg").symlink_to(outside_ffmpeg)

    with pytest.raises(ValueError, match="inside app bundle"):
        desktop_runtime_smoke.resolve_ffmpeg_runtime_binaries(
            backend="sidecar",
            sidecar_path=None,
            app_bundle=app_bundle,
        )


def test_success_rate_threshold_uses_all_iterations() -> None:
    results = [
        desktop_runtime_smoke.IterationResult(
            index=1,
            passed=True,
            duration_seconds=0.1,
            ready_seconds=0.01,
            warm_library_seconds=0.02,
        ),
        desktop_runtime_smoke.IterationResult(
            index=2,
            passed=False,
            duration_seconds=0.1,
            ready_seconds=0.01,
            warm_library_seconds=0.02,
        ),
        desktop_runtime_smoke.IterationResult(
            index=3,
            passed=True,
            duration_seconds=0.1,
            ready_seconds=0.01,
            warm_library_seconds=0.02,
        ),
    ]

    assert desktop_runtime_smoke.success_rate(results) == 2 / 3
    assert desktop_runtime_smoke.results_meet_threshold(results, 0.66)
    assert not desktop_runtime_smoke.results_meet_threshold(results, 1.0)


def test_multipart_body_contains_file_and_form_metadata() -> None:
    body, content_type = desktop_runtime_smoke.encode_multipart_file(
        field_name="file",
        file_name="sample.md",
        content=b"# Sample\n\nBody\n",
        content_type="text/markdown",
    )

    assert content_type.startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="sample.md"' in body
    assert b"Content-Type: text/markdown" in body
    assert b"# Sample\n\nBody\n" in body


def test_local_tts_runtime_available_requires_explicit_runtime_probe() -> None:
    """Catalog engines alone must never authorize a conversion start."""
    assert not desktop_runtime_smoke.local_tts_runtime_available({})
    assert not desktop_runtime_smoke.local_tts_runtime_available(
        {
            "available_tts_engines": ["kokoro"],
            "default_tts_engine": "kokoro",
        }
    )
    assert not desktop_runtime_smoke.local_tts_runtime_available(
        {
            "available_tts_engines": ["kokoro"],
            "default_tts_engine": "kokoro",
            "mlx_runtime_available": False,
            "runtime_probe": {
                "state": "fresh",
                "refresh_in_progress": False,
                "tts": {"available": False, "latency_ms": None, "error": "not installed"},
            },
        }
    )
    assert not desktop_runtime_smoke.local_tts_runtime_available(
        {
            "available_tts_engines": ["kokoro"],
            "default_tts_engine": "kokoro",
            "runtime_probe": {"state": "pending", "refresh_in_progress": True},
        }
    )
    assert desktop_runtime_smoke.local_tts_runtime_available(
        {
            "available_tts_engines": ["kokoro"],
            "default_tts_engine": "kokoro",
            "runtime_probe": {
                "state": "fresh",
                "refresh_in_progress": False,
                "tts": {"available": True, "latency_ms": 12, "error": None},
            },
        }
    )
    assert desktop_runtime_smoke.local_tts_runtime_available(
        {"available_tts_engines": [], "mlx_runtime_available": True}
    )


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("runtime_ready,assets_ready", [(True, True), (True, False), (False, True)])
def test_package_tts_gate_requires_runtime_and_cached_assets(configured, runtime_ready, assets_ready):
    group = {"id": "tts-cpu-cuda", "runtime_ready": runtime_ready, "assets_ready": assets_ready}
    status = {
        "configured": configured,
        "selected_profile": "cuda" if configured else None,
        "packages": {"ready": True},
        "selected_model_groups": [group] if configured else [],
        "installed_runtime": None if configured else {
            "profile": "cuda", "runtime_available": True, "model_groups": [group],
        },
    }
    capabilities = {
        "primary_runtime": "cuda",
        "available_tts_engines": ["kokoro"],
        "runtime_probe": {"tts": {"available": False}},
        "setup_status": status,
    }
    assert desktop_runtime_smoke.local_tts_runtime_available(capabilities) == (runtime_ready and assets_ready)
    status["restart_required"] = True
    assert not desktop_runtime_smoke.local_tts_runtime_available(capabilities)


@pytest.mark.parametrize("status", [None, {}, {"installed_runtime": {"ready": True}}, {
    "configured": True, "selected_profile": "cuda", "packages": {"ready": True},
    "selected_model_groups": [{"id": "cleaner-cuda", "runtime_ready": True, "assets_ready": True}],
}])
def test_package_tts_gate_rejects_missing_tts_evidence(status):
    assert not desktop_runtime_smoke.local_tts_runtime_available({
        "primary_runtime": "cuda", "setup_status": status, "mlx_runtime_available": True,
    })


def test_package_capabilities_fetcher_reads_setup_status(monkeypatch):
    calls = []
    def request(method, url, **kwargs):
        calls.append(url)
        assert kwargs["token"] == "test-token"
        payload = {"primary_runtime": "cuda"} if url.endswith("capabilities") else {"configured": False}
        return desktop_runtime_smoke.HttpResult(200, json.dumps(payload).encode())
    monkeypatch.setattr(desktop_runtime_smoke, "http_request", request)
    fetch = desktop_runtime_smoke._capabilities_fetcher(
        desktop_runtime_smoke.ReadyPayload("127.0.0.1", 9, "test-token"),
        timeout_seconds=5, label="test",
    )
    assert fetch()["setup_status"] == {"configured": False}
    assert [url.rsplit("/", 1)[-1] for url in calls] == ["capabilities", "status"]


def test_classify_tts_probe_readiness_distinguishes_pending_unavailable_ready() -> None:
    assert (
        desktop_runtime_smoke.classify_tts_probe_readiness(
            {"available_tts_engines": ["kokoro"], "default_tts_engine": "kokoro"}
        )
        == desktop_runtime_smoke.TtsProbeReadiness.UNAVAILABLE
    )
    assert (
        desktop_runtime_smoke.classify_tts_probe_readiness(
            {
                "runtime_probe": {
                    "state": "pending",
                    "refresh_in_progress": True,
                    "tts": {"available": False, "latency_ms": None, "error": None},
                }
            }
        )
        == desktop_runtime_smoke.TtsProbeReadiness.PENDING
    )
    assert (
        desktop_runtime_smoke.classify_tts_probe_readiness(
            {
                "runtime_probe": {
                    "state": "fresh",
                    "refresh_in_progress": False,
                    "tts": {"available": False, "latency_ms": None, "error": "missing weights"},
                }
            }
        )
        == desktop_runtime_smoke.TtsProbeReadiness.UNAVAILABLE
    )
    assert (
        desktop_runtime_smoke.classify_tts_probe_readiness(
            {
                "runtime_probe": {
                    "state": "fresh",
                    "refresh_in_progress": False,
                    "tts": {"available": True, "latency_ms": 8, "error": None},
                }
            }
        )
        == desktop_runtime_smoke.TtsProbeReadiness.READY
    )


def test_wait_for_tts_runtime_ready_polls_pending_until_ready(monkeypatch) -> None:
    snapshots = [
        {
            "runtime_probe": {
                "state": "pending",
                "refresh_in_progress": True,
                "tts": {"available": False, "latency_ms": None, "error": None},
            }
        },
        {
            "runtime_probe": {
                "state": "pending",
                "refresh_in_progress": True,
                "tts": {"available": False, "latency_ms": None, "error": None},
            }
        },
        {
            "mlx_runtime_available": True,
            "runtime_probe": {
                "state": "fresh",
                "refresh_in_progress": False,
                "tts": {"available": True, "latency_ms": 11, "error": None},
            },
        },
    ]
    calls = {"n": 0}
    sleeps: list[float] = []

    def fetch():
        idx = min(calls["n"], len(snapshots) - 1)
        calls["n"] += 1
        return snapshots[idx]

    result = desktop_runtime_smoke.wait_for_tts_runtime_ready(
        fetch,
        probe_wait_seconds=5.0,
        poll_interval_seconds=0.1,
        sleep=sleeps.append,
    )

    assert result.readiness == desktop_runtime_smoke.TtsProbeReadiness.READY
    assert result.capabilities["mlx_runtime_available"] is True
    assert result.reason is None
    assert calls["n"] == 3
    assert sleeps == [0.1, 0.1]


def test_wait_for_tts_runtime_ready_skips_explicit_unavailable_without_polling() -> None:
    fetches = {"n": 0}

    def fetch():
        fetches["n"] += 1
        return {
            "available_tts_engines": ["kokoro"],
            "runtime_probe": {
                "state": "fresh",
                "refresh_in_progress": False,
                "tts": {"available": False, "latency_ms": None, "error": "weights missing"},
            },
        }

    result = desktop_runtime_smoke.wait_for_tts_runtime_ready(
        fetch,
        probe_wait_seconds=30.0,
        poll_interval_seconds=0.5,
        sleep=lambda _seconds: (_ for _ in ()).throw(AssertionError("should not sleep")),
    )

    assert result.readiness == desktop_runtime_smoke.TtsProbeReadiness.UNAVAILABLE
    assert fetches["n"] == 1
    assert "unavailable" in (result.reason or "").lower()
    assert "timed out" not in (result.reason or "").lower()


def test_wait_for_tts_runtime_ready_times_out_while_pending() -> None:
    fetches = {"n": 0}
    sleeps: list[float] = []

    def fetch():
        fetches["n"] += 1
        return {
            "runtime_probe": {
                "state": "pending",
                "refresh_in_progress": True,
                "tts": {"available": False, "latency_ms": None, "error": None},
            }
        }

    clock = {"now": 100.0}

    def fake_monotonic():
        return clock["now"]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    result = desktop_runtime_smoke.wait_for_tts_runtime_ready(
        fetch,
        probe_wait_seconds=0.3,
        poll_interval_seconds=0.1,
        sleep=fake_sleep,
        monotonic=fake_monotonic,
    )

    assert result.readiness == desktop_runtime_smoke.TtsProbeReadiness.PENDING
    assert "timed out" in (result.reason or "").lower() or "not ready" in (result.reason or "").lower()
    assert fetches["n"] >= 2
    assert sleeps


def test_run_conversion_journey_skips_when_tts_unavailable(monkeypatch) -> None:
    ready = desktop_runtime_smoke.ReadyPayload(host="127.0.0.1", port=9, token="t")
    calls: list[str] = []

    def fake_http(method: str, url: str, **_kwargs):
        calls.append(f"{method} {url}")
        if url.endswith("/api/uploads/text"):
            body = b'{"source_handle":"upload:journey.md"}'
            return desktop_runtime_smoke.HttpResult(200, body)
        if url.endswith("/api/sources/preflight"):
            body = b'{"chapter_count":1,"title":"Journey","warnings":[],"size_bytes":12,"source_type":"md","sample":"hi","requires_ocr":false,"author":null,"pdf_text_layer":null}'
            return desktop_runtime_smoke.HttpResult(200, body)
        if url.endswith("/api/system/capabilities"):
            body = b'{"available_tts_engines":[],"default_tts_engine":""}'
            return desktop_runtime_smoke.HttpResult(200, body)
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(desktop_runtime_smoke, "http_request", fake_http)

    result = desktop_runtime_smoke.run_conversion_journey(
        ready,
        timeout_seconds=5.0,
        conversion_timeout_seconds=5.0,
    )

    assert result.skipped is True
    assert result.completed is False
    assert "TTS" in (result.reason or "") or "probe" in (result.reason or "").lower()
    assert not any("POST " in call and "/api/conversions" in call for call in calls)
    assert any(call.startswith("POST ") and call.endswith("/api/uploads/text") for call in calls)


def test_require_local_tts_fails_when_capabilities_report_mlx_tts_false(
    monkeypatch, tmp_path: Path
) -> None:
    """Packaged-sidecar gates must fail closed when MLX TTS probe is false."""
    process = object()
    ready = desktop_runtime_smoke.ReadyPayload(host="127.0.0.1", port=9, token="t")

    monkeypatch.setattr(
        desktop_runtime_smoke,
        "start_backend",
        lambda **_kwargs: (process, ready, [], []),
    )
    monkeypatch.setattr(
        desktop_runtime_smoke,
        "run_api_checks",
        lambda *_args, **_kwargs: desktop_runtime_smoke.ApiCheckMetrics(
            warm_library_seconds=0.01,
            authenticated_library_requests=1,
        ),
    )
    monkeypatch.setattr(desktop_runtime_smoke, "stop_backend", lambda _process: None)
    monkeypatch.setattr(
        desktop_runtime_smoke.RuntimePaths,
        "create",
        classmethod(
            lambda cls: desktop_runtime_smoke.RuntimePaths(
                root=tmp_path,
                data_dir=tmp_path / "data",
                output_dir=tmp_path / "output",
                temp_dir=tmp_path / "temp",
                db_path=tmp_path / "data" / "audiobooks.db",
                ready_file=tmp_path / "backend-ready.json",
            )
        ),
    )
    monkeypatch.setattr(
        desktop_runtime_smoke,
        "wait_for_tts_runtime_ready",
        lambda *_args, **_kwargs: desktop_runtime_smoke.TtsProbeWaitResult(
            capabilities={
                "mlx_runtime_available": False,
                "runtime_probe": {
                    "state": "fresh",
                    "refresh_in_progress": False,
                    "tts": {
                        "available": False,
                        "error": "probe produced no output",
                    },
                },
            },
            readiness=desktop_runtime_smoke.TtsProbeReadiness.UNAVAILABLE,
            reason="TTS runtime probe finished unavailable",
        ),
    )

    result = desktop_runtime_smoke.run_iteration(
        0,
        backend="sidecar",
        sidecar_path=tmp_path / "orpheus-desktop-backend",
        python_executable=sys.executable,
        request_count=1,
        timeout_seconds=1.0,
        reduced_path=True,
        skip_sse=True,
        require_local_tts=True,
        probe_wait_seconds=1.0,
    )

    assert result.passed is False
    assert "mlx" in (result.error or "").lower() or "tts" in (result.error or "").lower()
    assert "probe produced no output" in (result.error or "")


def test_run_iteration_runs_conversion_journey_when_requested(monkeypatch, tmp_path: Path) -> None:
    process = object()
    ready = desktop_runtime_smoke.ReadyPayload(host="127.0.0.1", port=12345, token="tok")
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        desktop_runtime_smoke,
        "start_backend",
        lambda **kwargs: (
            observed.__setitem__("start_backend_kwargs", kwargs) or (process, ready, [], [])
        ),
    )
    monkeypatch.setattr(
        desktop_runtime_smoke,
        "run_api_checks",
        lambda *_args, **_kwargs: desktop_runtime_smoke.ApiCheckMetrics(
            warm_library_seconds=0.01,
            authenticated_library_requests=1,
        ),
    )
    monkeypatch.setattr(desktop_runtime_smoke, "stop_backend", lambda _process: None)
    monkeypatch.setattr(
        desktop_runtime_smoke.RuntimePaths,
        "create",
        classmethod(
            lambda cls: desktop_runtime_smoke.RuntimePaths(
                root=tmp_path,
                data_dir=tmp_path / "data",
                output_dir=tmp_path / "output",
                temp_dir=tmp_path / "temp",
                db_path=tmp_path / "data" / "audiobooks.db",
                ready_file=tmp_path / "backend-ready.json",
            )
        ),
    )

    def fake_journey(ready_payload, **kwargs):
        observed["ready"] = ready_payload
        observed.update(kwargs)
        return desktop_runtime_smoke.ConversionJourneyResult(
            completed=False,
            skipped=True,
            reason=(
                "TTS runtime probe not ready "
                "(catalog engines alone are insufficient); "
                "conversion portion skipped without downloading models"
            ),
        )

    monkeypatch.setattr(desktop_runtime_smoke, "run_conversion_journey", fake_journey)

    result = desktop_runtime_smoke.run_iteration(
        1,
        backend="python",
        sidecar_path=None,
        python_executable=sys.executable,
        request_count=1,
        timeout_seconds=30.0,
        reduced_path=False,
        skip_sse=True,
        conversion_journey=True,
        conversion_timeout_seconds=12.0,
    )

    assert result.passed is True
    assert observed["conversion_timeout_seconds"] == 12.0
    assert observed["probe_wait_seconds"] == 25.0
    assert observed["start_backend_kwargs"]["conversion_journey"] is True


def test_run_iteration_records_ready_and_warm_library_timings(monkeypatch, tmp_path: Path) -> None:
    request_count = 10
    ready_budget_seconds = 0.5
    warm_library_budget_seconds = 0.5
    process = object()
    ready = desktop_runtime_smoke.ReadyPayload(
        host="127.0.0.1",
        port=12345,
        token="synthetic-token",
    )
    observed: dict[str, int | bool] = {}

    def fake_start_backend(**kwargs):
        started = time.perf_counter()
        while time.perf_counter() - started < 0.01:
            pass
        return process, ready, [], []

    def fake_run_api_checks(
        _ready_payload,
        *,
        request_count: int,
        timeout_seconds: float,
        skip_sse: bool,
    ):
        observed["requests"] = request_count
        observed["skip_sse"] = skip_sse
        started = time.perf_counter()
        while time.perf_counter() - started < 0.01:
            pass
        return desktop_runtime_smoke.ApiCheckMetrics(
            warm_library_seconds=time.perf_counter() - started,
            authenticated_library_requests=request_count,
        )

    monkeypatch.setattr(desktop_runtime_smoke, "start_backend", fake_start_backend)
    monkeypatch.setattr(desktop_runtime_smoke, "run_api_checks", fake_run_api_checks)
    monkeypatch.setattr(desktop_runtime_smoke, "stop_backend", lambda _process: None)
    monkeypatch.setattr(
        desktop_runtime_smoke.RuntimePaths,
        "create",
        classmethod(lambda cls: desktop_runtime_smoke.RuntimePaths(
            root=tmp_path,
            data_dir=tmp_path / "data",
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            db_path=tmp_path / "data" / "audiobooks.db",
            ready_file=tmp_path / "backend-ready.json",
        )),
    )

    result = desktop_runtime_smoke.run_iteration(
        1,
        backend="python",
        sidecar_path=None,
        python_executable=sys.executable,
        request_count=request_count,
        timeout_seconds=30.0,
        reduced_path=False,
        skip_sse=True,
    )

    assert result.passed is True
    assert result.ready_seconds is not None
    assert result.ready_seconds < ready_budget_seconds
    assert result.warm_library_seconds is not None
    assert result.warm_library_seconds < warm_library_budget_seconds
    assert result.authenticated_library_requests == request_count
    assert observed == {"requests": request_count, "skip_sse": True}


def test_run_iteration_rejects_packaged_runtime_profile_mismatch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    app_bundle = tmp_path / "Orpheus.app"
    marker = (
        app_bundle
        / "Contents/Resources/binaries/orpheus-desktop-backend/runtime-profile.json"
    )
    marker.parent.mkdir(parents=True)
    marker.write_text('{"profile": "base"}\n', encoding="utf-8")
    ready = desktop_runtime_smoke.ReadyPayload(
        host="127.0.0.1",
        port=12345,
        token="synthetic-token",
        runtime_profile="cuda",
    )
    runtime_paths = desktop_runtime_smoke.RuntimePaths(
        root=tmp_path / "runtime",
        data_dir=tmp_path / "runtime/data",
        output_dir=tmp_path / "runtime/output",
        temp_dir=tmp_path / "runtime/temp",
        db_path=tmp_path / "runtime/data/audiobooks.db",
        ready_file=tmp_path / "runtime/backend-ready.json",
    )
    monkeypatch.setattr(
        desktop_runtime_smoke,
        "start_backend",
        lambda **_kwargs: (object(), ready, [], []),
    )
    monkeypatch.setattr(
        desktop_runtime_smoke.RuntimePaths,
        "create",
        classmethod(lambda cls: runtime_paths),
    )
    monkeypatch.setattr(desktop_runtime_smoke, "stop_backend", lambda _process: None)
    monkeypatch.setattr(
        desktop_runtime_smoke,
        "run_api_checks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("profile must be checked before API requests")
        ),
    )

    result = desktop_runtime_smoke.run_iteration(
        1,
        backend="sidecar",
        sidecar_path=None,
        app_bundle=app_bundle,
        python_executable=sys.executable,
        request_count=1,
        timeout_seconds=30.0,
        reduced_path=True,
        skip_sse=True,
    )

    assert result.passed is False
    assert "runtime profile mismatch" in (result.error or "").lower()


def test_run_iteration_runs_ffmpeg_runtime_check_when_requested(monkeypatch, tmp_path: Path) -> None:
    process = object()
    ready = desktop_runtime_smoke.ReadyPayload(
        host="127.0.0.1",
        port=12345,
        token="synthetic-token",
    )
    runtime_paths = desktop_runtime_smoke.RuntimePaths(
        root=tmp_path,
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "audiobooks.db",
        ready_file=tmp_path / "backend-ready.json",
    )
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        desktop_runtime_smoke,
        "start_backend",
        lambda **_kwargs: (process, ready, [], []),
    )
    monkeypatch.setattr(
        desktop_runtime_smoke,
        "run_api_checks",
        lambda *_args, **_kwargs: desktop_runtime_smoke.ApiCheckMetrics(
            warm_library_seconds=0.01,
            authenticated_library_requests=1,
        ),
    )
    monkeypatch.setattr(desktop_runtime_smoke, "stop_backend", lambda _process: None)
    monkeypatch.setattr(
        desktop_runtime_smoke.RuntimePaths,
        "create",
        classmethod(lambda cls: runtime_paths),
    )

    def fake_ffmpeg_runtime_check(**kwargs) -> None:
        observed.update(kwargs)

    monkeypatch.setattr(
        desktop_runtime_smoke,
        "run_ffmpeg_runtime_check",
        fake_ffmpeg_runtime_check,
        raising=False,
    )

    result = desktop_runtime_smoke.run_iteration(
        1,
        backend="sidecar",
        sidecar_path=tmp_path / "sidecar" / "orpheus-desktop-backend",
        python_executable=sys.executable,
        request_count=1,
        timeout_seconds=30.0,
        reduced_path=True,
        skip_sse=True,
        check_ffmpeg_runtime=True,
    )

    assert result.passed is True
    assert observed["backend"] == "sidecar"
    assert observed["runtime_paths"] == runtime_paths
    assert observed["reduced_path"] is True


def test_ffmpeg_runtime_check_uses_sidecar_bin_and_reduced_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    sidecar = _write_executable(tmp_path / "sidecar" / "orpheus-desktop-backend")
    ffmpeg = _write_executable(sidecar.parent / "bin" / "ffmpeg")
    ffprobe = _write_executable(sidecar.parent / "bin" / "ffprobe")
    runtime_paths = desktop_runtime_smoke.RuntimePaths(
        root=tmp_path / "runtime",
        data_dir=tmp_path / "runtime" / "data",
        output_dir=tmp_path / "runtime" / "output",
        temp_dir=tmp_path / "runtime" / "temp",
        db_path=tmp_path / "runtime" / "data" / "audiobooks.db",
        ready_file=tmp_path / "runtime" / "backend-ready.json",
    )
    commands: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN202
        command_parts = [str(part) for part in command]
        commands.append((command_parts, kwargs))
        if command_parts[0] == str(ffmpeg):
            Path(command_parts[-1]).write_bytes(b"wav")
            return MagicMock(returncode=0, stdout="", stderr="")
        if command_parts[0] == str(ffprobe):
            return MagicMock(
                returncode=0,
                stdout='{"format":{"duration":"0.01"}}',
                stderr="",
            )
        raise AssertionError(f"unexpected command: {command_parts}")

    monkeypatch.setattr(desktop_runtime_smoke.subprocess, "run", fake_run)

    desktop_runtime_smoke.run_ffmpeg_runtime_check(
        backend="sidecar",
        sidecar_path=sidecar,
        runtime_paths=runtime_paths,
        timeout_seconds=5.0,
        reduced_path=True,
    )

    assert [command[0] for command, _kwargs in commands] == [str(ffmpeg), str(ffprobe)]
    assert commands[0][1]["env"]["PATH"] == desktop_runtime_smoke.REDUCED_MACOS_PATH
    assert commands[1][1]["env"]["PATH"] == desktop_runtime_smoke.REDUCED_MACOS_PATH
    assert "-f" in commands[0][0]
    assert "lavfi" in commands[0][0]
    assert "-show_entries" in commands[1][0]


def test_sidecar_ffmpeg_runtime_binaries_accept_windows_exe_names(tmp_path: Path) -> None:
    sidecar = _write_executable(tmp_path / "sidecar" / "orpheus-desktop-backend.exe")
    ffmpeg = _write_executable(sidecar.parent / "bin" / "ffmpeg.exe")
    ffprobe = _write_executable(sidecar.parent / "bin" / "ffprobe.exe")

    resolved = desktop_runtime_smoke.resolve_ffmpeg_runtime_binaries(
        backend="sidecar",
        sidecar_path=sidecar,
    )

    assert resolved == {"ffmpeg": ffmpeg, "ffprobe": ffprobe}


def test_sse_ping_uses_scoped_events_query_token(monkeypatch) -> None:
    ready = desktop_runtime_smoke.ReadyPayload(
        host="127.0.0.1",
        port=12345,
        token="long-lived-token",
    )
    observed: dict[str, object] = {}

    def fake_http_request(
        method: str,
        url: str,
        *,
        token: str | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
        timeout_seconds: float,
    ) -> desktop_runtime_smoke.HttpResult:
        observed["query_token_request"] = {
            "method": method,
            "url": url,
            "token": token,
            "data": data,
            "content_type": content_type,
            "timeout_seconds": timeout_seconds,
        }
        return desktop_runtime_smoke.HttpResult(
            status=200,
            body=b'{"token":"scoped-events-token","expires_at":1234567890}',
        )

    class FakeSseResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def readline(self) -> bytes:
            return b"event: ping\n"

    def fake_urlopen(request, *, timeout: float):
        observed["sse_url"] = request.full_url
        observed["sse_timeout"] = timeout
        return FakeSseResponse()

    monkeypatch.setattr(desktop_runtime_smoke, "http_request", fake_http_request)
    monkeypatch.setattr(desktop_runtime_smoke, "urlopen", fake_urlopen)

    desktop_runtime_smoke.check_sse_ping(ready, timeout_seconds=2.0)

    assert observed["query_token_request"] == {
        "method": "POST",
        "url": "http://127.0.0.1:12345/api/desktop/query-token",
        "token": "long-lived-token",
        "data": b'{"scope": "events"}',
        "content_type": "application/json",
        "timeout_seconds": 2.0,
    }
    assert observed["sse_url"] == (
        "http://127.0.0.1:12345/api/events/stream?token=scoped-events-token"
    )
    assert observed["sse_timeout"] == 2.0


def test_wait_for_conversion_terminal_ignores_wrong_job_terminal_payload(monkeypatch) -> None:
    ready = desktop_runtime_smoke.ReadyPayload(host="127.0.0.1", port=9, token="t")
    polls = [
        {
            "active": True,
            "job": {"id": "other-job", "status": "completed", "book_id": 99},
        },
        {
            "active": True,
            "job": {"id": "started-job", "status": "running", "book_id": None},
        },
        {
            "active": False,
            "job": {"id": "started-job", "status": "completed", "book_id": 42},
        },
    ]
    index = {"n": 0}

    def fake_http(method: str, url: str, **_kwargs):
        assert method == "GET"
        assert url.endswith("/api/conversions/active")
        payload = polls[min(index["n"], len(polls) - 1)]
        index["n"] += 1
        return desktop_runtime_smoke.HttpResult(200, json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(desktop_runtime_smoke, "http_request", fake_http)
    monkeypatch.setattr(desktop_runtime_smoke.time, "sleep", lambda _seconds: None)

    terminal = desktop_runtime_smoke.wait_for_conversion_terminal(
        ready,
        job_id="started-job",
        timeout_seconds=5.0,
    )

    assert terminal["status"] == "completed"
    assert terminal["book_id"] == 42
    assert index["n"] >= 3


@pytest.mark.parametrize("library_epoch", ["selected-library-epoch", None, ""])
def test_run_conversion_journey_selects_matching_book_not_library_first(
    monkeypatch, library_epoch,
) -> None:
    ready = desktop_runtime_smoke.ReadyPayload(host="127.0.0.1", port=9, token="t")
    calls: list[str] = []

    def fake_http(method: str, url: str, **kwargs):
        calls.append(f"{method} {url}")
        if url.endswith("/api/uploads/text"):
            return desktop_runtime_smoke.HttpResult(
                200, b'{"source_handle":"upload:journey.md"}'
            )
        if url.endswith("/api/sources/preflight"):
            return desktop_runtime_smoke.HttpResult(
                200,
                b'{"chapter_count":1,"title":"Conversion Journey Smoke","warnings":[],'
                b'"size_bytes":12,"source_type":"md","sample":"hi","requires_ocr":false,'
                b'"author":null,"pdf_text_layer":null}',
            )
        if url.endswith("/api/system/capabilities"):
            return desktop_runtime_smoke.HttpResult(
                200,
                b'{"available_tts_engines":["kokoro"],"default_tts_engine":"kokoro",'
                b'"runtime_probe":{"state":"fresh","refresh_in_progress":false,'
                b'"tts":{"available":true,"latency_ms":1,"error":null}}}',
            )
        if url.endswith("/api/options/models"):
            return desktop_runtime_smoke.HttpResult(
                200,
                b'{"tts_engines":[{"id":"kokoro","voices":[{"id":"am_adam"}]}]}',
            )
        if url.endswith("/api/conversions") and method == "POST":
            return desktop_runtime_smoke.HttpResult(
                200, b'{"job_id":"job-xyz","status":"running","book_id":null}'
            )
        if url.endswith("/api/library?limit=20"):
            return desktop_runtime_smoke.HttpResult(
                200,
                json.dumps({"items": [
                    {"id": 1, "title": "Unrelated First Book", "library_epoch": "unrelated-epoch"},
                    {"id": 77, "title": "Conversion Journey Smoke", "library_epoch": library_epoch},
                ]}).encode("utf-8"),
            )
        if "/api/desktop/query-token" in url:
            assert json.loads(kwargs["data"]) == {
                "scope": "audio", "book_id": 77, "expected_epoch": library_epoch,
            }
            return desktop_runtime_smoke.HttpResult(
                200, b'{"token":"audio-token","expires_at":1}'
            )
        if "/api/library/77/audio" in url:
            return desktop_runtime_smoke.HttpResult(200, b"ID3fake-audio")
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(desktop_runtime_smoke, "http_request", fake_http)
    monkeypatch.setattr(
        desktop_runtime_smoke,
        "wait_for_conversion_terminal",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "payload": {
                "active": False,
                "job": {"id": "job-xyz", "status": "completed", "book_id": 77},
            },
            "book_id": 77,
        },
    )

    if not library_epoch:
        with pytest.raises(desktop_runtime_smoke.SmokeFailure, match="missing its library epoch"):
            desktop_runtime_smoke.run_conversion_journey(
                ready,
                timeout_seconds=5.0,
                conversion_timeout_seconds=5.0,
                probe_wait_seconds=0.0,
            )
        assert not any("/api/desktop/query-token" in call for call in calls)
        return

    result = desktop_runtime_smoke.run_conversion_journey(
        ready,
        timeout_seconds=5.0,
        conversion_timeout_seconds=5.0,
        probe_wait_seconds=0.0,
    )

    assert result.completed is True
    assert result.book_id == 77
    assert any("/api/library/77/audio" in call for call in calls)
    assert not any("/api/library/1/audio" in call for call in calls)


def test_build_child_env_sets_offline_model_flags_only_for_conversion_journey(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    runtime_paths = desktop_runtime_smoke.RuntimePaths(
        root=tmp_path,
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "audiobooks.db",
        ready_file=tmp_path / "backend-ready.json",
    )

    default_env = desktop_runtime_smoke.build_child_env(
        runtime_paths,
        reduced_path=False,
        backend="python",
        conversion_journey=False,
    )
    assert "HF_HUB_OFFLINE" not in default_env
    assert "TRANSFORMERS_OFFLINE" not in default_env

    journey_env = desktop_runtime_smoke.build_child_env(
        runtime_paths,
        reduced_path=False,
        backend="python",
        conversion_journey=True,
    )
    assert journey_env["HF_HUB_OFFLINE"] == "1"
    assert journey_env["TRANSFORMERS_OFFLINE"] == "1"


def test_build_child_env_preserves_parent_offline_flags_for_conversion_journey(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    runtime_paths = desktop_runtime_smoke.RuntimePaths(
        root=tmp_path,
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "audiobooks.db",
        ready_file=tmp_path / "backend-ready.json",
    )

    journey_env = desktop_runtime_smoke.build_child_env(
        runtime_paths,
        reduced_path=False,
        backend="python",
        conversion_journey=True,
    )
    assert journey_env["HF_HUB_OFFLINE"] == "1"
    assert journey_env["TRANSFORMERS_OFFLINE"] == "1"


def test_wait_for_conversion_journey_book_fails_closed_on_ambiguous_title(
    monkeypatch,
) -> None:
    ready = desktop_runtime_smoke.ReadyPayload(host="127.0.0.1", port=9, token="t")
    title = desktop_runtime_smoke.CONVERSION_JOURNEY_TITLE
    payload = {
        "items": [
            {"id": 11, "title": title},
            {"id": 22, "title": title},
        ]
    }

    def fake_http(method: str, url: str, **_kwargs):
        assert method == "GET"
        assert url.endswith("/api/library?limit=20")
        return desktop_runtime_smoke.HttpResult(200, json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(desktop_runtime_smoke, "http_request", fake_http)
    monkeypatch.setattr(desktop_runtime_smoke.time, "sleep", lambda _seconds: None)

    with pytest.raises(desktop_runtime_smoke.SmokeFailure, match="ambiguous library title"):
        desktop_runtime_smoke.wait_for_conversion_journey_book(
            ready,
            book_id=None,
            expected_title=title,
            timeout_seconds=5.0,
        )


@pytest.mark.parametrize("startup", ["timeout", "invalid_stdout", "invalid_file", "exit", "ready"])
def test_backend_tree_is_stopped_after_startup_failure_or_normal_shutdown(
    tmp_path: Path, monkeypatch, startup: str,
) -> None:
    import psutil

    pid_file = tmp_path / "child.pid"
    ready_file = tmp_path / "ready.json"
    payload = json.dumps({
        "event": "ready", "host": "127.0.0.1", "port": 1234,
        "token": "test", "runtime_profile": "base",
    })
    code = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"Path({str(pid_file)!r}).write_text(str(child.pid))\n"
    )
    if startup == "invalid_stdout":
        code += "print('{}', flush=True)\n"
    elif startup == "invalid_file":
        code += f"Path({str(ready_file)!r}).write_text('{{')\n"
    elif startup == "exit":
        code += "sys.exit(3)\n"
    elif startup == "ready":
        code += f"print({payload!r}, flush=True)\n"
    code += "time.sleep(60)\n"
    monkeypatch.setattr(desktop_runtime_smoke, "build_backend_command", lambda **_: [sys.executable, "-c", code])
    processes = []
    popen = subprocess.Popen

    def spawn(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(desktop_runtime_smoke.subprocess, "Popen", spawn)
    paths = desktop_runtime_smoke.RuntimePaths(
        tmp_path, tmp_path / "data", tmp_path / "output", tmp_path / "temp",
        tmp_path / "db", ready_file,
    )
    try:
        kwargs = dict(
            backend="python", sidecar_path=None, python_executable=sys.executable,
            runtime_paths=paths, timeout_seconds=1.0, reduced_path=False,
        )
        if startup == "ready":
            process, _, _, _ = desktop_runtime_smoke.start_backend(**kwargs)
            desktop_runtime_smoke.stop_backend(process, timeout_seconds=0.5)
        else:
            with pytest.raises((desktop_runtime_smoke.SmokeFailure, json.JSONDecodeError)):
                desktop_runtime_smoke.start_backend(**kwargs)
        assert processes[0].poll() is not None
        try:
            child = psutil.Process(int(pid_file.read_text()))
            child.wait(timeout=3)
        except psutil.NoSuchProcess:
            pass
    finally:
        if pid_file.exists():
            try:
                child = psutil.Process(int(pid_file.read_text()))
                child.kill()
                child.wait(timeout=3)
            except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                pass
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


def test_backend_output_readers_survive_bytes_outside_the_locale_encoding(
    tmp_path: Path, monkeypatch,
) -> None:
    """Progress bars and native libraries write bytes no ANSI code page maps.

    A reader thread that dies on them stops draining its pipe, so the ready line
    is never seen and the backend eventually blocks on a full stderr pipe.
    """
    payload = json.dumps({
        "event": "ready", "host": "127.0.0.1", "port": 1234,
        "token": "test", "runtime_profile": "cuda",
    })
    code = (
        "import sys, time\n"
        "for stream in (sys.stdout.buffer, sys.stderr.buffer):\n"
        "    stream.write('Loading weights 10%|▏|\\n'.encode('utf-8') + b'\\x81\\x8f\\xff\\n')\n"
        "    stream.flush()\n"
        "sys.stderr.write('after-unmapped-bytes\\n'); sys.stderr.flush()\n"
        f"print({payload!r}, flush=True)\n"
        "time.sleep(60)\n"
    )
    monkeypatch.setattr(desktop_runtime_smoke, "build_backend_command", lambda **_: [sys.executable, "-c", code])
    paths = desktop_runtime_smoke.RuntimePaths(
        tmp_path, tmp_path / "data", tmp_path / "output", tmp_path / "temp",
        tmp_path / "db", tmp_path / "ready.json",
    )

    process, ready, _stdout, stderr = desktop_runtime_smoke.start_backend(
        backend="python", sidecar_path=None, python_executable=sys.executable,
        runtime_paths=paths, timeout_seconds=10.0, reduced_path=False,
    )
    try:
        assert ready.runtime_profile == "cuda"
        deadline = time.monotonic() + 5
        while "after-unmapped-bytes" not in stderr and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "after-unmapped-bytes" in stderr
    finally:
        desktop_runtime_smoke.stop_backend(process, timeout_seconds=2)


def test_default_sidecar_path_names_the_windows_executable(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(desktop_runtime_smoke, "DEFAULT_APP_SIDECAR", tmp_path / "missing.app" / "sidecar")
    monkeypatch.setattr(desktop_runtime_smoke.os, "name", "nt")

    resolved = desktop_runtime_smoke.resolve_default_sidecar_path()

    assert resolved.name == "orpheus-desktop-backend.exe"
    assert resolved.parent == desktop_runtime_smoke.DEFAULT_RESOURCE_SIDECAR.parent


@pytest.mark.parametrize("category", ["thread", "multiprocessing", "subprocess"])
@pytest.mark.parametrize("exitstatus", [0, 2])
def test_session_leaks_return_failure_without_hiding_existing_failure(monkeypatch, category, exitstatus):
    import importlib.util

    spec = importlib.util.spec_from_file_location("session_cleanup", Path(__file__).with_name("conftest.py"))
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)
    import faulthandler
    from types import SimpleNamespace

    leaked = MagicMock(daemon=False, pid=12345)
    leaked.name = "unfinished-worker" if category == "thread" else MagicMock(return_value="python")
    leaked.cmdline.return_value = ["python", "worker.py"]
    monkeypatch.setattr(conftest.threading, "enumerate", lambda: [leaked] if category == "thread" else [])
    monkeypatch.setattr(conftest.multiprocessing, "active_children", lambda: [leaked] if category == "multiprocessing" else [])
    parent = MagicMock()
    parent.children.return_value = [leaked] if category == "subprocess" else []
    class WaitExpired(Exception):
        pass

    leaked.wait.side_effect = WaitExpired
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        Process=lambda: parent, TimeoutExpired=WaitExpired, NoSuchProcess=ProcessLookupError,
    ))
    monkeypatch.setattr(faulthandler, "dump_traceback", lambda **_: None)
    exits = []
    monkeypatch.setattr(conftest.os, "_exit", exits.append)
    monkeypatch.setattr(conftest, "_session_exit_status", exitstatus)
    hook = conftest.pytest_unconfigure(MagicMock())
    next(hook)
    with pytest.raises(StopIteration):
        next(hook)
    assert exits == [exitstatus or 1]


def test_backend_output_retains_bounded_recent_diagnostics():
    import io
    import queue

    output = []
    lines = queue.Queue(maxsize=200)
    stream = io.StringIO("x" * 1_000_000 + "\n" + "last line\n")
    desktop_runtime_smoke._reader_thread(stream, output, lines)
    assert sum(map(len, output)) <= 200 * 16384
    assert max(map(len, output)) <= 16384
    assert output[-1] == "last line"


@pytest.mark.parametrize("child_command", ["multiprocessing.resource_tracker", "worker.py"])
def test_finished_or_expected_session_children_do_not_fail(monkeypatch, child_command):
    import importlib.util
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location("session_cleanup", Path(__file__).with_name("conftest.py"))
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)
    child = MagicMock()
    child.name.return_value = "python"
    child.cmdline.return_value = ["python", child_command]
    parent = MagicMock()
    parent.children.return_value = [child]
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=lambda: parent))
    thread = MagicMock(daemon=False)
    thread.name = "finishing-worker"
    thread.is_alive.side_effect = [True, False]
    monkeypatch.setattr(conftest.threading, "enumerate", lambda: [thread])
    monkeypatch.setattr(conftest.multiprocessing, "active_children", lambda: [])
    exits = []
    monkeypatch.setattr(conftest.os, "_exit", exits.append)
    hook = conftest.pytest_unconfigure(MagicMock())
    next(hook)
    with pytest.raises(StopIteration):
        next(hook)
    assert exits == []
    assert child.wait.call_count == (0 if "resource_tracker" in child_command else 1)


def test_backend_environment_watches_smoke_driver(tmp_path, monkeypatch):
    monkeypatch.setenv('ORPHEUS_DESKTOP_PARENT_PID', '999999')
    paths = desktop_runtime_smoke.RuntimePaths(tmp_path, tmp_path / 'data', tmp_path / 'output',
                                              tmp_path / 'temp', tmp_path / 'db', tmp_path / 'ready')
    env = desktop_runtime_smoke.build_child_env(paths, backend='python', reduced_path=False)
    assert env['ORPHEUS_DESKTOP_PARENT_PID'] == str(os.getpid())


def _mock_windows_backend(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(desktop_runtime_smoke, 'os', SimpleNamespace(**dict(vars(os), name='nt')))
    process = MagicMock()
    process.poll.return_value = None
    monkeypatch.setattr(desktop_runtime_smoke.subprocess, 'Popen', MagicMock(return_value=process))
    monkeypatch.setattr(desktop_runtime_smoke, 'build_backend_command', lambda **kwargs: ['backend'])
    monkeypatch.setattr(desktop_runtime_smoke, 'build_child_env', lambda *a, **kwargs: {})
    return process


def test_windows_backend_is_suspended_until_owned_and_resumed(tmp_path, monkeypatch):
    process = _mock_windows_backend(monkeypatch)
    events = []

    def own(child):
        assert desktop_runtime_smoke.subprocess.Popen.call_args.kwargs.get('creationflags', 0) & 0x4
        events.append('owned')

    monkeypatch.setattr(desktop_runtime_smoke, '_own_windows_process_tree', own)
    monkeypatch.setattr(desktop_runtime_smoke, '_resume_windows_process', lambda child: events.append('resumed'), raising=False)
    monkeypatch.setattr(desktop_runtime_smoke, '_reader_thread', lambda *args: None)
    monkeypatch.setattr(desktop_runtime_smoke, '_load_ready_file', lambda _: 'ready')
    paths = desktop_runtime_smoke.RuntimePaths(tmp_path, tmp_path / 'data', tmp_path / 'output',
                                              tmp_path / 'temp', tmp_path / 'db', tmp_path / 'ready')
    desktop_runtime_smoke.start_backend(backend='python', sidecar_path=None, python_executable=sys.executable,
                                       runtime_paths=paths, timeout_seconds=1, reduced_path=False)
    assert events == ['owned', 'resumed']
    process.kill.assert_not_called()


def test_windows_backend_assignment_failure_kills_before_resume(tmp_path, monkeypatch):
    process = _mock_windows_backend(monkeypatch)
    monkeypatch.setattr(desktop_runtime_smoke, '_own_windows_process_tree', MagicMock(side_effect=OSError('cannot assign')))
    resume = MagicMock()
    monkeypatch.setattr(desktop_runtime_smoke, '_resume_windows_process', resume, raising=False)
    monkeypatch.setattr(desktop_runtime_smoke, 'stop_backend', MagicMock())
    paths = desktop_runtime_smoke.RuntimePaths(tmp_path, tmp_path / 'data', tmp_path / 'output',
                                              tmp_path / 'temp', tmp_path / 'db', tmp_path / 'ready')
    with pytest.raises(OSError, match='cannot assign'):
        desktop_runtime_smoke.start_backend(backend='python', sidecar_path=None, python_executable=sys.executable,
                                           runtime_paths=paths, timeout_seconds=1, reduced_path=False)
    process.kill.assert_called_once()
    process.wait.assert_called()
    resume.assert_not_called()


def test_windows_cleanup_attempts_tree_kill_after_parent_exits(monkeypatch):
    process = _mock_windows_backend(monkeypatch)
    process._close_process_tree = None
    process.poll.return_value = 3
    process.pid = 123
    run = MagicMock()
    monkeypatch.setattr(desktop_runtime_smoke.subprocess, 'run', run)
    desktop_runtime_smoke.stop_backend(process)
    run.assert_called_once()
    assert run.call_args.args[0][-3:] == ['123', '/T', '/F']


@pytest.mark.parametrize('status', [0, -1])
def test_windows_resume_process_checks_native_status(monkeypatch, status):
    import ctypes

    resume = MagicMock(return_value=status)
    library = MagicMock(NtResumeProcess=resume)
    load = MagicMock(return_value=library)
    monkeypatch.setattr(ctypes, 'WinDLL', load, raising=False)
    process = MagicMock(_handle=123)
    if status < 0:
        with pytest.raises(OSError, match='NTSTATUS'):
            desktop_runtime_smoke._resume_windows_process(process)
    else:
        desktop_runtime_smoke._resume_windows_process(process)
    load.assert_called_once_with('ntdll')
    resume.assert_called_once_with(123)
    assert resume.argtypes == [ctypes.wintypes.HANDLE]
    assert resume.restype is ctypes.c_long


@pytest.mark.parametrize('platform', ['posix', 'nt'])
def test_bundled_backend_inherits_driver_process_group(tmp_path, monkeypatch, platform):
    import ctypes
    from types import SimpleNamespace

    monkeypatch.setattr(desktop_runtime_smoke, 'os', SimpleNamespace(**dict(vars(os), name=platform)))
    monkeypatch.setattr(ctypes, 'WinDLL', MagicMock(side_effect=AssertionError(
        'mocked backend must not call Windows process APIs'
    )), raising=False)
    monkeypatch.setenv('ORPHEUS_SMOKE_PROCESS_GROUP', '1')
    process = MagicMock()
    spawn = MagicMock(return_value=process)
    monkeypatch.setattr(desktop_runtime_smoke.subprocess, 'Popen', spawn)
    monkeypatch.setattr(desktop_runtime_smoke, '_own_windows_process_tree', lambda _: None)
    monkeypatch.setattr(desktop_runtime_smoke, '_resume_windows_process', lambda _: None)
    monkeypatch.setattr(desktop_runtime_smoke, 'build_backend_command', lambda **kwargs: ['backend'])
    monkeypatch.setattr(desktop_runtime_smoke, '_reader_thread', lambda *args: None)
    monkeypatch.setattr(desktop_runtime_smoke, '_load_ready_file', lambda _: 'ready')
    paths = desktop_runtime_smoke.RuntimePaths(tmp_path, tmp_path / 'data', tmp_path / 'output',
                                              tmp_path / 'temp', tmp_path / 'db', tmp_path / 'ready')
    desktop_runtime_smoke.start_backend(backend='python', sidecar_path=None, python_executable=sys.executable,
                                       runtime_paths=paths, timeout_seconds=1, reduced_path=False)
    assert spawn.call_args.kwargs['start_new_session'] is False
    assert process._owns_process_group is False


def test_runtime_root_removal_retries_until_handles_are_released(monkeypatch, tmp_path):
    root = tmp_path / 'smoke-root'
    (root / 'data').mkdir(parents=True)
    held = root / 'data' / 'audiobooks.db'
    held.write_bytes(b'db')
    real_rmtree = desktop_runtime_smoke.shutil.rmtree
    attempts = []

    def rmtree(path, ignore_errors=False):
        attempts.append(path)
        if len(attempts) > 1:
            real_rmtree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(desktop_runtime_smoke.shutil, 'rmtree', rmtree)
    monkeypatch.setattr(desktop_runtime_smoke.time, 'sleep', lambda _seconds: None)

    desktop_runtime_smoke._remove_runtime_root(root)

    assert len(attempts) == 2
    assert not root.exists()
