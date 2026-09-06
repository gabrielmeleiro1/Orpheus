"""Contract tests for ConversionRuntimeOptions field parity."""

from __future__ import annotations

import pickle

import pytest
from pydantic import ValidationError

from modules.contracts.conversion_runtime_options import (
    ConversionRuntimeOptions,
    conversion_runtime_option_field_names,
)
from modules.web.routes.options import ParallelWarmupRequest
from modules.web.schemas.conversions import ConversionRuntimeOptionsRequest


def test_conversion_runtime_options_survive_worker_serialization() -> None:
    options = ConversionRuntimeOptions(voice="af_heart", tts_options={"device": "cpu"})
    assert pickle.loads(pickle.dumps(options)) == options


def test_conversion_runtime_option_name_helper_matches_dataclass_fields() -> None:
    dataclass_names = frozenset(ConversionRuntimeOptions.__dataclass_fields__)

    assert conversion_runtime_option_field_names() == dataclass_names


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


@pytest.mark.parametrize("speed", [0.49, 3.01, float("nan"), float("inf")])
def test_conversion_runtime_options_request_rejects_invalid_speed(speed: float) -> None:
    with pytest.raises(ValidationError):
        ConversionRuntimeOptionsRequest(speed=speed)


def test_conversion_runtime_options_request_rejects_invalid_worker_count() -> None:
    with pytest.raises(ValidationError):
        ConversionRuntimeOptionsRequest(max_parallel_chapters=0)


def test_conversion_runtime_option_defaults_match_api_contract() -> None:
    assert ConversionRuntimeOptions() == ConversionRuntimeOptionsRequest().to_app_options()


def test_parallel_warmup_request_uses_contract_subset() -> None:
    warmup_names = frozenset(ParallelWarmupRequest.model_fields)
    expected_names = frozenset(
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

    assert warmup_names == expected_names
    assert warmup_names <= conversion_runtime_option_field_names()
