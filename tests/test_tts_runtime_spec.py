"""Canonical TTS runtime identity tests."""

from __future__ import annotations

import pytest

from modules.tts.runtime_spec import TTSRuntimeSpec


def test_kokoro_legacy_runtime_defaults_to_cpu_when_mlx_and_cuda_unsupported(monkeypatch) -> None:
    monkeypatch.setattr("modules.tts.runtime_spec.platform.system", lambda: "Linux")
    monkeypatch.setattr("modules.tts.runtime_spec.platform.machine", lambda: "x86_64")
    monkeypatch.setattr("modules.tts.runtime_spec.TTSRuntimeSpec.cuda_available_on_current_platform", lambda: False)

    spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
    )

    assert spec.provider == "cpu"
    assert spec.engine == "kokoro"
    assert spec.resolved_model() == "hexgrad/Kokoro-82M"
    assert spec.quantization == "bf16"


def test_kokoro_legacy_runtime_defaults_to_cuda_when_cuda_available(monkeypatch) -> None:
    monkeypatch.setattr("modules.tts.runtime_spec.platform.system", lambda: "Windows")
    monkeypatch.setattr("modules.tts.runtime_spec.platform.machine", lambda: "AMD64")
    monkeypatch.setattr("modules.tts.runtime_spec.TTSRuntimeSpec.cuda_available_on_current_platform", lambda: True)

    spec = TTSRuntimeSpec.from_legacy("kokoro", "bf16")

    assert spec.provider == "cuda"
    assert spec.resolved_model() == "hexgrad/Kokoro-82M"


def test_kokoro_legacy_runtime_defaults_to_mlx_on_apple_silicon(monkeypatch) -> None:
    monkeypatch.setattr("modules.tts.runtime_spec.platform.system", lambda: "Darwin")
    monkeypatch.setattr("modules.tts.runtime_spec.platform.machine", lambda: "arm64")
    monkeypatch.setattr("modules.tts.runtime_spec.TTSRuntimeSpec.cuda_available_on_current_platform", lambda: True)

    spec = TTSRuntimeSpec.from_legacy("kokoro", "bf16")

    assert spec.provider == "mlx"
    assert spec.resolved_model() == "mlx-community/Kokoro-82M"


def test_kokoro_runtime_canonicalizes_mlx_audio_provider_alias() -> None:
    default_spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        model="mlx-community/Kokoro-82M",
        provider="mlx",
    )
    explicit_alias_spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        model="mlx-community/Kokoro-82M",
        provider="mlx-audio",
    )

    assert explicit_alias_spec.provider == "mlx"
    assert explicit_alias_spec.cache_key() == default_spec.cache_key()


def test_runtime_cache_key_distinguishes_provider_and_device() -> None:
    mlx_spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        model="Kokoro-82M",
        provider="mlx",
        device="mps",
    )
    cuda_spec = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model="Kokoro-82M",
        quantization="bf16",
        device="cuda:0",
    )

    assert mlx_spec.cache_key() != cuda_spec.cache_key()


def test_runtime_cache_key_distinguishes_provider_only() -> None:
    mlx_spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        model="Kokoro-82M",
        provider="mlx",
    )
    cuda_spec = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model="Kokoro-82M",
        quantization="bf16",
    )

    assert mlx_spec.cache_key() != cuda_spec.cache_key()


def test_runtime_cache_key_distinguishes_device_only() -> None:
    first = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model="Kokoro-82M",
        quantization="bf16",
        device="cuda:0",
    )
    second = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model="Kokoro-82M",
        quantization="bf16",
        device="cuda:1",
    )

    assert first.cache_key() != second.cache_key()


def test_unknown_engine_legacy_runtime_defaults_to_local_provider() -> None:
    spec = TTSRuntimeSpec.from_legacy("openai", "full")

    assert spec.provider == "local"


def test_runtime_cache_key_canonicalizes_options() -> None:
    first = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model=None,
        quantization="bf16",
        runtime_options=TTSRuntimeSpec.canonicalize_runtime_options(
            {"dtype": "fp16", "compile": True},
        ),
    )
    second = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model=None,
        quantization="bf16",
        runtime_options=TTSRuntimeSpec.canonicalize_runtime_options(
            {"compile": True, "dtype": "fp16"},
        ),
    )

    assert first.cache_key() == second.cache_key()
    assert first.options == (("compile", "true"), ("dtype", "fp16"))


def test_runtime_cache_key_rejects_uncacheable_option_values() -> None:
    class _Unstable:
        pass

    try:
        TTSRuntimeSpec.canonicalize_options({"compile": _Unstable()})
    except ValueError as exc:
        assert "not stable" in str(exc)
    else:  # pragma: no cover - explicit failure message
        raise AssertionError("Expected unstable option value to be rejected")


def test_runtime_factory_kwargs_include_legacy_and_spec_fields() -> None:
    spec = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model="Kokoro-82M",
        quantization="bf16",
        device="cuda:1",
        runtime_options=TTSRuntimeSpec.canonicalize_runtime_options({"dtype": "fp16"}),
    )

    kwargs = spec.factory_kwargs()

    assert kwargs["model_name"] == "Kokoro-82M"
    assert kwargs["quantization"] == "bf16"
    assert kwargs["provider"] == "cuda"
    assert kwargs["device"] == "cuda:1"
    assert kwargs["runtime_options"] == {"dtype": "fp16"}
    assert kwargs["runtime_spec"] is spec


