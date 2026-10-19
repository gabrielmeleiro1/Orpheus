"""
Pipeline Module
================
Conversion pipeline orchestration for audiobook creation.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "ConversionPipeline": ("modules.pipeline.orchestrator", "ConversionPipeline"),
    "PipelineConfig": ("modules.pipeline.models", "PipelineConfig"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = _EXPORTS[name]
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
