"""FastAPI bootstrap for the desktop backend API."""

from __future__ import annotations

import ipaddress
import logging
import importlib
import os
import threading
from contextlib import asynccontextmanager
from typing import Callable, Optional
from urllib.parse import urlsplit
import uuid

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from modules.app import AppController
from modules.contracts.public_errors import PublicError, public_error_detail
from modules.web.desktop_auth import add_desktop_auth_middleware, desktop_query_token_router
from modules.web.events import EventBroker
from modules.web.routes import (
    conversions,
    events,
    health,
    library,
    maintenance,
    options,
    setup,
    sources,
    uploads,
    voices,
)
from modules.web.deps import get_controller_from_app
from modules.runtime_setup.state import acknowledge_backend_startup, persist_backend_instance
from modules.runtime_setup import state as setup_runtime

DESKTOP_CORS_ORIGIN_RE = (
    r"^(http://tauri\.localhost|"
    r"https://tauri\.localhost|"
    r"tauri://localhost)$"
)
DESKTOP_DEV_ORIGIN = "http://localhost:5173"

# The Vite dev server origin is only reachable while running `tauri dev` from a
# source checkout. The desktop launcher sets this env var exclusively in debug
# builds (Rust `#[cfg(debug_assertions)]`), so packaged/release builds never add
# the dev origin to their CORS allowlist — only the tauri.localhost regex applies.
DESKTOP_DEV_MODE_ENV = "ORPHEUS_DESKTOP_DEV"

logger = logging.getLogger(__name__)


