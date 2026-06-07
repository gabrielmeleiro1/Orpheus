"""Shared FastAPI dependencies for the desktop API surface."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Callable, TypeVar

from fastapi import Depends, HTTPException, Request
from fastapi import FastAPI
from modules.app import AppController
from modules.web.events import EventBroker

T = TypeVar("T")


def get_controller_from_app(app: FastAPI) -> AppController:
    """Return the app controller, creating it lazily when needed."""
    service = getattr(app.state, "controller", None)
    if service is None:
        factory = getattr(app.state, "controller_factory", None)
        if factory is None:
            raise RuntimeError("Controller factory is not configured")
        service = factory()
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


def call_with_http_errors(action: Callable[[], T], *, runtime_status: int = 500) -> T:
    """Translate controller/service errors into consistent HTTP responses."""
    try:
        return action()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=runtime_status, detail=str(exc)) from exc


ControllerDep = Annotated[AppController, Depends(get_controller)]
EventBrokerDep = Annotated[EventBroker, Depends(get_event_broker)]
TempDirDep = Annotated[Path, Depends(get_temp_dir)]
