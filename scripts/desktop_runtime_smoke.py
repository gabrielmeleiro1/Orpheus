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
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
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
class ConversionJourneyResult:
    attempted: bool
    completed: bool
    skipped: bool
    reason: str | None = None
    job_id: str | None = None
    book_id: int | None = None
    audio_bytes: int = 0


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class SmokeFailure(RuntimeError):
    """Raised when a live smoke check fails."""


CONVERSION_JOURNEY_TITLE = "Conversion Journey Smoke"


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
        "--app-bundle",
        type=Path,
        default=None,
        help=(
            "Packaged .app bundle whose embedded sidecar must be used. "
            "This is deterministic and never falls back to source staging."
        ),
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
    parser.add_argument(
        "--conversion-journey",
        action="store_true",
        help=(
            "Opt-in production-contract conversion journey: upload Markdown, "
            "preflight, and start a bounded conversion only when a local TTS "
            "engine is already available. Model resolution runs offline "
            "(HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE); cached weights are required. "
            "Skips the model-backed portion when required runtime is missing "
            "(does not download models)."
        ),
    )
    parser.add_argument(
        "--conversion-timeout-seconds",
        type=float,
        default=180.0,
        help="Timeout for the opt-in conversion journey terminal wait.",
    )
    parser.add_argument(
        "--probe-wait-seconds",
        type=float,
        default=25.0,
        help=(
            "Bounded wait for asynchronous TTS runtime_probe readiness during "
            "--conversion-journey. Polls capabilities only while the probe is "
            "pending/refreshing; does not download models."
        ),
    )
    return parser.parse_args(argv)


def resolve_default_sidecar_path() -> Path:
    if DEFAULT_APP_SIDECAR.is_file():
        return DEFAULT_APP_SIDECAR
    return DEFAULT_RESOURCE_SIDECAR


def resolve_packaged_app_sidecar(app_bundle: Path) -> Path:
    """Resolve only the sidecar embedded at the fixed Tauri app resource path."""
    requested = Path(app_bundle).expanduser()
    if requested.suffix != ".app":
        raise ValueError(f"packaged app bundle must have a .app suffix: {requested}")
    if not requested.exists():
        raise FileNotFoundError(f"packaged app bundle not found: {requested}")
    bundle = requested.resolve()
    if not bundle.is_dir():
        raise ValueError(f"packaged app bundle is not a directory: {requested}")

    sidecar = bundle / "Contents/Resources/binaries/orpheus-desktop-backend/orpheus-desktop-backend"
    if not sidecar.exists():
        raise FileNotFoundError(f"embedded packaged sidecar not found: {sidecar}")
    resolved_sidecar = sidecar.resolve()
    try:
        resolved_sidecar.relative_to(bundle)
    except ValueError as exc:
        raise ValueError(f"embedded packaged sidecar must stay inside app bundle: {sidecar}") from exc
    return _validate_runtime_binary(resolved_sidecar, "packaged sidecar")


