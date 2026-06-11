"""Cleaner backend option metadata built from the registry."""

from __future__ import annotations

from typing import Any

from modules.tts.cleaner_backends.registry import (
    available_cleaner_backends,
    get_cleaner_backend,
)
def build_cleaner_backend_option(backend_name: str) -> dict[str, Any]:
    """Build a UI/API option descriptor for one registered cleaner backend."""
    backend = get_cleaner_backend(backend_name)
    option: dict[str, Any] = {
        "name": backend.name,
        "display_name": backend.display_name,
        "is_local": backend.is_local,
        "requires_model": backend.requires_model,
        "requires_api_key": backend.requires_api_key,
        "models": backend.list_models(),
    }
    if backend.supported_quantizations is not None:
        option["supported_quantizations"] = list(backend.supported_quantizations)
    if backend.default_device is not None:
        option["default_device"] = backend.default_device
    return option


def list_cleaner_backend_options() -> list[dict[str, Any]]:
    """Return option descriptors for every registered cleaner backend."""
    return [build_cleaner_backend_option(name) for name in available_cleaner_backends()]
