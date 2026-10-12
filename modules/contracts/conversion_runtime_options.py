"""Canonical application-facing conversion runtime options."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ConversionRuntimeOptions:
    """Runtime selections shared by start and repair conversions."""

    voice: str = "am_adam"
    speed: float = 1.0
    tts_engine: str | None = None
    tts_quantization: str | None = None
    tts_provider: str | None = None
    tts_device: str | None = None
    cleaner_model: str | None = None
    cleaner_backend: str | None = None
    cleaner_cuda_device: str | None = None
    cleaner_cuda_quantization: str | None = None
    cleaner_api_model: str | None = None
    # Excluded from repr so logged or traced options never reveal the key.
    cleaner_api_key: str | None = field(default=None, repr=False)
    tts_options: Mapping[str, object] | None = None
    parallel_worker_mode: str | None = None
    enable_parallel: bool | None = None
    max_parallel_chapters: int | None = None
