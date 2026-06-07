#!/usr/bin/env python3
"""Live desktop backend smoke and bounded batch harness."""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_APP_SIDECAR = (
    REPO_ROOT
    / "desktop"
    / "src-tauri"
    / "target"
    / "release"
    / "bundle"
    / "macos"
    / "Orpheus.app"
    / "Contents"
    / "Resources"
    / "binaries"
    / "orpheus-desktop-backend"
    / "orpheus-desktop-backend"
)
DEFAULT_RESOURCE_SIDECAR = (
    REPO_ROOT
    / "desktop"
    / "src-tauri"
    / "binaries"
    / "orpheus-desktop-backend"
    / "orpheus-desktop-backend"
)
REDUCED_MACOS_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


@dataclass(frozen=True)
class ReadyPayload:
    host: str
    port: int
    token: str

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    data_dir: Path
    output_dir: Path
    temp_dir: Path
    db_path: Path
    ready_file: Path

    @classmethod
    def create(cls) -> "RuntimePaths":
        root = Path(tempfile.mkdtemp(prefix="audiobook-runtime-smoke-"))
        data_dir = root / "data"
        return cls(
            root=root,
            data_dir=data_dir,
            output_dir=root / "output",
            temp_dir=root / "temp",
            db_path=data_dir / "audiobooks.db",
            ready_file=root / "backend-ready.json",
        )

    def as_env(self) -> dict[str, str]:
        return {
            "AUDIOBOOK_DATA_DIR": str(self.data_dir),
            "AUDIOBOOK_OUTPUT_DIR": str(self.output_dir),
            "AUDIOBOOK_TEMP_DIR": str(self.temp_dir),
            "AUDIOBOOK_DB_PATH": str(self.db_path),
            "AUDIOBOOK_DESKTOP_READY_FILE": str(self.ready_file),
        }


@dataclass(frozen=True)
class IterationResult:
    index: int
    passed: bool
    duration_seconds: float
    ready_seconds: float | None = None
    warm_library_seconds: float | None = None
    authenticated_library_requests: int = 0
    error: str | None = None