def build_backend_command(
    *,
    backend: str,
    sidecar_path: Path | None,
    app_bundle: Path | None = None,
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

    resolved_sidecar = (
        resolve_packaged_app_sidecar(app_bundle)
        if app_bundle is not None
        else sidecar_path or resolve_default_sidecar_path()
    )
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


def build_child_env(
    runtime_paths: RuntimePaths,
    *,
    reduced_path: bool,
    backend: str,
    conversion_journey: bool = False,
) -> dict[str, str]:
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
    if conversion_journey:
        _ensure_offline_model_resolution_env(env)
    return env


def _ensure_offline_model_resolution_env(env: dict[str, str]) -> None:
    """Force Hugging Face/Transformers offline model resolution for conversion journey.

    Never weakens an already-enabled parent offline setting.
    """
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        current = str(env.get(key, "")).strip().lower()
        if current in {"1", "true", "yes", "on"}:
            continue
        env[key] = "1"


def _runtime_command_env(*, reduced_path: bool) -> dict[str, str]:
    env = os.environ.copy()
    if reduced_path:
        env["PATH"] = REDUCED_MACOS_PATH
    return env


def _validate_runtime_binary(path: Path, label: str) -> Path:
    if not path.is_file() or not os.access(path, os.X_OK):
        raise SmokeFailure(f"{label} runtime binary is not executable: {path}")
    return path


def _resolve_sidecar_runtime_binary(
    bin_dir: Path,
    binary: str,
    *,
    allowed_root: Path | None = None,
) -> Path:
    candidates = (bin_dir / binary, bin_dir / f"{binary}.exe")
    for candidate in candidates:
        if candidate.is_file():
            resolved = _validate_runtime_binary(candidate.resolve(), binary)
            if allowed_root is not None:
                try:
                    resolved.relative_to(allowed_root)
                except ValueError as exc:
                    raise ValueError(
                        f"{binary} runtime binary must stay inside app bundle: {candidate}"
                    ) from exc
            return resolved
    formatted = ", ".join(str(candidate) for candidate in candidates)
    raise SmokeFailure(f"{binary} runtime binary is not executable: {formatted}")


def resolve_ffmpeg_runtime_binaries(
    *,
    backend: str,
    sidecar_path: Path | None,
    app_bundle: Path | None = None,
) -> dict[str, Path]:
    if backend == "sidecar":
        resolved_sidecar = (
            resolve_packaged_app_sidecar(app_bundle)
            if app_bundle is not None
            else sidecar_path or resolve_default_sidecar_path()
        )
        if not resolved_sidecar.is_file():
            raise FileNotFoundError(f"sidecar executable not found: {resolved_sidecar}")
        bin_dir = resolved_sidecar.parent / "bin"
        app_root = app_bundle.expanduser().resolve() if app_bundle is not None else None
        return {
            "ffmpeg": _resolve_sidecar_runtime_binary(
                bin_dir,
                "ffmpeg",
                allowed_root=app_root,
            ),
            "ffprobe": _resolve_sidecar_runtime_binary(
                bin_dir,
                "ffprobe",
                allowed_root=app_root,
            ),
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
    app_bundle: Path | None = None,
    runtime_paths: RuntimePaths,
    timeout_seconds: float,
    reduced_path: bool,
) -> None:
    binaries = resolve_ffmpeg_runtime_binaries(
        backend=backend,
        sidecar_path=sidecar_path,
        app_bundle=app_bundle,
    )
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
    app_bundle: Path | None = None,
    python_executable: str,
    runtime_paths: RuntimePaths,
    timeout_seconds: float,
    reduced_path: bool,
    conversion_journey: bool = False,
) -> tuple[subprocess.Popen[str], ReadyPayload, list[str], list[str]]:
    command = build_backend_command(
        backend=backend,
        sidecar_path=sidecar_path,
        app_bundle=app_bundle,
        python_executable=python_executable,
    )
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=build_child_env(
            runtime_paths,
            reduced_path=reduced_path,
            backend=backend,
            conversion_journey=conversion_journey,
        ),
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


def _first_available_tts_engine(capabilities: dict[str, Any]) -> str | None:
    engines = capabilities.get("available_tts_engines")
    if isinstance(engines, list):
        for engine in engines:
            value = str(engine).strip()
            if value:
                return value
    default_engine = str(capabilities.get("default_tts_engine") or "").strip()
    return default_engine or None


class TtsProbeReadiness(str, Enum):
    READY = "ready"
    UNAVAILABLE = "unavailable"
    PENDING = "pending"


@dataclass(frozen=True)
class TtsProbeWaitResult:
    capabilities: dict[str, Any]
    readiness: TtsProbeReadiness
    reason: str | None = None


def local_tts_runtime_available(capabilities: dict[str, Any]) -> bool:
    """True only when an explicit runtime-probe/ready flag says TTS can run.

    ``available_tts_engines`` / ``default_tts_engine`` are factory catalog
    values and must never alone authorize a conversion (they can be present
    when models are not installed and would trigger downloads).
    """
    if capabilities.get("mlx_runtime_available") is True:
        return True

    probe = capabilities.get("runtime_probe")
    if isinstance(probe, dict):
        tts = probe.get("tts")
        if isinstance(tts, dict) and tts.get("available") is True:
            return True

    return False


