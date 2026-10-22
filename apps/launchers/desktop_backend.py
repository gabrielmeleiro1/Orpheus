#!/usr/bin/env python3
"""Desktop backend launcher implementation."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import ipaddress
import json
import multiprocessing
import os
import secrets
import shutil
import signal
import socket
import sys
import tempfile
import threading
from collections.abc import Callable, Generator
from pathlib import Path
from typing import BinaryIO, Sequence

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

from modules.hf_cache import configure_hf_cache  # noqa: E402

configure_hf_cache()

import uvicorn  # noqa: E402
import psutil  # noqa: E402
from uvicorn.server import HANDLED_SIGNALS  # noqa: E402

from modules.app import AppConfig, AppController  # noqa: E402
from modules.storage.db_ownership import (  # noqa: E402
    DatabaseOwnership, acquire_database, open_process_lock, time as time,
)
from modules.web.server import create_app  # noqa: E402

# libespeak-ng POSIX path_home buffer (speech.h N_PATH_HOME).
# Limit is filesystem bytes (C char buffer), not Python string code points.
_LIBESPEAK_PATH_HOME_LIMIT = 160
_SUPPORTED_RUNTIME_PROFILES = frozenset({"base", "mlx", "cuda"})
_RUNTIME_PROFILE_MARKER = "runtime-profile.json"
_library_lock: DatabaseOwnership | None = None

# Upper bound on how long Uvicorn will wait for active connections to drain on
# shutdown. The persistent SSE stream is closed proactively on signal (see
# `_DesktopReadyServer.handle_exit`), so this is only a backstop that keeps
# desktop close bounded even if a client connection lingers.
DESKTOP_GRACEFUL_SHUTDOWN_TIMEOUT = 3.0


def _open_process_lock(path: Path, *, blocking: bool) -> BinaryIO:
    return open_process_lock(path, blocking=blocking, windows=os.name == "nt")


def _exit_orphaned_backend(code: int) -> None:
    try:
        descendants = psutil.Process().children(recursive=True)
        for child in descendants:
            with contextlib.suppress(psutil.Error):
                child.terminate()
        _, alive = psutil.wait_procs(descendants, timeout=1.0)
        for child in alive:
            with contextlib.suppress(psutil.Error):
                child.kill()
        psutil.wait_procs(alive, timeout=0.5)
    finally:
        os._exit(code)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _watch_desktop_parent(
    expected_pid: int,
    request_shutdown: Callable[[], None],
    stop: threading.Event,
    *,
    getppid: Callable[[], int] = os.getppid,
    pid_exists: Callable[[int], bool] = _pid_exists,
) -> None:
    while not stop.wait(1.0):
        if getppid() != expected_pid or not pid_exists(expected_pid):
            # Arm before cleanup so a stuck callback cannot prevent the fallback.
            # Keep it armed through interpreter teardown and multiprocessing joins.
            fallback = threading.Timer(8.0, _exit_orphaned_backend, args=(1,))
            fallback.daemon = True
            fallback.start()
            request_shutdown()
            return


def _start_desktop_parent_watchdog(
    request_shutdown: Callable[[], None],
) -> threading.Event | None:
    if sys.platform == "win32":
        return None
    try:
        expected_pid = int(os.environ.get("ORPHEUS_DESKTOP_PARENT_PID", ""))
    except ValueError:
        return None
    if expected_pid <= 0:
        return None
    stop = threading.Event()
    threading.Thread(
        target=_watch_desktop_parent, args=(expected_pid, request_shutdown, stop), daemon=True
    ).start()
    return stop


def _path_filesystem_byte_length(path: Path | str) -> int:
    """Return path length in filesystem-encoded bytes (libespeak buffer units)."""
    return len(os.fsencode(os.fspath(path)))


def _is_native_safe_espeak_path(path: Path, *, platform: str | None = None) -> bool:
    platform = sys.platform if platform is None else platform
    return (
        _path_filesystem_byte_length(path) < _LIBESPEAK_PATH_HOME_LIMIT
        and (platform != "win32" or str(path).isascii())
    )


def _espeak_runtime_base() -> Path:
    """Short per-user cache dir for the espeak-ng-data copy."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "Orpheus" / "espeak"
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        return Path(xdg_cache) / "orpheus" / "espeak"
    return Path.home() / ".cache" / "orpheus" / "espeak"


