"""Tests for the desktop runtime smoke harness."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

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


def test_parse_args_defaults_to_quick_python_smoke() -> None:
    args = desktop_runtime_smoke.parse_args([])

    assert args.backend == "python"
    assert args.iterations == 1
    assert args.requests == 3
    assert args.timeout_seconds == 30.0
    assert args.min_success_rate == 1.0
    assert not args.reduced_path
    assert not args.check_ffmpeg_runtime


def test_parse_args_accepts_opt_in_ffmpeg_runtime_check() -> None:
    args = desktop_runtime_smoke.parse_args(["--check-ffmpeg-runtime"])

    assert args.check_ffmpeg_runtime is True


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
