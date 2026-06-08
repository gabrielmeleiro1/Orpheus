"""Service-level TTS engine session cache tests."""

from __future__ import annotations

from modules.app.services.tts_session import TtsEngineSession
from modules.pipeline.models import PipelineConfig
from modules.tts.runtime_spec import TTSRuntimeSpec


def test_tts_engine_session_cache_key_uses_runtime_spec_identity(tmp_path) -> None:
    first_spec = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model="Kokoro-82M",
        quantization="bf16",
        device="cuda:0",
        runtime_options=TTSRuntimeSpec.canonicalize_runtime_options({"dtype": "fp16"}),
    )
    second_spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        model="Kokoro-82M",
        provider="mlx",
        device="mps",
        options={"dtype": "fp16"},
    )
    first_config = PipelineConfig(
        output_dir=tmp_path / "one",
        temp_dir=tmp_path / "temp-one",
        tts_runtime=first_spec,
    )
    second_config = PipelineConfig(
        output_dir=tmp_path / "two",
        temp_dir=tmp_path / "temp-two",
        tts_runtime=second_spec,
    )

    assert TtsEngineSession.cache_key(first_config) != TtsEngineSession.cache_key(
        second_config
    )
    assert first_spec.cache_key() in TtsEngineSession.cache_key(first_config)


def test_tts_engine_session_cache_key_includes_chunk_size(tmp_path) -> None:
    runtime_spec = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model="Kokoro-82M",
        quantization="bf16",
        device="cuda:0",
    )
    first_config = PipelineConfig(
        output_dir=tmp_path / "one",
        temp_dir=tmp_path / "temp-one",
        tts_runtime=runtime_spec,
        chunk_size=700,
    )
    second_config = PipelineConfig(
        output_dir=tmp_path / "two",
        temp_dir=tmp_path / "temp-two",
        tts_runtime=runtime_spec,
        chunk_size=900,
    )

    assert TtsEngineSession.cache_key(first_config) != TtsEngineSession.cache_key(
        second_config
    )


def test_tts_engine_session_cache_key_derives_spec_for_legacy_config() -> None:
    class _LegacyConfig:
        keep_tts_engine_warm = True
        tts_engine = "kokoro"
        tts_model_name = "Kokoro-82M"
        tts_quantization = "bf16"
        tts_runtime = None
        chunk_size = 700

    expected_spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        model="Kokoro-82M",
    )

    assert TtsEngineSession.cache_key(_LegacyConfig()) == (
        expected_spec.cache_key(),
        700,
    )