def _ensure_short_espeak_data_path(packaged_data: Path) -> Path:
    """Expose packaged speech data through a path the native library can read."""
    packaged_data = packaged_data.resolve()
    if sys.platform == "win32":
        return _windows_espeak_data_path(packaged_data)
    if _is_native_safe_espeak_path(packaged_data):
        return packaged_data

    manifest = _espeak_data_manifest(packaged_data)
    fingerprint = hashlib.sha256()
    for path, (size, digest) in sorted(manifest.items()):
        fingerprint.update(f"{path}:{size}:{digest}\n".encode("utf-8"))
    version = fingerprint.hexdigest()[:12]
    user_key = hashlib.sha256(os.fsencode(Path.home())).hexdigest()[:12]
    candidates = (
        _espeak_runtime_base() / version / "espeak-ng-data",
        Path(tempfile.gettempdir()) / f"orpheus-espeak-{user_key}" / version / "espeak-ng-data",
    )
    for target in candidates:
        try:
            if not _is_native_safe_espeak_path(target.resolve()):
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _publish_espeak_data(packaged_data, target, manifest)
            resolved = target.resolve()
            if (
                _is_native_safe_espeak_path(resolved)
                and (resolved / "phontab").is_file()
                and _espeak_data_matches(target, manifest)
            ):
                return resolved
        except OSError:
            continue
    raise RuntimeError("Speech data needs a writable short cache path to enable speech.")


def _espeak_data_manifest(root: Path) -> dict[Path, tuple[int, str]]:
    manifest = {}
    pending = [root]
    while pending:
        path = pending.pop()
        if path.is_symlink() or path.is_junction():
            raise OSError("Speech data must not contain links")
        if path.is_dir():
            pending.extend(path.iterdir())
        elif path.is_file():
            with path.open("rb") as handle:
                manifest[path.relative_to(root)] = (
                    os.fstat(handle.fileno()).st_size,
                    hashlib.file_digest(handle, "sha256").hexdigest(),
                )
        else:
            raise OSError(f"Invalid speech data entry: {path}")
    return manifest


def _espeak_data_matches(root: Path, expected: dict[Path, tuple[int, str]]) -> bool:
    try:
        return _espeak_data_manifest(root) == expected
    except OSError:
        return False


def _publish_espeak_data(
    packaged_data: Path, target: Path, manifest: dict[Path, tuple[int, str]],
) -> None:
    # Keep the lock file in place so every launcher locks the same inode.
    with _open_process_lock(target.parent / ".publish.lock", blocking=True):
        if _espeak_data_matches(target, manifest):
            return
        with tempfile.TemporaryDirectory(prefix=".copy-", dir=target.parent) as staging:
            copied = Path(staging) / "espeak-ng-data"
            shutil.copytree(packaged_data, copied)
            if not _espeak_data_matches(copied, manifest):
                raise OSError("Copied speech data does not match the package")
            if _espeak_data_matches(target, manifest):
                return
            try:
                target.rename(Path(staging) / "discarded")
            except FileNotFoundError:
                pass
            copied.rename(target)