def classify_tts_probe_readiness(capabilities: dict[str, Any]) -> TtsProbeReadiness:
    """Classify whether TTS is ready, definitively unavailable, or still probing."""
    if local_tts_runtime_available(capabilities):
        return TtsProbeReadiness.READY

    probe = capabilities.get("runtime_probe")
    if not isinstance(probe, dict):
        # Catalog/default fields alone are insufficient — treat as unavailable.
        return TtsProbeReadiness.UNAVAILABLE

    if probe.get("refresh_in_progress") is True:
        return TtsProbeReadiness.PENDING

    state = str(probe.get("state") or "").strip().lower()
    if state in {"pending", "missing"}:
        return TtsProbeReadiness.PENDING

    # fresh/stale (or unknown finished) without an explicit positive → unavailable
    return TtsProbeReadiness.UNAVAILABLE


def _tts_unavailable_reason(capabilities: dict[str, Any]) -> str:
    probe = capabilities.get("runtime_probe")
    detail = ""
    if isinstance(probe, dict):
        tts = probe.get("tts") if isinstance(probe.get("tts"), dict) else {}
        error = str((tts or {}).get("error") or "").strip()
        state = str(probe.get("state") or "").strip()
        if error:
            detail = f" (state={state or 'unknown'}, error={error})"
        elif state:
            detail = f" (state={state})"
    return f"TTS runtime probe finished unavailable{detail}; conversion portion skipped without downloading models"


def _tts_probe_timeout_reason(probe_wait_seconds: float, capabilities: dict[str, Any]) -> str:
    probe = capabilities.get("runtime_probe") if isinstance(capabilities, dict) else None
    state = ""
    refreshing = False
    if isinstance(probe, dict):
        state = str(probe.get("state") or "").strip()
        refreshing = bool(probe.get("refresh_in_progress"))
    detail = f" state={state or 'unknown'} refresh_in_progress={refreshing}"
    return (
        f"TTS runtime probe timed out / not ready after {probe_wait_seconds:.1f}s;"
        f"{detail}; conversion portion skipped without downloading models"
    )


