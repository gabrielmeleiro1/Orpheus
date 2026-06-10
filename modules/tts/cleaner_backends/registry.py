"""Registry for text cleaner backends (mlx, cuda, api)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modules.tts.cleaner_backends.base import CleanerBackend

_REGISTRY: dict[str, Callable[[], CleanerBackend]] = {}


def register_cleaner_backend(
    name: str,
    backend_factory: Callable[[], CleanerBackend],
) -> None:
    """Register a cleaner backend factory under a normalized name."""
    normalized = str(name or "").strip().lower()
    _REGISTRY[normalized] = backend_factory


def get_cleaner_backend(name: str) -> CleanerBackend:
    """Instantiate a registered cleaner backend."""
    normalized = validate_cleaner_backend(name)
    return _REGISTRY[normalized]()


def available_cleaner_backends() -> tuple[str, ...]:
    """Return registered cleaner backend names in sorted order."""
    return tuple(sorted(_REGISTRY))


def validate_cleaner_backend(name: str) -> str:
    """Normalize and validate a cleaner backend name."""
    normalized = str(name or "").strip().lower()
    if normalized in _REGISTRY:
        return normalized
    available = ", ".join(available_cleaner_backends())
    raise ValueError(
        f"Text cleaner backend '{normalized}' is not supported. "
        f"Available backends: {available}."
    )


def list_cleaner_backend_models(backend: str) -> list[str]:
    """Return model identifiers for the requested cleaner backend."""
    return get_cleaner_backend(backend).list_models()


_BUILTIN_BACKENDS_REGISTERED = False


def _register_builtin_backends() -> None:
    """Register built-in cleaner backend strategies."""
    global _BUILTIN_BACKENDS_REGISTERED
    if _BUILTIN_BACKENDS_REGISTERED:
        return

    from modules.tts.cleaner_api import CleanerAPIStrategy
    from modules.tts.cleaner_cuda import CleanerCUDAStrategy
    from modules.tts.cleaner_mlx import CleanerMLXStrategy

    register_cleaner_backend("mlx", CleanerMLXStrategy)
    register_cleaner_backend("cuda", CleanerCUDAStrategy)
    register_cleaner_backend("api", CleanerAPIStrategy)
    _BUILTIN_BACKENDS_REGISTERED = True


_register_builtin_backends()


def is_local_cleaner_backend(name: str) -> bool:
    """Return whether the cleaner backend runs locally on device."""
    normalized = str(name or "").strip().lower()
    factory = _REGISTRY.get(normalized)
    if factory is None:
        return False
    return factory().is_local


def requires_api_key_cleaner_backend(name: str) -> bool:
    """Return whether the cleaner backend requires an API key."""
    normalized = str(name or "").strip().lower()
    factory = _REGISTRY.get(normalized)
    if factory is None:
        return False
    return factory().requires_api_key
