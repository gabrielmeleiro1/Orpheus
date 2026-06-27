"""Contract tests for ConversionRuntimeOptions field parity."""

from __future__ import annotations

from modules.contracts.conversion_runtime_options import (
    CONVERSION_RUNTIME_OPTION_SPECS,
    ConversionRuntimeOptions,
    PARALLEL_WARMUP_RUNTIME_OPTION_NAMES,
    conversion_runtime_option_field_names,
    parallel_warmup_runtime_option_specs,
)
from modules.web.routes.conversions import ConversionRuntimeOptionsRequest
from modules.web.routes.options import ParallelWarmupRequest


def test_conversion_runtime_option_specs_match_dataclass_fields() -> None:
    spec_names = conversion_runtime_option_field_names()
    dataclass_names = frozenset(ConversionRuntimeOptions.__dataclass_fields__)

    assert spec_names == dataclass_names
    assert len(CONVERSION_RUNTIME_OPTION_SPECS) == len(spec_names)


def test_conversion_runtime_options_request_matches_dataclass_fields() -> None:
    dataclass_names = frozenset(ConversionRuntimeOptions.__dataclass_fields__)
    api_names = frozenset(ConversionRuntimeOptionsRequest.model_fields)

    assert api_names == dataclass_names


def test_conversion_runtime_options_request_round_trip() -> None:
    request = ConversionRuntimeOptionsRequest(
        voice="af_bella",
        speed=1.2,
        tts_engine="kokoro",
        cleaner_backend="cuda",
        max_parallel_chapters=3,
    )
    options = request.to_app_options()

    assert options == ConversionRuntimeOptions(
        voice="af_bella",
        speed=1.2,
        tts_engine="kokoro",
        tts_quantization=None,
        tts_provider=None,
        tts_device=None,
        cleaner_model=None,
        cleaner_backend="cuda",
        cleaner_cuda_device=None,
        cleaner_cuda_quantization=None,
        cleaner_api_model=None,
        cleaner_api_key=None,
        tts_options=None,
        parallel_worker_mode=None,
        enable_parallel=None,
        max_parallel_chapters=3,
    )


def test_parallel_warmup_request_uses_contract_subset() -> None:
    warmup_names = frozenset(ParallelWarmupRequest.model_fields)
    expected_names = frozenset(spec.name for spec in parallel_warmup_runtime_option_specs())

    assert warmup_names == expected_names
    assert warmup_names <= PARALLEL_WARMUP_RUNTIME_OPTION_NAMES
    assert warmup_names <= conversion_runtime_option_field_names()
