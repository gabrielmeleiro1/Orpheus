"""FastAPI bootstrap for the desktop backend API."""

from __future__ import annotations

import logging
import math
import os
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional
import uuid

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from modules.app import AppController
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
from scripts.setup_runtime import acknowledge_backend_startup, persist_backend_instance

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


def _scrub_non_finite(value: Any) -> Any:
    """Replace NaN/±Infinity floats with their string form.

    Starlette renders response bodies with ``allow_nan=False``. A validation
    error whose echoed ``input`` is non-finite (a NaN/Infinity JSON literal,
    which ``json.loads`` accepts) therefore crashes the 422 renderer into a
    500. Stringifying non-finite floats keeps the error body JSON-compliant.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _scrub_non_finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_non_finite(item) for item in value]
    return value


async def _validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    detail = _scrub_non_finite(jsonable_encoder(exc.errors()))
    return JSONResponse(status_code=422, content={"detail": detail})


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
) -> None:
    app.state.desktop_mode = desktop_mode
    app.state.desktop_api_token = desktop_api_token

    if not desktop_mode:
        return

    add_desktop_auth_middleware(app)

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


def create_app(
    controller: Optional[AppController] = None,
    broker: Optional[EventBroker] = None,
    controller_factory: Optional[Callable[[], AppController]] = None,
    *,
    desktop_mode: bool = False,
    desktop_api_token: str | None = None,
) -> FastAPI:
    """Create FastAPI app instance for the desktop backend API."""
    event_broker = broker or EventBroker()
    owns_controller = controller is None
    build_controller = controller_factory or AppController
    backend_instance_id = uuid.uuid4().hex

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if desktop_mode:
            _acknowledge_runtime_setup_on_startup(backend_instance_id)
        controller = get_controller_from_app(app)
        _sweep_stale_uploads_on_startup(controller)
        try:
            yield
        finally:
            _shutdown_model_download_service(app)
            _shutdown_event_broker(app)
            _cleanup_owned_controller(app)

    app = FastAPI(title="Orpheus Desktop API", version="0.1.0", lifespan=lifespan)
    app.add_exception_handler(
        RequestValidationError, _validation_exception_handler
    )
    uploads.add_upload_size_limit_middleware(app)
    app.state.event_broker = event_broker
    app.state.controller = controller
    app.state.controller_factory = build_controller
    app.state.owns_controller = owns_controller
    app.state.backend_instance_id = backend_instance_id
    app.state.runtime_setup_status_cache = setup.RuntimeSetupStatusCache()
    _configure_desktop_runtime(
        app,
        desktop_mode=desktop_mode,
        desktop_api_token=desktop_api_token,
    )
    _register_routes(app)
    return app
