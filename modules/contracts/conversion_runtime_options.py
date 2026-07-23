"""Canonical ConversionRuntimeOptions field definitions."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import Field


@dataclass(frozen=True)
class RuntimeOptionFieldSpec:
    name: str
    annotation: Any
    default: Any
    pydantic_constraints: dict[str, Any] = dataclasses.field(default_factory=dict)


CONVERSION_RUNTIME_OPTION_SPECS: tuple[RuntimeOptionFieldSpec, ...] = (
    RuntimeOptionFieldSpec("voice", str, "am_adam"),
    RuntimeOptionFieldSpec("speed", float, 1.0, {"ge": 0.5, "le": 3.0}),
    RuntimeOptionFieldSpec("tts_engine", Optional[str], None),
    RuntimeOptionFieldSpec("tts_quantization", Optional[str], None),
    RuntimeOptionFieldSpec("tts_provider", Optional[str], None),
    RuntimeOptionFieldSpec("tts_device", Optional[str], None),
    RuntimeOptionFieldSpec("cleaner_model", Optional[str], None),
    RuntimeOptionFieldSpec("cleaner_backend", Optional[str], None),
    RuntimeOptionFieldSpec("cleaner_cuda_device", Optional[str], None),
    RuntimeOptionFieldSpec("cleaner_cuda_quantization", Optional[str], None),
    RuntimeOptionFieldSpec("cleaner_api_model", Optional[str], None),
    RuntimeOptionFieldSpec("cleaner_api_key", Optional[str], None),
    RuntimeOptionFieldSpec("tts_options", Optional[dict[str, Any]], None),
    RuntimeOptionFieldSpec("parallel_worker_mode", Optional[str], None),
    RuntimeOptionFieldSpec("enable_parallel", Optional[bool], None),
    RuntimeOptionFieldSpec(
        "max_parallel_chapters",
        Optional[int],
        None,
        {"ge": 1},
    ),
)

ConversionRuntimeOptions = dataclasses.make_dataclass(
    "ConversionRuntimeOptions",
    [
        (spec.name, spec.annotation, spec.default)
        for spec in CONVERSION_RUNTIME_OPTION_SPECS
    ],
    frozen=True,
)
ConversionRuntimeOptions.__doc__ = (
    "Runtime selections shared by start and repair conversions."
)

PARALLEL_WARMUP_RUNTIME_OPTION_NAMES: frozenset[str] = frozenset(
    {
        "voice",
        "speed",
        "tts_engine",
        "tts_quantization",
        "cleaner_model",
        "cleaner_backend",
        "cleaner_cuda_device",
        "cleaner_cuda_quantization",
        "cleaner_api_model",
        "cleaner_api_key",
        "max_parallel_chapters",
    }
)

PARALLEL_WARMUP_RUNTIME_OVERRIDES: dict[str, RuntimeOptionFieldSpec] = {
    "max_parallel_chapters": RuntimeOptionFieldSpec(
        "max_parallel_chapters",
        int,
        4,
        {"ge": 1},
    ),
}


def conversion_runtime_option_field_names() -> frozenset[str]:
    return frozenset(spec.name for spec in CONVERSION_RUNTIME_OPTION_SPECS)


def parallel_warmup_runtime_option_specs() -> tuple[RuntimeOptionFieldSpec, ...]:
    return tuple(
        PARALLEL_WARMUP_RUNTIME_OVERRIDES.get(spec.name, spec)
        for spec in CONVERSION_RUNTIME_OPTION_SPECS
        if spec.name in PARALLEL_WARMUP_RUNTIME_OPTION_NAMES
    )


def build_runtime_option_pydantic_fields(
    specs: tuple[RuntimeOptionFieldSpec, ...],
) -> dict[str, tuple[Any, Any]]:
    fields: dict[str, tuple[Any, Any]] = {}
    for spec in specs:
        if spec.pydantic_constraints:
            fields[spec.name] = (
                spec.annotation,
                Field(default=spec.default, **spec.pydantic_constraints),
            )
        else:
            fields[spec.name] = (spec.annotation, spec.default)
    return fields
