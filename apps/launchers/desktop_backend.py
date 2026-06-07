#!/usr/bin/env python3
"""Desktop backend launcher implementation."""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import multiprocessing
import os
import secrets
import signal
import socket
import sys
import threading
from collections.abc import Generator
from pathlib import Path
from typing import Sequence

import uvicorn
from uvicorn.server import HANDLED_SIGNALS

from modules.web.server import create_app


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


class _DesktopReadyServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, *, ready_host: str, token: str) -> None:
        super().__init__(config)
        self._ready_host = ready_host
        self._token = token
        self.ready_emitted = False

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


def main(argv: list[str] | None = None) -> int:
    # Required for PyInstaller-frozen multiprocessing helper subprocesses
    # (resource tracker / forkserver / worker bootstrap) so they don't fall
    # through to CLI arg parsing.
    multiprocessing.freeze_support()
    args = build_parser().parse_args(argv)
    token = secrets.token_urlsafe(32)
    app = create_app(desktop_mode=True, desktop_api_token=token)
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        access_log=False,
        log_level="warning",
    )
    server = _DesktopReadyServer(config, ready_host=args.host, token=token)

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
