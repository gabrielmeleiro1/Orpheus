"""App service layer modules."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "LibraryService": ("modules.app.services.library_service", "LibraryService"),
    "RuntimeCapabilityService": (
        "modules.app.services.runtime_capability_service",
        "RuntimeCapabilityService",
    ),
    "VoiceService": ("modules.app.services.voice_service", "VoiceService"),
    "ConversionRuntimeOptions": (
        "modules.app.services.conversion_service",
        "ConversionRuntimeOptions",
    ),
    "ConversionService": ("modules.app.services.conversion_service", "ConversionService"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = _EXPORTS[name]
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
