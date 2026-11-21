"""Shared FastAPI dependencies for the desktop API surface."""

from __future__ import annotations

from pathlib import Path
import threading
from typing import Annotated, Callable, TypeVar

from fastapi import Depends, HTTPException, Request
from fastapi import FastAPI
from modules.app import AppController
from modules.contracts.public_errors import PublicError, public_error_detail
from modules.errors import (
    ConversionCleanupPendingError,
    SourceInputError,
    TTSInstallationError,
)
from modules.web.events import EventBroker

T = TypeVar("T")

# ``create_app`` installs a per-app lock. Keep a fallback for callers that
# construct a FastAPI app by hand and use these dependencies directly.
_CONTROLLER_INIT_FALLBACK_LOCK = threading.Lock()


def get_controller_from_app(app: FastAPI) -> AppController:
    """Return the app controller, creating it lazily when needed."""
    service = getattr(app.state, "controller", None)
    if service is not None:
        return service

    factory = getattr(app.state, "controller_factory", None)
    if factory is None:
        raise RuntimeError("Controller factory is not configured")

    # Synchronous FastAPI dependencies run in a thread pool. Without a
    # single-flight guard, concurrent first requests can construct multiple
    # controllers (and therefore multiple SQLite connections/event services)
    # before any of them stores its instance in app.state.
    lock = getattr(app.state, "controller_init_lock", None)
    if lock is None:
        lock = _CONTROLLER_INIT_FALLBACK_LOCK
    with lock:
        service = getattr(app.state, "controller", None)
        if service is None:
            service = factory()
            apply_preferences = getattr(getattr(service, "config", None), "apply_setup_preferences", None)
            if callable(apply_preferences):
                apply_preferences()
            app.state.controller = service
    return service


def get_controller(request: Request) -> AppController:
    return get_controller_from_app(request.app)


def get_event_broker(request: Request) -> EventBroker:
    broker = getattr(request.app.state, "event_broker", None)
    if broker is None:
        raise RuntimeError("Event broker is not configured")
    return broker


def get_temp_dir(request: Request) -> Path:
    service_config = getattr(get_controller(request), "config", None)
    return getattr(service_config, "temp_dir", Path("temp"))


def public_http_error(error: PublicError, *, legacy_error: bool = False) -> HTTPException:
    """Translate an explicitly classified public error to the HTTP envelope."""
    return HTTPException(status_code=error.status, detail=public_error_detail(error, legacy_error=legacy_error))


def call_with_http_errors(action: Callable[[], T], *, runtime_status: int = 500) -> T:
    """Translate controller/service errors into consistent HTTP responses."""
    try:
        return action()
    except (ConversionCleanupPendingError, SourceInputError) as exc:
        raise public_http_error(exc.public_error) from exc
    except TTSInstallationError as exc:
        error = exc.public_error
        raise HTTPException(status_code=error.status, detail=error.message) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=runtime_status, detail=str(exc)) from exc


ControllerDep = Annotated[AppController, Depends(get_controller)]
EventBrokerDep = Annotated[EventBroker, Depends(get_event_broker)]
TempDirDep = Annotated[Path, Depends(get_temp_dir)]