def wait_for_tts_runtime_ready(
    fetch_capabilities: Callable[[], dict[str, Any]],
    *,
    probe_wait_seconds: float,
    poll_interval_seconds: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> TtsProbeWaitResult:
    """Wait only while the TTS runtime probe is pending/refreshing."""
    capabilities = fetch_capabilities()
    readiness = classify_tts_probe_readiness(capabilities)
    if readiness is TtsProbeReadiness.READY:
        return TtsProbeWaitResult(capabilities=capabilities, readiness=readiness)
    if readiness is TtsProbeReadiness.UNAVAILABLE:
        return TtsProbeWaitResult(
            capabilities=capabilities,
            readiness=readiness,
            reason=_tts_unavailable_reason(capabilities),
        )

    deadline = monotonic() + max(0.0, probe_wait_seconds)
    while monotonic() < deadline:
        sleep(max(0.0, poll_interval_seconds))
        capabilities = fetch_capabilities()
        readiness = classify_tts_probe_readiness(capabilities)
        if readiness is TtsProbeReadiness.READY:
            return TtsProbeWaitResult(capabilities=capabilities, readiness=readiness)
        if readiness is TtsProbeReadiness.UNAVAILABLE:
            return TtsProbeWaitResult(
                capabilities=capabilities,
                readiness=readiness,
                reason=_tts_unavailable_reason(capabilities),
            )

    return TtsProbeWaitResult(
        capabilities=capabilities,
        readiness=TtsProbeReadiness.PENDING,
        reason=_tts_probe_timeout_reason(probe_wait_seconds, capabilities),
    )


def wait_for_conversion_terminal(
    ready: ReadyPayload,
    *,
    job_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] = {}
    saw_running = False
    while time.monotonic() < deadline:
        active = expect_status(
            http_request(
                "GET",
                f"{ready.base_url}/api/conversions/active",
                token=ready.token,
                timeout_seconds=min(10.0, timeout_seconds),
            ),
            200,
            "active conversion poll",
        ).json()
        last_payload = active if isinstance(active, dict) else {}
        job = last_payload.get("job") if isinstance(last_payload.get("job"), dict) else None
        if job is not None:
            observed_id = str(job.get("id") or "").strip()
            if observed_id and observed_id != job_id:
                # Different job occupies the active slot; keep waiting for ours.
                time.sleep(0.25)
                continue
        status = str((job or {}).get("status") or "").strip().lower()
        if status == "running" or last_payload.get("active") is True:
            saw_running = True
        if job is not None and status in {"completed", "failed", "cancelled"}:
            book_id = _coerce_positive_book_id(job.get("book_id"))
            return {"status": status, "payload": last_payload, "book_id": book_id}
        if saw_running and last_payload.get("active") is False and job is None:
            # Job cleared from active slot after a run; treat as completed candidate
            # and let the library/audio checks confirm success via title/book_id.
            return {"status": "completed", "payload": last_payload, "book_id": None}
        time.sleep(0.25)
    raise SmokeFailure(
        f"conversion journey timed out after {timeout_seconds:.1f}s waiting for job {job_id}; "
        f"last payload={last_payload!r}"
    )


def _coerce_positive_book_id(raw: Any) -> int | None:
    try:
        book_id = int(raw)
    except (TypeError, ValueError):
        return None
    return book_id if book_id > 0 else None


def _find_library_item_by_book_id(items: list[Any], book_id: int) -> dict[str, Any] | None:
    for item in items:
        if not isinstance(item, dict):
            continue
        if _coerce_positive_book_id(item.get("id")) == book_id:
            return item
    return None


def _find_library_items_by_title(items: list[Any], title: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("title") or "").strip() == title:
            matches.append(item)
    return matches


def wait_for_conversion_journey_book(
    ready: ReadyPayload,
    *,
    book_id: int | None,
    expected_title: str,
    timeout_seconds: float,
) -> tuple[int, dict[str, Any]]:
    """Resolve the converted library item by terminal book_id or unique title."""
    deadline = time.monotonic() + timeout_seconds
    last_library: Any = None
    while time.monotonic() < deadline:
        library = expect_status(
            http_request(
                "GET",
                f"{ready.base_url}/api/library?limit=20",
                token=ready.token,
                timeout_seconds=min(10.0, timeout_seconds),
            ),
            200,
            "conversion journey library",
        ).json()
        last_library = library
        items = library.get("items") if isinstance(library, dict) else None
        if not isinstance(items, list):
            time.sleep(0.25)
            continue

        if book_id is not None:
            match = _find_library_item_by_book_id(items, book_id)
            if match is not None:
                title = str(match.get("title") or "").strip()
                if title != expected_title:
                    raise SmokeFailure(
                        f"library book id={book_id} title={title!r} does not match "
                        f"expected {expected_title!r}; library={library!r}"
                    )
                return book_id, match
        else:
            matches = _find_library_items_by_title(items, expected_title)
            if len(matches) == 1:
                resolved_id = _coerce_positive_book_id(matches[0].get("id"))
                if resolved_id is None:
                    raise SmokeFailure(
                        f"matching library title {expected_title!r} has invalid id: {matches[0]!r}"
                    )
                return resolved_id, matches[0]
            if len(matches) > 1:
                raise SmokeFailure(
                    f"ambiguous library title {expected_title!r}: {matches!r}"
                )
        time.sleep(0.25)

    raise SmokeFailure(
        f"could not establish conversion journey library identity "
        f"(book_id={book_id!r}, title={expected_title!r}); last library={last_library!r}"
    )


def run_conversion_journey(
    ready: ReadyPayload,
    *,
    timeout_seconds: float,
    conversion_timeout_seconds: float,
    probe_wait_seconds: float = 25.0,
) -> ConversionJourneyResult:
    """Upload → preflight → optional conversion → library/audio verification.

    Model-backed conversion runs only when capabilities expose an explicit TTS
    runtime-ready signal (after a bounded wait while the probe is pending).
    Otherwise the production-contract portion up through preflight still runs
    and conversion is explicitly skipped (not a fake success).
    """
    text_payload = json.dumps(
        {
            "title": CONVERSION_JOURNEY_TITLE,
            "text": (
                "# Conversion Journey\n\n"
                "A short deterministic document for the opt-in smoke journey.\n"
            ),
        }
    ).encode("utf-8")
    upload = expect_status(
        http_request(
            "POST",
            f"{ready.base_url}/api/uploads/text",
            token=ready.token,
            data=text_payload,
            content_type="application/json",
            timeout_seconds=timeout_seconds,
        ),
        200,
        "conversion journey text upload",
    ).json()
    source_handle = str(upload.get("source_handle") or "").strip()
    if not source_handle.startswith("upload:"):
        raise SmokeFailure(f"unexpected conversion journey upload payload: {upload!r}")

    preflight = expect_status(
        http_request(
            "POST",
            f"{ready.base_url}/api/sources/preflight",
            token=ready.token,
            data=json.dumps({"source_path": source_handle}).encode("utf-8"),
            content_type="application/json",
            timeout_seconds=timeout_seconds,
        ),
        200,
        "conversion journey preflight",
    ).json()
    if int(preflight.get("chapter_count") or 0) < 1:
        raise SmokeFailure(f"preflight returned no chapters: {preflight!r}")

    def fetch_capabilities() -> dict[str, Any]:
        payload = expect_status(
            http_request(
                "GET",
                f"{ready.base_url}/api/system/capabilities",
                token=ready.token,
                timeout_seconds=timeout_seconds,
            ),
            200,
            "conversion journey capabilities",
        ).json()
        if not isinstance(payload, dict):
            raise SmokeFailure(f"unexpected capabilities payload: {payload!r}")
        return payload

    probe_wait = wait_for_tts_runtime_ready(
        fetch_capabilities,
        probe_wait_seconds=probe_wait_seconds,
    )
    capabilities = probe_wait.capabilities
    if probe_wait.readiness is not TtsProbeReadiness.READY:
        return ConversionJourneyResult(
            attempted=False,
            completed=False,
            skipped=True,
            reason=probe_wait.reason
            or "TTS runtime probe not ready; conversion portion skipped without downloading models",
        )

    engine = _first_available_tts_engine(capabilities) or "kokoro"
    model_options = expect_status(
        http_request(
            "GET",
            f"{ready.base_url}/api/options/models",
            token=ready.token,
            timeout_seconds=timeout_seconds,
        ),
        200,
        "conversion journey model options",
    ).json()
    voice = "am_adam"
    engines = model_options.get("tts_engines") if isinstance(model_options, dict) else None
    if isinstance(engines, list):
        for item in engines:
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "").strip() != engine:
                continue
            voices = item.get("voices")
            if isinstance(voices, list) and voices:
                first = voices[0]
                if isinstance(first, dict):
                    candidate = str(first.get("id") or "").strip()
                else:
                    candidate = str(first).strip()
                if candidate:
                    voice = candidate
                    break

    start_payload = {
        "source_path": source_handle,
        "conversion_mode": "document_audio",
        "title": CONVERSION_JOURNEY_TITLE,
        "runtime_options": {
            "voice": voice,
            "speed": 1.0,
            "tts_engine": engine,
            "parallel_worker_mode": "manual",
            "enable_parallel": False,
            "max_parallel_chapters": 1,
        },
    }
    started = expect_status(
        http_request(
            "POST",
            f"{ready.base_url}/api/conversions",
            token=ready.token,
            data=json.dumps(start_payload).encode("utf-8"),
            content_type="application/json",
            timeout_seconds=timeout_seconds,
        ),
        200,
        "conversion journey start",
    ).json()
    job_id = str(started.get("job_id") or "").strip()
    if not job_id:
        raise SmokeFailure(f"conversion start missing job_id: {started!r}")

    terminal = wait_for_conversion_terminal(
        ready,
        job_id=job_id,
        timeout_seconds=conversion_timeout_seconds,
    )
    status = str(terminal.get("status") or "").lower()
    if status != "completed":
        raise SmokeFailure(f"conversion journey ended with status={status!r}: {terminal!r}")

    book_id, _book = wait_for_conversion_journey_book(
        ready,
        book_id=_coerce_positive_book_id(terminal.get("book_id")),
        expected_title=CONVERSION_JOURNEY_TITLE,
        timeout_seconds=min(30.0, max(5.0, conversion_timeout_seconds)),
    )

    audio_token = fetch_desktop_query_token(
        ready,
        scope="audio",
        timeout_seconds=timeout_seconds,
        book_id=book_id,
    )
    audio = expect_status(
        http_request(
            "GET",
            f"{ready.base_url}/api/library/{book_id}/audio?{urlencode({'token': audio_token})}",
            timeout_seconds=timeout_seconds,
        ),
        200,
        "conversion journey audio",
    )
    if not audio.body:
        raise SmokeFailure("conversion journey audio response was empty")

    return ConversionJourneyResult(
        attempted=True,
        completed=True,
        skipped=False,
        job_id=job_id,
        book_id=book_id,
        audio_bytes=len(audio.body),
    )