def _windows_espeak_data_path(packaged_data: Path) -> Path:
    # The bundled C library uses narrow Windows filesystem calls. UTF-8 paths
    # can terminate the process inside espeak_Initialize, before Python can
    # catch an error. Phonemizer resolves junctions/8.3 names back to the unsafe
    # path, so keep a small, versioned data copy when a bridge cannot work.
    if _is_native_safe_espeak_path(packaged_data, platform="win32"):
        return packaged_data
    files = sorted(path for path in packaged_data.rglob("*") if path.is_file())
    fingerprint = hashlib.sha256(str(packaged_data).encode("utf-8"))
    for path in files:
        stat = path.stat()
        fingerprint.update(
            f"{path.relative_to(packaged_data)}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
        )
    version = fingerprint.hexdigest()[:16]
    # A single key preserves user isolation and versioning without spending the
    # native path budget on two separate hash directories.
    cache_key = hashlib.sha256(f"{Path.home()}:{version}".encode("utf-8")).hexdigest()[:16]
    program_data = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))
    packaged_manifest = _espeak_data_manifest(packaged_data)
    for base in (
        _espeak_runtime_base() / version,
        Path(tempfile.gettempdir()) / "orpheus-espeak" / version,
        program_data / f"orpheus-espeak-{cache_key}",
    ):
        target = base / "espeak-ng-data"
        if not _is_native_safe_espeak_path(target, platform="win32"):
            continue
        try:
            shared_cache = base.is_relative_to(program_data) or base.resolve().is_relative_to(program_data.resolve())
            if shared_cache and (base.is_symlink() or base.is_junction()):
                continue
            # Windows Python 3.12.4+ restricts new 0700 directories to the user
            # and administrators. Existing directories (or older Python builds)
            # still require content verification; their inherited ACL is unchanged.
            base.mkdir(mode=0o700 if shared_cache else 0o777, parents=True, exist_ok=True)
            # Verify write access even when reusing a previously populated cache.
            with tempfile.TemporaryFile(dir=target.parent) as probe:
                probe.write(b"orpheus")
            _publish_espeak_data(packaged_data, target, packaged_manifest)
            if not _espeak_data_matches(target, packaged_manifest):
                continue
            if (target / "phontab").is_file() and all(
                (target / path.relative_to(packaged_data)).is_file()
                and (target / path.relative_to(packaged_data)).stat().st_size == path.stat().st_size
                for path in files
            ):
                resolved = target.resolve()
                if _is_native_safe_espeak_path(resolved, platform="win32"):
                    return resolved
        except OSError:
            continue
    raise RuntimeError(
        "Speech data needs a writable short ASCII cache path in ProgramData or TEMP. "
        "Library features remain available; repair cache access and restart to enable speech."
    )