def _desktop_dev_cors_origins() -> list[str]:
    enabled = os.environ.get(DESKTOP_DEV_MODE_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return [DESKTOP_DEV_ORIGIN] if enabled else []


def _sweep_stale_uploads_on_startup(controller: object) -> None:
    sweep = getattr(controller, "sweep_stale_uploads", None)
    if not callable(sweep):
        return
    try:
        sweep()
    except Exception:
        logger.warning("Startup stale-upload sweep failed", exc_info=True)


def _acknowledge_runtime_setup_on_startup(backend_instance_id: str) -> None:
    try:
        persist_backend_instance(backend_instance_id)
        acknowledge_backend_startup(backend_instance_id)
    except Exception:
        logger.warning("Runtime setup restart acknowledgement failed", exc_info=True)


def _shutdown_event_broker(app: FastAPI) -> None:
    broker = getattr(app.state, "event_broker", None)
    shutdown = getattr(broker, "shutdown", None)
    if callable(shutdown):
        shutdown()


def _warm_configured_runtime_imports(app: FastAPI, cancelled: threading.Event) -> None:
    """Warm package imports only after this launch has verified its selection."""
    try:
        state, error = setup_runtime._read_setup_state(setup_runtime.default_state_path())
        if error or state is None or state["profile"] != "cuda":
            return
        request = Request({"type": "http", "app": app})
        while not cancelled.is_set():
            status = setup.build_setup_status_response(request)
            if cancelled.is_set():
                return
            checks = status.get("probe", {}).get("checks", [])
            # Wait out the frozen build's placeholder, but a probe that timed
            # out is never cached: asking again would spawn another probe.
            if status.get("state") == "checking" and not any(
                check.get("state") == "timeout" for check in checks
            ):
                cancelled.wait(0.25)
                continue
            if (
                not status.get("configured") or not status.get("ready")
                or status.get("state") != "ready" or status.get("selected_profile") != "cuda"
                or status.get("restart_required")
            ):
                return
            # Imports only: do not resolve a device, construct a strategy, load
            # weights, or run the capability script (which launches a CUDA kernel).
            for module in ("torch", "kokoro"):
                if cancelled.is_set():
                    return
                importlib.import_module(module)
            return
    except Exception:
        logger.debug("Background TTS import warmup failed", exc_info=True)


def _shutdown_model_download_service(app: FastAPI) -> None:
    service = getattr(app.state, "model_download_service", None)
    shutdown = getattr(service, "shutdown", None)
    if callable(shutdown):
        try:
            shutdown(timeout_seconds=2.0)
        except Exception:
            logger.warning("Model download service shutdown failed", exc_info=True)


def _cleanup_owned_controller(app: FastAPI) -> None:
    if not getattr(app.state, "owns_controller", False):
        return

    service = getattr(app.state, "controller", None)
    cleanup = getattr(service, "cleanup", None)
    if callable(cleanup):
        cleanup()
    app.state.controller = None


async def _validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    # ``input`` echoes request values, including API keys sent alongside the
    # invalid field; clients need only ``loc`` and ``msg``.
    errors = [{key: value for key, value in error.items() if key != "input"} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


_INTERNAL_ERROR = PublicError(
    "internal_error",
    "The backend could not complete the request. Details are in the backend log.",
    500,
)
_INVALID_HOST_ERROR = PublicError("invalid_host", "Requests must address the local backend.", 421)


class _InternalErrorMiddleware:
    """Answer unexpected route failures with the typed public error envelope.

    Starlette renders an unhandled exception above every user middleware, so
    the default plain-text 500 carries no CORS headers and the cross-origin
    desktop WebView reports an opaque network failure instead. Running inside
    CORS keeps the response readable; the exception text, which can carry
    local paths, stays in the backend log.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except ClientDisconnect:
            raise
        except Exception:
            if response_started:
                raise
            # The path only: query strings can carry short-lived credentials.
            logger.exception("Unhandled error in %s %s", scope.get("method"), scope.get("path"))
            response = JSONResponse(status_code=500, content={"detail": public_error_detail(_INTERNAL_ERROR)})
            await response(scope, receive, send)


def _is_loopback_host_header(value: str) -> bool:
    try:
        parsed = urlsplit(f"//{value}")
        parsed.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        return False
    hostname = parsed.hostname
    if not hostname or parsed.username is not None or parsed.path or parsed.query or parsed.fragment:
        return False
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class _LoopbackHostMiddleware:
    """Refuse requests whose Host is not a loopback name (DNS rebinding)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            host = Headers(scope=scope).get("host", "")
            if not _is_loopback_host_header(host):
                response = JSONResponse(
                    status_code=_INVALID_HOST_ERROR.status,
                    content={"detail": public_error_detail(_INVALID_HOST_ERROR)},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _register_routes(app: FastAPI) -> None:
    app.include_router(health.router)
    app.include_router(library.router)
    app.include_router(maintenance.router)
    app.include_router(options.router)
    app.include_router(setup.router)
    app.include_router(sources.router)
    app.include_router(voices.router)
    app.include_router(uploads.router)
    app.include_router(events.router)
    app.include_router(conversions.router)
    app.include_router(desktop_query_token_router)


def _configure_desktop_runtime(
    app: FastAPI,
    *,
    desktop_mode: bool,
    desktop_api_token: str | None,
    enforce_loopback_host: bool,
) -> None:
    app.state.desktop_mode = desktop_mode
    app.state.desktop_api_token = desktop_api_token

    if not desktop_mode:
        return

    add_desktop_auth_middleware(app)

    @app.post("/api/desktop/shutdown", status_code=202)
    async def shutdown_desktop_backend() -> JSONResponse:
        shutdown = getattr(app.state, "desktop_shutdown", None)
        if not callable(shutdown):
            return JSONResponse(status_code=503, content={"detail": "Shutdown is unavailable"})
        shutdown()
        return JSONResponse(status_code=202, content={"status": "shutting_down"})

    # Vite runs on a separate localhost port during desktop development; that
    # origin is allowed only in debug builds (see _desktop_dev_cors_origins).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_desktop_dev_cors_origins(),
        allow_origin_regex=DESKTOP_CORS_ORIGIN_RE,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Outermost, so a page rebound to the loopback address gets no answer,
    # not even a CORS preflight or the unauthenticated health check.
    if enforce_loopback_host:
        app.add_middleware(_LoopbackHostMiddleware)


def create_app(
    controller: Optional[AppController] = None,
    broker: Optional[EventBroker] = None,
    controller_factory: Optional[Callable[[], AppController]] = None,
    *,
    desktop_mode: bool = False,
    desktop_api_token: str | None = None,
    enforce_loopback_host: bool = False,
) -> FastAPI:
    """Create FastAPI app instance for the desktop backend API.

    ``enforce_loopback_host`` rejects requests whose Host header
    is not a loopback name, so a DNS-rebound web page cannot reach the API.
    The launcher enables it; in-process test clients keep their own hosts.
    """
    event_broker = broker or EventBroker()
    owns_controller = controller is None
    build_controller = controller_factory or AppController
    backend_instance_id = uuid.uuid4().hex

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if desktop_mode:
            _acknowledge_runtime_setup_on_startup(backend_instance_id)
        try:
            controller = get_controller_from_app(app)
        except Exception as error:
            # The launcher reports this cause as its final line; the desktop
            # shell quotes the stderr tail when the backend exits before ready.
            app.state.startup_error = error
            raise
        _sweep_stale_uploads_on_startup(controller)
        import_warmup_cancelled = threading.Event()
        if desktop_mode:
            try:
                # Bind the cache while the controller is still owned by startup;
                # the daemon must never recreate a controller after shutdown.
                setup._get_status_cache(Request({"type": "http", "app": app}))
                threading.Thread(
                    target=_warm_configured_runtime_imports,
                    args=(app, import_warmup_cancelled),
                    name="tts-import-warmup",
                    daemon=True,
                ).start()
            except Exception:
                logger.debug("Could not start TTS import warmup", exc_info=True)
        try:
            yield
        finally:
            import_warmup_cancelled.set()
            _shutdown_model_download_service(app)
            _shutdown_event_broker(app)
            _cleanup_owned_controller(app)

    # Desktop mode exposes only `/health` without the session token; the
    # interactive docs and schema routes would otherwise be served openly.
    docs_routes = {"docs_url": None, "redoc_url": None, "openapi_url": None} if desktop_mode else {}
    app = FastAPI(title="Orpheus Desktop API", version="0.1.0", lifespan=lifespan, **docs_routes)
    app.add_exception_handler(
        RequestValidationError, _validation_exception_handler
    )
    # Innermost: inside CORS and the upload size limit, so it never masks a 413.
    app.add_middleware(_InternalErrorMiddleware)
    uploads.add_upload_size_limit_middleware(app)
    app.state.event_broker = event_broker
    app.state.controller = controller
    app.state.controller_factory = build_controller
    app.state.controller_init_lock = threading.Lock()
    # Setup status and model-download services are both lazy and share their
    # cache initialization path, so this must be re-entrant.
    app.state.runtime_setup_state_lock = threading.RLock()
    app.state.owns_controller = owns_controller
    app.state.backend_instance_id = backend_instance_id
    # Probe cache is wired lazily in setup._get_status_cache so it shares the
    # controller RuntimeCapabilityService (setup vs capabilities agreement).
    app.state.runtime_setup_status_cache = None
    _configure_desktop_runtime(
        app,
        desktop_mode=desktop_mode,
        desktop_api_token=desktop_api_token,
        enforce_loopback_host=enforce_loopback_host,
    )
    _register_routes(app)
    return app
