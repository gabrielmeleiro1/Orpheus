"""FastAPI bootstrap for the desktop backend API."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Callable, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from modules.app import AppController
from modules.web.desktop_auth import add_desktop_auth_middleware, desktop_query_token_router
from modules.web.events import EventBroker
from modules.web.routes import conversions, events, health, library, maintenance, options, sources, uploads, voices
from modules.web.deps import get_controller_from_app

DESKTOP_CORS_ORIGIN_RE = (
    r"^(http://tauri\.localhost|"
    r"https://tauri\.localhost|"
    r"tauri://localhost)$"
)
DESKTOP_DEV_ORIGIN = "http://localhost:5173"

logger = logging.getLogger(__name__)


def _sweep_stale_uploads_on_startup(controller: object) -> None:
    sweep = getattr(controller, "sweep_stale_uploads", None)
    if not callable(sweep):
        return
    try:
        sweep()
    except Exception:
        logger.warning("Startup stale-upload sweep failed", exc_info=True)


def _shutdown_event_broker(app: FastAPI) -> None:
    broker = getattr(app.state, "event_broker", None)
    shutdown = getattr(broker, "shutdown", None)
    if callable(shutdown):
        shutdown()


def _cleanup_owned_controller(app: FastAPI) -> None:
    if not getattr(app.state, "owns_controller", False):
        return

    service = getattr(app.state, "controller", None)
    cleanup = getattr(service, "cleanup", None)
    if callable(cleanup):
        cleanup()
    app.state.controller = None


def _register_routes(app: FastAPI) -> None:
    app.include_router(health.router)
    app.include_router(library.router)
    app.include_router(maintenance.router)
    app.include_router(options.router)
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

    # Vite runs on a separate localhost port during desktop development.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[DESKTOP_DEV_ORIGIN],
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        controller = get_controller_from_app(app)
        _sweep_stale_uploads_on_startup(controller)
        try:
            yield
        finally:
            _shutdown_event_broker(app)
            _cleanup_owned_controller(app)

    app = FastAPI(title="Orpheus Desktop API", version="0.1.0", lifespan=lifespan)
    uploads.add_upload_size_limit_middleware(app)
    app.state.event_broker = event_broker
    app.state.controller = controller
    app.state.controller_factory = build_controller
    app.state.owns_controller = owns_controller
    _configure_desktop_runtime(
        app,
        desktop_mode=desktop_mode,
        desktop_api_token=desktop_api_token,
    )
    _register_routes(app)
    return app