def run_iteration(
    index: int,
    *,
    backend: str,
    sidecar_path: Path | None,
    app_bundle: Path | None = None,
    python_executable: str,
    request_count: int,
    timeout_seconds: float,
    reduced_path: bool,
    skip_sse: bool,
    check_ffmpeg_runtime: bool = False,
    conversion_journey: bool = False,
    conversion_timeout_seconds: float = 180.0,
    probe_wait_seconds: float = 25.0,
) -> IterationResult:
    start = time.monotonic()
    runtime_paths = RuntimePaths.create()
    process: subprocess.Popen[str] | None = None
    try:
        ready_started = time.monotonic()
        process, ready, _stdout, _stderr = start_backend(
            backend=backend,
            sidecar_path=sidecar_path,
            app_bundle=app_bundle,
            python_executable=python_executable,
            runtime_paths=runtime_paths,
            timeout_seconds=timeout_seconds,
            reduced_path=reduced_path,
            conversion_journey=conversion_journey,
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
                app_bundle=app_bundle,
                runtime_paths=runtime_paths,
                timeout_seconds=timeout_seconds,
                reduced_path=reduced_path,
            )
        if conversion_journey:
            journey = run_conversion_journey(
                ready,
                timeout_seconds=timeout_seconds,
                conversion_timeout_seconds=conversion_timeout_seconds,
                probe_wait_seconds=probe_wait_seconds,
            )
            if journey.skipped:
                print(f"  conversion journey skipped: {journey.reason}")
            elif journey.completed:
                print(
                    "  conversion journey completed: "
                    f"job={journey.job_id} book={journey.book_id} "
                    f"audio_bytes={journey.audio_bytes}"
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
    if args.app_bundle is not None and args.backend != "sidecar":
        raise ValueError("--app-bundle requires --backend sidecar")
    if args.iterations < 1:
        raise ValueError("--iterations must be at least 1")
    if args.requests < 1:
        raise ValueError("--requests must be at least 1")
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")
    if args.conversion_timeout_seconds <= 0:
        raise ValueError("--conversion-timeout-seconds must be positive")
    if args.probe_wait_seconds < 0:
        raise ValueError("--probe-wait-seconds must be non-negative")
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
        f"ffmpeg_runtime={args.check_ffmpeg_runtime} "
        f"conversion_journey={args.conversion_journey} "
        f"probe_wait={args.probe_wait_seconds:.1f}s"
    )
    results: list[IterationResult] = []
    for index in range(1, args.iterations + 1):
        result = run_iteration(
            index,
            backend=args.backend,
            sidecar_path=args.sidecar_path,
            app_bundle=args.app_bundle,
            python_executable=sys.executable,
            request_count=args.requests,
            timeout_seconds=args.timeout_seconds,
            reduced_path=args.reduced_path,
            skip_sse=args.skip_sse,
            check_ffmpeg_runtime=args.check_ffmpeg_runtime,
            conversion_journey=args.conversion_journey,
            conversion_timeout_seconds=args.conversion_timeout_seconds,
            probe_wait_seconds=args.probe_wait_seconds,
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
