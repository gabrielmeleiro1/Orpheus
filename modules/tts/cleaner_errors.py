"""Text cleaner runtime errors for local MLX/CUDA and API backends."""

from __future__ import annotations

import re

# Recognize allocation failures, not generic device or model-path words.
# In particular, "bloom", "/memory/", and an unsupported Metal device are
# not evidence that retrying with a smaller memory budget can help.
_MEMORY_FAILURE = re.compile(
    r"out[ -]of[ -]memory|"
    r"(?:cannot|can't|failed to|unable to) allocate (?:\w+ )?memory|"
    r"(?:insufficient|exhausted) (?:device )?(?:memory|vram)|"
    r"(?:memory|vram) (?:exhausted|pressure)|\bvram overflow\b|"
    r"std::bad_alloc|cudaErrorMemoryAllocation|CUBLAS_STATUS_ALLOC_FAILED|"
    r"(?<![\w/\\.-])oom(?![\w/\\.-])",
    re.IGNORECASE,
)


def is_memory_pressure_error(message: str) -> bool:
    """Recognize known provider allocation failures in third-party messages."""
    return bool(_MEMORY_FAILURE.search(str(message or "")))


class CleanerRuntimeUnavailable(RuntimeError):
    """Requested cleaner runtime is not registered or installed."""


class CleanerRuntimeMemoryError(RuntimeError):
    """Cleaner runtime failed due to memory pressure."""
