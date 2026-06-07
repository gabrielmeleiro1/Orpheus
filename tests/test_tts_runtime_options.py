"""Typed TTS runtime option parsing tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from unittest.mock import MagicMock

from modules.app.config import AppConfig
from modules.app.services.conversion_runtime import (
    ConversionRuntimeOptions,
    RuntimeOptionsResolver,
)
from modules.tts.runtime_options import (
    PROVIDER_QUANTIZATIONS,
    TTSRuntimeOptions,
    validate_device,
    validate_provider_options,
    validate_quantization,
    validate_runtime_spec_provider_compatibility,
)


def test_legacy_payload_quantization_resolves_quantization() -> None:
    options = TTSRuntimeOptions.from_payload({"quantization": "q4"})

    assert options.quantization == "q4"


def test_legacy_tts_model_name_alias_resolves_model_name() -> None:
    options = TTSRuntimeOptions.from_payload({"tts_model_name": "local/kokoro"})

    assert options.model_name == "local/kokoro"


def test_cuda_shaped_payload_resolves_typed_fields_and_provider_options() -> None:
    options = TTSRuntimeOptions.from_payload(
        {
            "provider": "cuda",
            "device": "cuda:1",
            "dtype": "fp16",
            "compile_mode": "cuda_graph",
            "batch_policy": "static",
            "model_path": "/models/kokoro.onnx",
            "provider_options": {"use_tf32": 1, "device_id": 1},
        }
    )

    assert options.provider == "cuda"
    assert options.device == "cuda:1"
    assert options.dtype == "fp16"
    assert options.compile_mode == "cuda_graph"
    assert options.batch_policy == "static"
    assert options.model_path == "/models/kokoro.onnx"
    assert options.provider_options == (("device_id", "1"), ("use_tf32", "1"))
    assert options.runtime_provider_options == (("device_id", 1), ("use_tf32", 1))


def test_unknown_top_level_tts_option_fails_with_specific_message() -> None:
    with pytest.raises(ValueError, match="Unsupported top-level TTS option 'cuda_graphs'"):
        TTSRuntimeOptions.from_payload({"cuda_graphs": "on"})


def test_provider_options_are_sorted_and_string_normalized() -> None:
    options = TTSRuntimeOptions.from_payload(
        {"provider_options": {"z": True, "a": 2, "none": None}}
    )

    assert options.provider_options == (("a", "2"), ("none", "null"), ("z", "true"))
    assert options.runtime_provider_options == (("a", 2), ("none", None), ("z", True))


def test_tts_engine_precedence_explicit_over_config(tmp_path) -> None:
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
        tts_engine="config-default-engine",
        tts_quantization="bf16",
    )
    resolver = RuntimeOptionsResolver(
        config=config,
        cleaner_api_limiter=MagicMock(),
        runtime_validator=lambda *args, **kwargs: None,
    )

    spec = resolver.resolve_tts_runtime_spec(
        runtime_options=ConversionRuntimeOptions(
            tts_engine="explicit-engine",
            tts_quantization="bf16",
        )
    )

    assert spec.engine == "explicit-engine"


def test_runtime_options_precedence_prefers_explicit_quantization_over_payload(
    tmp_path,
) -> None:
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
        tts_quantization="bf16",
    )

    options = TTSRuntimeOptions.from_runtime_options(
        ConversionRuntimeOptions(
            tts_quantization="bf16",
            tts_options={"quantization": "q4", "provider": "mlx"},
        ),
        config,
    )
    spec = options.to_runtime_spec("kokoro", config)

    assert options.quantization == "bf16"
    assert spec.quantization == "bf16"
    assert spec.provider == "mlx"


def test_validation_helpers_reject_invalid_cuda_device_and_provider_options() -> None:
    validate_quantization("cuda", "fp16")
    validate_device("cuda", "cuda:0")
    validate_provider_options("cuda", {"device_id": "0", "enable_cuda_graph": "1"})

    with pytest.raises(ValueError, match="CUDA device"):
        validate_device("cuda", "mps")
    with pytest.raises(ValueError, match="Unsupported CUDA provider option"):
        validate_provider_options("cuda", {"not_a_cuda_option": "1"})


def test_from_payload_defers_quantization_validation_until_runtime_options() -> None:
    options = TTSRuntimeOptions.from_payload(
        {"provider": "cuda", "quantization": "fp16"},
    )

    assert options.provider == "cuda"
    assert options.quantization == "fp16"


def test_from_runtime_options_rejects_cuda_precision_after_precedence(tmp_path) -> None:
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "app.db",
        tts_quantization="bf16",
    )

    with pytest.raises(ValueError, match="Kokoro provider 'cuda' is not supported"):
        TTSRuntimeOptions.from_runtime_options(
            ConversionRuntimeOptions(
                tts_quantization="fp16",
                tts_provider="cuda",
                tts_options={"quantization": "bf16", "provider": "mlx"},
            ),
            config,
        ).to_runtime_spec("kokoro", config)


def test_provider_quantizations_include_cuda_fp16_and_fp32() -> None:
    assert "fp16" in PROVIDER_QUANTIZATIONS["cuda"]
    assert "fp32" in PROVIDER_QUANTIZATIONS["cuda"]


def test_validate_runtime_spec_provider_compatibility_rejects_cuda_kokoro() -> None:
    from modules.tts.runtime_spec import TTSRuntimeSpec

    spec = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model="Kokoro-82M",
        quantization="bf16",
        device="cuda:0",
    )

    with pytest.raises(ValueError, match="Kokoro provider 'cuda' is not supported"):
        validate_runtime_spec_provider_compatibility(spec)


def test_provider_only_in_provider_options_resolves_cuda_spec() -> None:
    options = TTSRuntimeOptions.from_payload(
        {
            "device": "cuda:0",
            "dtype": "fp16",
            "provider_options": {"provider": "cuda", "device_id": 0},
        }
    )

    assert options.provider == "cuda"
    assert options.device == "cuda:0"
    assert "provider" not in dict(options.provider_options)

    with pytest.raises(ValueError, match="Kokoro provider 'cuda' is not supported"):
        options.to_runtime_spec("kokoro", {"tts_quantization": "bf16"})


def test_device_cuda_infers_provider_without_explicit_provider() -> None:
    options = TTSRuntimeOptions.from_payload({"device": "cuda:0", "dtype": "fp16"})

    assert options.provider == "cuda"
    assert options.device == "cuda:0"

    with pytest.raises(ValueError, match="Kokoro provider 'cuda' is not supported"):
        options.to_runtime_spec("kokoro", {"tts_quantization": "bf16"})


def test_uninferable_device_without_provider_fails() -> None:
    with pytest.raises(ValueError, match="Cannot infer TTS provider"):
        TTSRuntimeOptions.from_payload({"device": "mps"})


def test_spec_options_preserves_typed_runtime_provider_options() -> None:
    options = TTSRuntimeOptions(
        provider="mlx",
        device="mps",
        dtype="fp16",
        runtime_provider_options=(("device_id", 1), ("use_tf32", True)),
    )

    assert options._spec_options() == {  # noqa: SLF001
        "device_id": 1,
        "dtype": "fp16",
        "use_tf32": True,
    }

    spec = options.to_runtime_spec("kokoro", {"tts_quantization": "bf16"})

    assert spec.factory_kwargs()["runtime_options"] == {
        "device_id": 1,
        "dtype": "fp16",
        "use_tf32": True,
    }


def test_from_serialized_coerces_legacy_provider_options_to_runtime_types() -> None:
    options = TTSRuntimeOptions.from_serialized(
        {
            "provider": "cuda",
            "device": "cuda:0",
            "provider_options": {"device_id": "1", "use_tf32": "true"},
        }
    )

    assert options.runtime_provider_options == (("device_id", 1), ("use_tf32", True))
    assert options._spec_options() == {"device_id": 1, "use_tf32": True}  # noqa: SLF001


def test_pipeline_and_worker_runtime_specs_stay_aligned(tmp_path: Path) -> None:
    from modules.pipeline.models import PipelineConfig
    from modules.pipeline.worker_config import build_worker_init_config, build_worker_pipeline_config
    from modules.pipeline.worker_runtime import _runtime_spec_from_worker_config

    runtime_options = TTSRuntimeOptions(
        quantization="bf16",
        provider="mlx",
        device="mps",
        dtype="fp16",
        runtime_provider_options=(("device_id", 0),),
    )
    config = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        tts_runtime_options=runtime_options,
    )
    worker_init_config = build_worker_init_config(config)
    worker_config = build_worker_pipeline_config(worker_init_config["pipeline_config"])
    worker_runtime_spec = _runtime_spec_from_worker_config(worker_init_config)

    assert worker_runtime_spec == config.tts_runtime
    assert worker_config.tts_runtime == config.tts_runtime
    assert worker_config.tts_runtime.provider == "mlx"