def _parse_host(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        raise argparse.ArgumentTypeError("host is required")
    if value == "localhost":
        return "127.0.0.1"

    try:
        parsed = ipaddress.ip_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("host must be a valid IP address") from exc

    if not parsed.is_loopback:
        raise argparse.ArgumentTypeError("host must be a loopback address (for example 127.0.0.1)")
    return str(parsed)


def _parse_port(raw_value: str) -> int:
    try:
        port = int(raw_value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc

    if port < 0 or port > 65535:
        raise argparse.ArgumentTypeError("port must be in range 0-65535")
    return port


def _extract_bound_port(servers: Sequence[object] | None) -> int:
    if not servers:
        raise RuntimeError("Desktop backend did not expose any active server sockets.")
    for server in servers:
        sockets = getattr(server, "sockets", None) or []
        for bound_socket in sockets:
            sockname = bound_socket.getsockname()
            if isinstance(sockname, tuple) and len(sockname) >= 2:
                return int(sockname[1])
    raise RuntimeError("Desktop backend failed to resolve the bound socket port.")


def _write_ready_file(payload: dict[str, object]) -> None:
    ready_file_value = os.environ.get("AUDIOBOOK_DESKTOP_READY_FILE", "").strip()
    if not ready_file_value:
        return

    ready_file = Path(ready_file_value).expanduser()
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = ready_file.with_name(f".{ready_file.name}.tmp")
    temp_file.unlink(missing_ok=True)
    encoded_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    fd = os.open(temp_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded_payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temp_file.unlink(missing_ok=True)
        raise
    os.replace(temp_file, ready_file)


def _runtime_profile_identity() -> str:
    """Resolve the runtime identity embedded by the sidecar build."""
    if getattr(sys, "frozen", False):
        marker_path = Path(sys.executable).resolve().parent / _RUNTIME_PROFILE_MARKER
        try:
            payload = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Packaged runtime profile marker is missing or invalid: {marker_path}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Packaged runtime profile marker is missing or invalid: {marker_path}"
            )
        profile = str(payload.get("profile", "")).strip().lower()
        if profile not in _SUPPORTED_RUNTIME_PROFILES:
            raise RuntimeError(
                f"Packaged runtime profile marker has an unsupported profile: {profile!r}"
            )
        return profile

    configured = os.environ.get("ORPHEUS_RUNTIME_PROFILE", "").strip().lower()
    if not configured:
        return "development"
    if configured not in _SUPPORTED_RUNTIME_PROFILES:
        raise RuntimeError(f"Unsupported ORPHEUS_RUNTIME_PROFILE: {configured!r}")
    return configured


class _DesktopReadyServer(uvicorn.Server):
    def __init__(
        self,
        config: uvicorn.Config,
        *,
        ready_host: str,
        token: str,
        runtime_profile: str,
    ) -> None:
        super().__init__(config)
        self._ready_host = ready_host
        self._token = token
        self._runtime_profile = runtime_profile
        self.ready_emitted = False
        self._broker_shutdown_done = False
        self.config.app.state.desktop_shutdown = self.request_shutdown

    def request_shutdown(self) -> None:
        self._shutdown_event_broker()
        self.should_exit = True

    def handle_exit(self, sig: int, frame: object) -> None:
        # Close the event broker the instant Uvicorn handles its exit signal,
        # before Uvicorn begins draining active connections. The persistent SSE
        # response would otherwise keep a connection alive until FastAPI lifespan
        # teardown, so closing here breaks that shutdown cycle and lets desktop
        # close feel immediate. Runs in the signal handler, so it must stay cheap
        # and non-blocking (broker shutdown is just bounded queue puts).
        self._shutdown_event_broker()
        super().handle_exit(sig, frame)

    def _shutdown_event_broker(self) -> None:
        # Idempotent: only fan out the close sentinel once even if the signal is
        # delivered repeatedly. `EventBroker.shutdown` is itself idempotent, but
        # this also guards apps that expose a non-idempotent broker.
        if self._broker_shutdown_done:
            return
        self._broker_shutdown_done = True
        app_state = getattr(self.config.app, "state", None)
        broker = getattr(app_state, "event_broker", None)
        shutdown = getattr(broker, "shutdown", None)
        if callable(shutdown):
            shutdown()

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.should_exit or self.ready_emitted:
            return

        bound_port = _extract_bound_port(self.servers)
        payload = {
            "event": "ready",
            "host": self._ready_host,
            "port": bound_port,
            "token": self._token,
            "runtime_profile": self._runtime_profile,
        }
        _write_ready_file(payload)
        print(json.dumps(payload, separators=(",", ":")), flush=True)
        self.ready_emitted = True

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        # Keep uvicorn's graceful signal handling, but do not re-raise SIGTERM after
        # shutdown so the desktop launcher exits with code 0.
        if threading.current_thread() is not threading.main_thread():
            yield
            return

        original_handlers = {
            sig: signal.signal(sig, self.handle_exit) for sig in HANDLED_SIGNALS
        }
        try:
            yield
        finally:
            for sig, handler in original_handlers.items():
                signal.signal(sig, handler)
            self._captured_signals.clear()


def _build_desktop_server(
    app: object,
    *,
    host: str,
    port: int,
    token: str,
    runtime_profile: str,
) -> _DesktopReadyServer:
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        access_log=False,
        log_level="warning",
        timeout_graceful_shutdown=DESKTOP_GRACEFUL_SHUTDOWN_TIMEOUT,
    )
    return _DesktopReadyServer(
        config,
        ready_host=host,
        token=token,
        runtime_profile=runtime_profile,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="desktop-backend",
        description="Orpheus desktop backend launcher",
    )
    parser.add_argument(
        "--host",
        type=_parse_host,
        default="127.0.0.1",
        help="Loopback host to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=_parse_port,
        default=0,
        help="Port to bind (default: 0 for ephemeral port)",
    )
    return parser


def configure_espeak_runtime() -> None:
    """Point phonemizer/espeak at safe data paths before TTS imports.

    The bundled libespeak-ng defaults to its compile-time CI data path. Misaki
    configures phonemizer from espeakng_loader getters at import time, so the
    launcher must retarget those getters and EspeakWrapper before any
    misaki/phonemizer/TTS import can lock in the missing path.

    Nested .app and virtualenv paths can exceed libespeak POSIX N_PATH_HOME (160
    filesystem bytes). When the data path is too long, expose it through a short
    verified data copy in a per-user cache location and set
    ESPEAK_DATA_PATH / PHONEMIZER_* consistently for parent and spawn-worker
    re-entry. Windows uses a versioned ASCII data copy for unsafe paths.
    """
    from modules.tts.mlx_compat import patch_espeakng_loader_paths

    patch_espeakng_loader_paths()

    try:
        import espeakng_loader
    except ImportError:
        return

    library_path = Path(espeakng_loader.get_library_path())
    packaged_data = Path(espeakng_loader.get_data_path())
    if not library_path.is_file() or not packaged_data.is_dir():
        return
    if (
        not getattr(sys, "frozen", False)
        and _is_native_safe_espeak_path(packaged_data.resolve())
    ):
        return

    try:
        data_path = _ensure_short_espeak_data_path(packaged_data)
    except (OSError, RuntimeError) as exc:
        message = f"Speech initialization is unavailable: {exc}"
        print(message, file=sys.stderr)

        def unavailable(*_args: object, **_kwargs: object) -> str:
            raise RuntimeError(message)

        # Fail in Python before any native initialization can use the unsafe path.
        espeakng_loader.get_library_path = unavailable
        espeakng_loader.get_data_path = unavailable
        try:
            from phonemizer.backend.espeak.wrapper import EspeakWrapper
        except ImportError:
            return
        EspeakWrapper.library = classmethod(unavailable)
        return
    library = str(library_path)
    data = str(data_path)

    espeakng_loader.get_library_path = lambda library=library: library
    espeakng_loader.get_data_path = lambda data=data: data
    os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = library
    os.environ["PHONEMIZER_ESPEAK_DATA_PATH"] = data
    os.environ["ESPEAK_DATA_PATH"] = data
    try:
        from phonemizer.backend.espeak.wrapper import EspeakWrapper
    except ImportError:
        return
    EspeakWrapper.set_library(library)
    EspeakWrapper.set_data_path(data)


def main(argv: list[str] | None = None) -> int:
    global _library_lock
    # App-owned databases, sources, temporary audio, and generated artifacts
    # are private by default. Explicit chmod calls also repair existing roots;
    # the process umask covers every newly created nested path.
    if os.name != "nt":
        os.umask(0o077)
    # Configure espeak paths before freeze_support: frozen multiprocessing
    # children re-enter main and never return from freeze_support.
    configure_espeak_runtime()
    # Required for PyInstaller-frozen multiprocessing helper subprocesses
    # (resource tracker / forkserver / worker bootstrap) so they don't fall
    # through to CLI arg parsing.
    multiprocessing.freeze_support()
    args = build_parser().parse_args(argv)
    token = secrets.token_urlsafe(32)
    try:
        runtime_profile = _runtime_profile_identity()
    except RuntimeError as exc:
        print(f"failed to start desktop backend: {exc}", file=sys.stderr)
        return 1
    config = AppConfig()
    try:
        # Retain ownership through interpreter teardown and worker joins.
        _library_lock = acquire_database(config.db_path)
        config.db_path = _library_lock.path
    except BlockingIOError:
        print("Orpheus is already running with this library", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"failed to lock desktop library: {exc}", file=sys.stderr)
        return 1
    app = create_app(
        controller_factory=lambda: AppController(config=config),
        desktop_mode=True, desktop_api_token=token, enforce_loopback_host=True,
    )
    server = _build_desktop_server(
        app,
        host=args.host,
        port=args.port,
        token=token,
        runtime_profile=runtime_profile,
    )

    parent_watchdog_stop = _start_desktop_parent_watchdog(server.request_shutdown)
    try:
        server.run()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"failed to start desktop backend: {exc}", file=sys.stderr)
        return 1
    finally:
        if parent_watchdog_stop is not None:
            parent_watchdog_stop.set()

    if not server.ready_emitted:
        # A library that cannot be opened (newer schema, read-only file) names
        # its cause and remedy; keep it last so the shell's error tail ends with it.
        cause = getattr(getattr(app, "state", None), "startup_error", None)
        if isinstance(cause, Exception) and str(cause):
            print(f"failed to start desktop backend: {cause}", file=sys.stderr)
        else:
            # No named cause: leave the prefix off so the shell keeps the
            # stderr tail above, which holds the actual error.
            print("desktop backend startup did not complete", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
