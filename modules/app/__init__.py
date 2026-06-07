"""
Application Module
==================
Core application controller and business logic.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "AppConfig": ("modules.app.config", "AppConfig"),
    "AppController": ("modules.app.controller", "AppController"),
    "ConversionJob": ("modules.app.controller", "ConversionJob"),
    "ConversionCallbacks": ("modules.app.controller", "ConversionCallbacks"),
    "ConversionRuntimeOptions": (
        "modules.app.services.conversion_service",
        "ConversionRuntimeOptions",
    ),
    "AppEvent": ("modules.app.events", "AppEvent"),
    "EventType": ("modules.app.events", "EventType"),
    "JobState": ("modules.app.events", "JobState"),
    "LogEvent": ("modules.app.events", "LogEvent"),
    "ProgressEvent": ("modules.app.events", "ProgressEvent"),
    "StateEvent": ("modules.app.events", "StateEvent"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = _EXPORTS[name]
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