def test_runtime_factory_kwargs_preserve_json_primitive_runtime_options() -> None:
    options = {
        "use_tf32": True,
        "device_id": 1,
        "temperature": 0.25,
        "optional": None,
        "dtype": "fp16",
    }
    spec = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model=None,
        quantization="bf16",
        runtime_options=TTSRuntimeSpec.canonicalize_runtime_options(options),
    )

    kwargs = spec.factory_kwargs()

    assert spec.options == (
        ("device_id", "1"),
        ("dtype", "fp16"),
        ("optional", "null"),
        ("temperature", "0.25"),
        ("use_tf32", "true"),
    )
    assert kwargs["runtime_options"] == {
        "device_id": 1,
        "dtype": "fp16",
        "optional": None,
        "temperature": 0.25,
        "use_tf32": True,
    }


def test_runtime_factory_kwargs_omit_absent_model_name() -> None:
    spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        provider="mlx",
    )

    kwargs = spec.factory_kwargs()

    assert "model_name" not in kwargs
    assert kwargs["runtime_spec"] is spec


def test_resolved_model_and_cache_key_use_kokoro_default_when_model_absent() -> None:
    spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        provider="mlx",
    )
    explicit_spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        model="mlx-community/Kokoro-82M",
        provider="mlx",
    )

    assert spec.model is None
    assert spec.resolved_model() == "mlx-community/Kokoro-82M"
    assert spec.cache_key() == explicit_spec.cache_key()
    assert spec.engine_config_kwargs() == {
        "model_name": "mlx-community/Kokoro-82M",
        "quantization": "bf16",
        "runtime_cache_key": repr(spec.cache_key()),
    }


def test_cuda_kokoro_default_model_uses_upstream_huggingface_id() -> None:
    spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        provider="cuda",
        device="cuda:0",
    )

    assert spec.model is None
    assert spec.resolved_model() == "hexgrad/Kokoro-82M"


def test_from_dict_coerces_legacy_cuda_string_options_for_runtime_options() -> None:
    payload = {
        "provider": "mlx",
        "engine": "kokoro",
        "model": "Kokoro-82M",
        "quantization": "bf16",
        "device": "mps",
        "options": {
            "device_id": "1",
            "use_tf32": "true",
            "enable_cuda_graph": "false",
            "gpu_mem_limit": "2147483648",
            "dtype": "fp16",
        },
    }

    spec = TTSRuntimeSpec.from_dict(payload)
    kwargs = spec.factory_kwargs()

    assert kwargs["runtime_options"] == {
        "device_id": 1,
        "dtype": "fp16",
        "enable_cuda_graph": False,
        "gpu_mem_limit": 2147483648,
        "use_tf32": True,
    }
    assert spec.options == (
        ("device_id", "1"),
        ("dtype", "fp16"),
        ("enable_cuda_graph", "false"),
        ("gpu_mem_limit", "2147483648"),
        ("use_tf32", "true"),
    )


def test_runtime_spec_round_trips_through_serialized_payload() -> None:
    spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        model="Kokoro-82M",
        provider="mlx",
        device="mps",
        options={"dtype": "fp16", "compile": True},
    )

    payload = spec.to_dict()
    restored = TTSRuntimeSpec.from_dict(payload)

    assert restored == spec
    assert restored is not spec
    assert restored.cache_key() == spec.cache_key()


def test_from_legacy_preserves_cuda_kokoro_identity_until_runtime_load() -> None:
    spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "fp16",
        model="Kokoro-82M",
        provider="cuda",
        device="cuda:0",
        options={"dtype": "fp16", "device_id": "1", "use_tf32": "true"},
    )
    restored = TTSRuntimeSpec.from_dict(spec.to_dict())

    assert spec.provider == "cuda"
    assert spec.device == "cuda:0"
    assert restored == spec
    assert restored.cache_key() == spec.cache_key()
    assert dict(restored.runtime_options) == {
        "device_id": 1,
        "dtype": "fp16",
        "use_tf32": True,
    }


def test_from_dict_preserves_cuda_kokoro_identity_until_runtime_load() -> None:
    spec = TTSRuntimeSpec.from_dict(
        {
            "provider": "cuda",
            "engine": "kokoro",
            "model": "Kokoro-82M",
            "quantization": "bf16",
            "device": "cuda:0",
        }
    )

    assert spec.provider == "cuda"
    assert spec.device == "cuda:0"


def test_runtime_spec_rejects_invalid_cuda_device_shape() -> None:
    with pytest.raises(ValueError, match="CUDA device"):
        TTSRuntimeSpec.from_legacy(
            "kokoro",
            "bf16",
            provider="cuda",
            device="mps",
        )

    with pytest.raises(ValueError, match="CUDA device"):
        TTSRuntimeSpec.from_dict(
            {
                "provider": "cuda",
                "engine": "kokoro",
                "model": "Kokoro-82M",
                "quantization": "bf16",
                "device": "mps",
            }
        )


def test_from_dict_rejects_conflicting_legacy_and_runtime_options() -> None:
    with pytest.raises(ValueError, match="options and runtime_options"):
        TTSRuntimeSpec.from_dict(
            {
                "provider": "cuda",
                "engine": "kokoro",
                "model": "Kokoro-82M",
                "quantization": "bf16",
                "device": "cuda:0",
                "options": {"dtype": "fp16"},
                "runtime_options": {"dtype": "bf16"},
            }
        )


def test_engine_runtime_cache_key_prefers_canonical_runtime_identity() -> None:
    from modules.tts.engine_config import TTSConfig
    from modules.tts.engine_runtime import get_model_cache_key, get_model_path

    runtime_spec = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model="Kokoro-82M",
        quantization="bf16",
        device="cuda:0",
    )
    config = TTSConfig(
        model_name="Kokoro-82M",
        quantization="bf16",
        runtime_cache_key=repr(runtime_spec.cache_key()),
    )

    assert get_model_path(config) == "Kokoro-82M-bf16"
    assert get_model_cache_key(config) == config.runtime_cache_key