@dataclass(frozen=True)
class ApiCheckMetrics:
    warm_library_seconds: float
    authenticated_library_requests: int


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class SmokeFailure(RuntimeError):
    """Raised when a live smoke check fails."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend",
        choices=("python", "sidecar"),
        default="python",
        help="Backend launcher to exercise.",
    )
    parser.add_argument(
        "--sidecar-path",
        type=Path,
        default=None,
        help="Packaged sidecar executable to use when --backend=sidecar.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="Number of fresh startup/shutdown iterations.",
    )
    parser.add_argument(
        "--requests",
        type=int,
        default=3,
        help="Authenticated /api/library requests per iteration.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="Per-operation timeout.",
    )
    parser.add_argument(
        "--min-success-rate",
        type=float,
        default=1.0,
        help="Required iteration success rate from 0.0 to 1.0.",
    )
    parser.add_argument(
        "--reduced-path",
        action="store_true",
        help="Run the child with a Finder-like reduced macOS PATH.",
    )
    parser.add_argument(
        "--skip-sse",
        action="store_true",
        help="Skip the SSE heartbeat check.",
    )
    parser.add_argument(
        "--check-ffmpeg-runtime",
        action="store_true",
        help=(
            "Run a lightweight real ffmpeg/ffprobe check in the smoke runtime. "
            "This does not start an ML conversion."
        ),
    )
    return parser.parse_args(argv)


def resolve_default_sidecar_path() -> Path:
    if DEFAULT_APP_SIDECAR.is_file():
        return DEFAULT_APP_SIDECAR
    return DEFAULT_RESOURCE_SIDECAR


def build_backend_command(
    *,
    backend: str,
    sidecar_path: Path | None,
    python_executable: str,
) -> list[str]:
    if backend == "python":
        return [
            python_executable,
            "-m",
            "apps.launchers.desktop_backend",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
        ]

    resolved_sidecar = sidecar_path or resolve_default_sidecar_path()
    if not resolved_sidecar.is_file():
        raise FileNotFoundError(f"sidecar executable not found: {resolved_sidecar}")
    return [str(resolved_sidecar), "--host", "127.0.0.1", "--port", "0"]


def success_rate(results: list[IterationResult]) -> float:
    if not results:
        return 0.0
    return sum(1 for result in results if result.passed) / len(results)


def results_meet_threshold(results: list[IterationResult], threshold: float) -> bool:
    return success_rate(results) >= threshold


def encode_multipart_file(
    *,
    field_name: str,
    file_name: str,
    content: bytes,
    content_type: str,
) -> tuple[bytes, str]:
    boundary = f"----audiobook-smoke-{uuid.uuid4().hex}"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("ascii"),
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{file_name}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode("ascii"),
        ]
    )
    return body, f"multipart/form-data; boundary={boundary}"


def _reader_thread(stream: Any, output: list[str], lines: queue.Queue[str]) -> None:
    for raw_line in iter(stream.readline, ""):
        line = raw_line.rstrip("\n")
        output.append(line)
        lines.put(line)


def _parse_ready_payload(raw: dict[str, Any]) -> ReadyPayload:
    if raw.get("event") != "ready":
        raise SmokeFailure(f"unexpected ready event: {raw!r}")
    host = str(raw.get("host", ""))
    port = raw.get("port")
    token = str(raw.get("token", ""))
    if host not in {"127.0.0.1", "localhost"}:
        raise SmokeFailure(f"unexpected ready host: {host!r}")
    if not isinstance(port, int) or port <= 0:
        raise SmokeFailure(f"unexpected ready port: {port!r}")
    if not token:
        raise SmokeFailure("ready payload did not include a token")
    return ReadyPayload(host=host, port=port, token=token)


def _load_ready_file(path: Path) -> ReadyPayload | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    return _parse_ready_payload(json.loads(path.read_text(encoding="utf-8")))


def _format_process_output(stdout: list[str], stderr: list[str]) -> str:
    recent_stdout = "\n".join(stdout[-40:])
    recent_stderr = "\n".join(stderr[-40:])
    return f"stdout:\n{recent_stdout}\nstderr:\n{recent_stderr}"


def build_child_env(runtime_paths: RuntimePaths, *, reduced_path: bool, backend: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(runtime_paths.as_env())
    if backend == "python":
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            f"{REPO_ROOT}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else str(REPO_ROOT)
        )
    if reduced_path:
        env["PATH"] = REDUCED_MACOS_PATH
    return env


def _runtime_command_env(*, reduced_path: bool) -> dict[str, str]:
    env = os.environ.copy()
    if reduced_path:
        env["PATH"] = REDUCED_MACOS_PATH
    return env


def _validate_runtime_binary(path: Path, label: str) -> Path:
    if not path.is_file() or not os.access(path, os.X_OK):
        raise SmokeFailure(f"{label} runtime binary is not executable: {path}")
    return path


def resolve_ffmpeg_runtime_binaries(
    *,
    backend: str,
    sidecar_path: Path | None,
) -> dict[str, Path]:
    if backend == "sidecar":
        resolved_sidecar = sidecar_path or resolve_default_sidecar_path()
        if not resolved_sidecar.is_file():
            raise FileNotFoundError(f"sidecar executable not found: {resolved_sidecar}")
        bin_dir = resolved_sidecar.parent / "bin"
        return {
            "ffmpeg": _validate_runtime_binary(bin_dir / "ffmpeg", "ffmpeg"),
            "ffprobe": _validate_runtime_binary(bin_dir / "ffprobe", "ffprobe"),
        }

    from modules.audio.ffmpeg import resolve_ffmpeg_binary

    resolved: dict[str, Path] = {}
    for binary in ("ffmpeg", "ffprobe"):
        binary_path = resolve_ffmpeg_binary(binary)
        if binary_path is None:
            raise SmokeFailure(f"{binary} runtime binary could not be resolved")
        resolved[binary] = _validate_runtime_binary(Path(binary_path), binary)
    return resolved


def _run_runtime_command(
    command: list[str],
    *,
    env: dict[str, str],
    timeout_seconds: float,
    label: str,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmokeFailure(f"{label} timed out after {timeout_seconds:.1f}s") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise SmokeFailure(f"{label} failed with code {completed.returncode}: {stderr}")
    return completed


def run_ffmpeg_runtime_check(
    *,
    backend: str,
    sidecar_path: Path | None,
    runtime_paths: RuntimePaths,
    timeout_seconds: float,
    reduced_path: bool,
) -> None:
    binaries = resolve_ffmpeg_runtime_binaries(backend=backend, sidecar_path=sidecar_path)
    runtime_paths.temp_dir.mkdir(parents=True, exist_ok=True)
    probe_output = runtime_paths.temp_dir / "ffmpeg-runtime-check.wav"
    env = _runtime_command_env(reduced_path=reduced_path)

    _run_runtime_command(
        [
            str(binaries["ffmpeg"]),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=8000:cl=mono",
            "-t",
            "0.02",
            "-f",
            "wav",
            str(probe_output),
        ],
        env=env,
        timeout_seconds=timeout_seconds,
        label="ffmpeg runtime check",
    )
    if not probe_output.is_file():
        raise SmokeFailure(f"ffmpeg runtime check did not create {probe_output}")

    probe = _run_runtime_command(
        [
            str(binaries["ffprobe"]),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_entries",
            "format=duration",
            str(probe_output),
        ],
        env=env,
        timeout_seconds=timeout_seconds,
        label="ffprobe runtime check",
    )
    try:
        payload = json.loads(probe.stdout)
        duration = float(payload["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SmokeFailure(f"ffprobe runtime check returned unexpected output: {probe.stdout}") from exc
    if duration <= 0:
        raise SmokeFailure(f"ffprobe runtime check returned non-positive duration: {duration}")


def start_backend(
    *,
    backend: str,
    sidecar_path: Path | None,
    python_executable: str,
    runtime_paths: RuntimePaths,
    timeout_seconds: float,
    reduced_path: bool,
) -> tuple[subprocess.Popen[str], ReadyPayload, list[str], list[str]]:
    command = build_backend_command(
        backend=backend,
        sidecar_path=sidecar_path,
        python_executable=python_executable,
    )
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=build_child_env(runtime_paths, reduced_path=reduced_path, backend=backend),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout: list[str] = []
    stderr: list[str] = []
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_reader_thread, args=(process.stdout, stdout, lines), daemon=True).start()
    threading.Thread(target=_reader_thread, args=(process.stderr, stderr, queue.Queue()), daemon=True).start()

    deadline = time.monotonic() + timeout_seconds
    last_decode_error: str | None = None
    while time.monotonic() < deadline:
        while True:
            try:
                line = lines.get_nowait()
            except queue.Empty:
                break
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                last_decode_error = str(exc)
                continue
            return process, _parse_ready_payload(parsed), stdout, stderr

        ready_from_file = _load_ready_file(runtime_paths.ready_file)
        if ready_from_file is not None:
            return process, ready_from_file, stdout, stderr

        return_code = process.poll()
        if return_code is not None:
            detail = _format_process_output(stdout, stderr)
            raise SmokeFailure(f"backend exited before ready, code={return_code}\n{detail}")
        time.sleep(0.05)

    detail = _format_process_output(stdout, stderr)
    if last_decode_error:
        detail = f"last JSON decode error: {last_decode_error}\n{detail}"
    raise SmokeFailure(f"timed out waiting for ready payload after {timeout_seconds:.1f}s\n{detail}")


def stop_backend(process: subprocess.Popen[str], timeout_seconds: float = 5.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout_seconds)


def http_request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    data: bytes | None = None,
    content_type: str | None = None,
    timeout_seconds: float,
) -> HttpResult:
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if content_type is not None:
        headers["Content-Type"] = content_type
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return HttpResult(status=response.status, body=response.read())
    except HTTPError as exc:
        return HttpResult(status=exc.code, body=exc.read())
    except URLError as exc:
        raise SmokeFailure(f"{method} {url} failed: {exc}") from exc


def expect_status(result: HttpResult, expected: int, label: str) -> HttpResult:
    if result.status != expected:
        body = result.body.decode("utf-8", errors="replace")
        raise SmokeFailure(f"{label} returned HTTP {result.status}, expected {expected}: {body}")
    return result


def fetch_desktop_query_token(
    ready: ReadyPayload,
    *,
    scope: str,
    timeout_seconds: float,
    book_id: int | None = None,
) -> str:
    payload: dict[str, object] = {"scope": scope}
    if book_id is not None:
        payload["book_id"] = book_id

    response = expect_status(
        http_request(
            "POST",
            f"{ready.base_url}/api/desktop/query-token",
            token=ready.token,
            data=json.dumps(payload).encode("utf-8"),
            content_type="application/json",
            timeout_seconds=timeout_seconds,
        ),
        200,
        f"{scope} query token",
    ).json()
    query_token = str(response.get("token", "")).strip()
    if not query_token:
        raise SmokeFailure(f"{scope} query token payload did not include a token")
    return query_token


def check_sse_ping(ready: ReadyPayload, timeout_seconds: float) -> None:
    query_token = fetch_desktop_query_token(
        ready,
        scope="events",
        timeout_seconds=timeout_seconds,
    )
    url = f"{ready.base_url}/api/events/stream?{urlencode({'token': query_token})}"
    request = Request(url, method="GET")
    deadline = time.monotonic() + timeout_seconds
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise SmokeFailure(f"SSE returned HTTP {response.status}")
            lines: list[str] = []
            while time.monotonic() < deadline:
                raw_line = response.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    lines.append(line)
                if line == "event: ping" or line.startswith("data:"):
                    return
    except (HTTPError, URLError) as exc:
        raise SmokeFailure(f"SSE check failed: {exc}") from exc
    raise SmokeFailure("SSE did not produce a heartbeat frame before timeout")


def run_api_checks(
    ready: ReadyPayload,
    *,
    request_count: int,
    timeout_seconds: float,
    skip_sse: bool,
) -> ApiCheckMetrics:
    expect_status(
        http_request("GET", f"{ready.base_url}/health", timeout_seconds=timeout_seconds),
        200,
        "health",
    )
    expect_status(
        http_request("GET", f"{ready.base_url}/api/library", timeout_seconds=timeout_seconds),
        401,
        "unauthenticated library",
    )

    library_started = time.monotonic()
    for _ in range(request_count):
        library = expect_status(
            http_request(
                "GET",
                f"{ready.base_url}/api/library?limit=1",
                token=ready.token,
                timeout_seconds=timeout_seconds,
            ),
            200,
            "authenticated library",
        ).json()
        if "items" not in library or "count" not in library:
            raise SmokeFailure(f"unexpected library payload: {library!r}")
    warm_library_seconds = time.monotonic() - library_started

    text_payload = json.dumps(
        {"title": "Runtime Smoke", "text": "A short local smoke document."}
    ).encode("utf-8")
    text_upload = expect_status(
        http_request(
            "POST",
            f"{ready.base_url}/api/uploads/text",
            token=ready.token,
            data=text_payload,
            content_type="application/json",
            timeout_seconds=timeout_seconds,
        ),
        200,
        "text upload",
    ).json()
    if not str(text_upload.get("source_handle", "")).startswith("upload:"):
        raise SmokeFailure(f"unexpected text upload payload: {text_upload!r}")

    source_body, source_content_type = encode_multipart_file(
        field_name="file",
        file_name="runtime-smoke.md",
        content=b"# Runtime Smoke\n\nA short local smoke document.\n",
        content_type="text/markdown",
    )
    source_upload = expect_status(
        http_request(
            "POST",
            f"{ready.base_url}/api/uploads/source",
            token=ready.token,
            data=source_body,
            content_type=source_content_type,
            timeout_seconds=timeout_seconds,
        ),
        200,
        "source upload",
    ).json()
    if not str(source_upload.get("source_handle", "")).startswith("upload:"):
        raise SmokeFailure(f"unexpected source upload payload: {source_upload!r}")

    invalid_conversion = json.dumps({"source_path": "upload:missing-smoke-source.md"}).encode(
        "utf-8"
    )
    expect_status(
        http_request(
            "POST",
            f"{ready.base_url}/api/conversions",
            token=ready.token,
            data=invalid_conversion,
            content_type="application/json",
            timeout_seconds=timeout_seconds,
        ),
        400,
        "invalid conversion start",
    )
    active = expect_status(
        http_request(
            "GET",
            f"{ready.base_url}/api/conversions/active",
            token=ready.token,
            timeout_seconds=timeout_seconds,
        ),
        200,
        "active conversion",
    ).json()
    if active.get("active") is not False:
        raise SmokeFailure(f"unexpected active conversion payload: {active!r}")

    if not skip_sse:
        check_sse_ping(ready, timeout_seconds=timeout_seconds)
    return ApiCheckMetrics(
        warm_library_seconds=warm_library_seconds,
        authenticated_library_requests=request_count,
    )


def run_iteration(
    index: int,
    *,
    backend: str,
    sidecar_path: Path | None,
    python_executable: str,
    request_count: int,
    timeout_seconds: float,
    reduced_path: bool,
    skip_sse: bool,
    check_ffmpeg_runtime: bool = False,
) -> IterationResult:
    start = time.monotonic()
    runtime_paths = RuntimePaths.create()
    process: subprocess.Popen[str] | None = None
    try:
        ready_started = time.monotonic()
        process, ready, _stdout, _stderr = start_backend(
            backend=backend,
            sidecar_path=sidecar_path,
            python_executable=python_executable,
            runtime_paths=runtime_paths,
            timeout_seconds=timeout_seconds,
            reduced_path=reduced_path,
        )
        ready_seconds = time.monotonic() - ready_started
        api_metrics = run_api_checks(
            ready,
            request_count=request_count,
            timeout_seconds=timeout_seconds,
            skip_sse=skip_sse,
        )
        if check_ffmpeg_runtime:
            run_ffmpeg_runtime_check(
                backend=backend,
                sidecar_path=sidecar_path,
                runtime_paths=runtime_paths,
                timeout_seconds=timeout_seconds,
                reduced_path=reduced_path,
            )
        return IterationResult(
            index=index,
            passed=True,
            duration_seconds=time.monotonic() - start,
            ready_seconds=ready_seconds,
            warm_library_seconds=api_metrics.warm_library_seconds,
            authenticated_library_requests=api_metrics.authenticated_library_requests,
        )
    except Exception as exc:  # noqa: BLE001
        return IterationResult(
            index=index,
            passed=False,
            duration_seconds=time.monotonic() - start,
            error=str(exc),
        )
    finally:
        if process is not None:
            stop_backend(process)
        shutil.rmtree(runtime_paths.root, ignore_errors=True)


def validate_args(args: argparse.Namespace) -> None:
    if args.iterations < 1:
        raise ValueError("--iterations must be at least 1")
    if args.requests < 1:
        raise ValueError("--requests must be at least 1")
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")
    if not 0.0 <= args.min_success_rate <= 1.0:
        raise ValueError("--min-success-rate must be between 0.0 and 1.0")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_args(args)
    except ValueError as exc:
        print(f"argument error: {exc}", file=sys.stderr)
        return 2

    print(
        "desktop runtime smoke: "
        f"backend={args.backend} iterations={args.iterations} "
        f"requests={args.requests} timeout={args.timeout_seconds:.1f}s "
        f"ffmpeg_runtime={args.check_ffmpeg_runtime}"
    )
    results: list[IterationResult] = []
    for index in range(1, args.iterations + 1):
        result = run_iteration(
            index,
            backend=args.backend,
            sidecar_path=args.sidecar_path,
            python_executable=sys.executable,
            request_count=args.requests,
            timeout_seconds=args.timeout_seconds,
            reduced_path=args.reduced_path,
            skip_sse=args.skip_sse,
            check_ffmpeg_runtime=args.check_ffmpeg_runtime,
        )
        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        timing_parts = [f"total={result.duration_seconds:.2f}s"]
        if result.ready_seconds is not None:
            timing_parts.append(f"ready={result.ready_seconds:.2f}s")
        if result.warm_library_seconds is not None:
            timing_parts.append(
                "warm_library="
                f"{result.warm_library_seconds:.2f}s/{result.authenticated_library_requests} req"
            )
        print(f"iteration {result.index}: {status} ({', '.join(timing_parts)})")
        if result.error:
            print(f"  error: {result.error}")

    rate = success_rate(results)
    passed = sum(1 for result in results if result.passed)
    print(
        "summary: "
        f"passed={passed} failed={len(results) - passed} "
        f"success_rate={rate:.2%} threshold={args.min_success_rate:.2%}"
    )
    return 0 if results_meet_threshold(results, args.min_success_rate) else 1


if __name__ == "__main__":
    raise SystemExit(main())
