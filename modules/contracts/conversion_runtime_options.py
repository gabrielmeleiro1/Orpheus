"""Canonical application-facing conversion runtime options."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields


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
    cleaner_api_key: str | None = None
    tts_options: Mapping[str, object] | None = None
    parallel_worker_mode: str | None = None
    enable_parallel: bool | None = None
    max_parallel_chapters: int | None = None


def conversion_runtime_option_field_names() -> frozenset[str]:
    return frozenset(field.name for field in fields(ConversionRuntimeOptions))
