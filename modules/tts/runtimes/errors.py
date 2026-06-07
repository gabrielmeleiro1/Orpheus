"""Provider runtime errors."""

from __future__ import annotations


class TTSRuntimeUnavailable(RuntimeError):
    """Requested runtime provider is not registered or installed."""


class TTSRuntimeInitializationError(RuntimeError):
    """Runtime provider failed during startup."""


class TTSRuntimeMemoryError(RuntimeError):
    """Runtime provider failed due to memory pressure."""
