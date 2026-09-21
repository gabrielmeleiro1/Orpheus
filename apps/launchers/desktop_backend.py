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
from collections.abc import Generator
from pathlib import Path
from typing import Sequence

import uvicorn
from uvicorn.server import HANDLED_SIGNALS

from modules.web.server import create_app

# libespeak-ng POSIX path_home buffer (speech.h N_PATH_HOME).
# Limit is filesystem bytes (C char buffer), not Python string code points.
_LIBESPEAK_PATH_HOME_LIMIT = 160
_SUPPORTED_RUNTIME_PROFILES = frozenset({"base", "mlx", "cuda"})
_RUNTIME_PROFILE_MARKER = "runtime-profile.json"

# Upper bound on how long Uvicorn will wait for active connections to drain on
# shutdown. The persistent SSE stream is closed proactively on signal (see
# `_DesktopReadyServer.handle_exit`), so this is only a backstop that keeps
# desktop close bounded even if a client connection lingers.
DESKTOP_GRACEFUL_SHUTDOWN_TIMEOUT = 3.0


def _path_filesystem_byte_length(path: Path | str) -> int:
    """Return path length in filesystem-encoded bytes (libespeak buffer units)."""
    return len(os.fsencode(os.fspath(path)))


def _frozen_espeak_runtime_base() -> Path:
    """Short per-user cache dir for the espeak-ng-data bridge or copy."""
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
    if _path_filesystem_byte_length(packaged_data) < _LIBESPEAK_PATH_HOME_LIMIT:
        return packaged_data

    candidates = (
        _frozen_espeak_runtime_base() / "espeak-ng-data",
        Path(tempfile.gettempdir()) / "orpheus-espeak" / "espeak-ng-data",
    )
    for link_path in candidates:
        if _path_filesystem_byte_length(link_path) >= _LIBESPEAK_PATH_HOME_LIMIT:
            continue
        try:
            link_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        try:
            if link_path.is_symlink() and link_path.resolve() == packaged_data:
                return link_path
        except OSError:
            pass

        # Replace only symlinks/files. Never delete a real directory tree.
        try:
            if link_path.is_symlink() or link_path.is_file():
                link_path.unlink()
            elif link_path.exists():
                continue
            link_path.symlink_to(packaged_data, target_is_directory=True)
        except FileExistsError:
            try:
                if link_path.is_symlink() and link_path.resolve() == packaged_data:
                    return link_path
            except OSError:
                continue
            continue
        except OSError:
            continue

        try:
            if link_path.resolve() == packaged_data:
                return link_path
        except OSError:
            continue

    # Last resort: return packaged path (may still truncate in libespeak).
    return packaged_data


def _windows_espeak_data_path(packaged_data: Path) -> Path:
    # The bundled C library uses narrow Windows filesystem calls. UTF-8 paths
    # can terminate the process inside espeak_Initialize, before Python can
    # catch an error. Phonemizer resolves junctions/8.3 names back to the unsafe
    # path, so keep a small, versioned data copy when a bridge cannot work.
    if (
        str(packaged_data).isascii()
        and _path_filesystem_byte_length(packaged_data) < _LIBESPEAK_PATH_HOME_LIMIT
    ):
        return packaged_data
    files = sorted(path for path in packaged_data.rglob("*") if path.is_file())
    fingerprint = hashlib.sha256(str(packaged_data).encode("utf-8"))
    for path in files:
        stat = path.stat()
        fingerprint.update(
            f"{path.relative_to(packaged_data)}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
        )
    version = fingerprint.hexdigest()[:16]
    for base in (_frozen_espeak_runtime_base(), Path(tempfile.gettempdir()) / "orpheus-espeak"):
        target = base / version / "espeak-ng-data"
        if not str(target).isascii() or _path_filesystem_byte_length(target) >= _LIBESPEAK_PATH_HOME_LIMIT:
            continue
        try:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=".copy-", dir=target.parent) as staging:
                    copied = Path(staging) / "espeak-ng-data"
                    shutil.copytree(packaged_data, copied)
                    try:
                        copied.rename(target)
                    except FileExistsError:
                        # Another backend may have completed the same cache.
                        pass
            if (target / "phontab").is_file() and all(
                (target / path.relative_to(packaged_data)).is_file()
                and (target / path.relative_to(packaged_data)).stat().st_size == path.stat().st_size
                for path in files
            ):
                resolved = target.resolve()
                if (
                    str(resolved).isascii()
                    and _path_filesystem_byte_length(resolved) < _LIBESPEAK_PATH_HOME_LIMIT
                ):
                    return resolved
        except OSError:
            continue
    raise RuntimeError(
        "Speech data needs a short ASCII cache path. "
        "Choose an ASCII Windows TEMP directory and restart Orpheus."
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


def configure_frozen_espeak_runtime() -> None:
    """Point phonemizer/espeak at packaged data before TTS imports (PyInstaller only).

    The bundled libespeak-ng defaults to its compile-time CI data path. Misaki
    configures phonemizer from espeakng_loader getters at import time, so the
    frozen sidecar must retarget those getters and EspeakWrapper before any
    misaki/phonemizer/TTS import can lock in the missing path.

    Nested .app paths often exceed libespeak POSIX N_PATH_HOME (160 filesystem
    bytes). When the packaged data path is too long, expose it through a short
    symlink bridge in a per-user cache location and set
    ESPEAK_DATA_PATH / PHONEMIZER_* consistently for parent and spawn-worker
    re-entry. Windows uses a versioned ASCII data copy for unsafe paths.
    """
    if not getattr(sys, "frozen", False):
        return

    from modules.tts.mlx_compat import patch_frozen_espeakng_loader_paths

    patch_frozen_espeakng_loader_paths()

    try:
        import espeakng_loader
        from phonemizer.backend.espeak.wrapper import EspeakWrapper
    except ImportError:
        return

    library_path = Path(espeakng_loader.get_library_path())
    packaged_data = Path(espeakng_loader.get_data_path())
    if not library_path.is_file() or not packaged_data.is_dir():
        return

    data_path = _ensure_short_espeak_data_path(packaged_data)
    library = str(library_path)
    data = str(data_path)

    espeakng_loader.get_library_path = lambda library=library: library
    espeakng_loader.get_data_path = lambda data=data: data
    EspeakWrapper.set_library(library)
    EspeakWrapper.set_data_path(data)
    os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = library
    os.environ["PHONEMIZER_ESPEAK_DATA_PATH"] = data
    os.environ["ESPEAK_DATA_PATH"] = data


def main(argv: list[str] | None = None) -> int:
    # App-owned databases, sources, temporary audio, and generated artifacts
    # are private by default. Explicit chmod calls also repair existing roots;
    # the process umask covers every newly created nested path.
    if os.name != "nt":
        os.umask(0o077)
    # Configure packaged espeak paths before freeze_support: multiprocessing
    # children re-enter main and never return from freeze_support.
    configure_frozen_espeak_runtime()
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
    app = create_app(desktop_mode=True, desktop_api_token=token)
    server = _build_desktop_server(
        app,
        host=args.host,
        port=args.port,
        token=token,
        runtime_profile=runtime_profile,
    )

    try:
        server.run()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"failed to start desktop backend: {exc}", file=sys.stderr)
        return 1

    if not server.ready_emitted:
        print("failed to start desktop backend: startup did not complete", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
