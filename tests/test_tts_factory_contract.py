"""Factory/strategy contract tests for pluggable TTS engines."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from modules.tts.factory import TTSEngineFactory
from modules.tts.runtime_spec import TTSRuntimeSpec
from modules.tts.strategies.base import SynthesisItem, TTSConfig, TTSStrategy


EXPECTED_KOKORO_VOICE_IDS = {
    "af_alloy",
    "af_aoede",
    "af_bella",
    "af_heart",
    "af_jessica",
    "af_kore",
    "af_nicole",
    "af_nova",
    "af_river",
    "af_sarah",
    "af_sky",
    "am_adam",
    "am_echo",
    "am_eric",
    "am_fenrir",
    "am_liam",
    "am_michael",
    "am_onyx",
    "am_puck",
    "am_santa",
    "bf_alice",
    "bf_emma",
    "bf_isabella",
    "bf_lily",
    "bm_daniel",
    "bm_fable",
    "bm_george",
    "bm_lewis",
}


def test_engine_info_contains_capability_contract() -> None:
    engines = TTSEngineFactory.available_engines()
    assert "kokoro" in engines

    for engine_name in engines:
        info = TTSEngineFactory.get_engine_info(engine_name)
        assert info["id"] == engine_name
        assert isinstance(info["display_name"], str)
        assert isinstance(info["voices"], list)
        assert isinstance(info["capabilities"], dict)
        assert "supports_batching" in info["capabilities"]
        assert "supports_native_batching" in info["capabilities"]
        assert "batching_mode" in info["capabilities"]
        assert "supports_streaming" in info["capabilities"]
        assert isinstance(info["supported_quantizations"], list)


def test_factory_create_strategy_metadata_without_loading_model() -> None:
    strategy = TTSEngineFactory.create("kokoro")
    assert strategy.name == "kokoro"
    assert strategy.display_name
    assert isinstance(strategy.supported_voices, list)
    assert strategy.capabilities.supports_batching is True
    assert strategy.capabilities.supports_native_batching is False
    assert strategy.capabilities.batching_mode == "threaded"


def test_factory_create_from_absent_runtime_model_keeps_kokoro_default() -> None:
    spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        provider="mlx",
    )

    strategy = TTSEngineFactory.create("kokoro", **spec.factory_kwargs())

    assert strategy.config.model_name == "mlx-community/Kokoro-82M"
    assert strategy.config.runtime_spec is spec


def test_conversion_engine_provider_filter_accepts_mlx_audio_alias() -> None:
    assert TTSEngineFactory.available_conversion_engines(provider="mlx") == ["kokoro"]
    assert TTSEngineFactory.available_conversion_engines(provider="mlx-audio") == ["kokoro"]
    assert TTSEngineFactory.available_conversion_engines(provider="mlx_audio") == ["kokoro"]


def test_conversion_engine_provider_filter_accepts_package_kokoro_providers() -> None:
    assert TTSEngineFactory.available_conversion_engines(provider="cuda") == ["kokoro"]
    assert TTSEngineFactory.available_conversion_engines(provider="cpu") == ["kokoro"]


def test_kokoro_strategy_exposes_full_known_voice_catalog() -> None:
    strategy = TTSEngineFactory.create("kokoro")
    voice_ids = {voice.id for voice in strategy.supported_voices}

    assert voice_ids == EXPECTED_KOKORO_VOICE_IDS


def test_kokoro_strategy_exposes_english_only_voices() -> None:
    strategy = TTSEngineFactory.create("kokoro")
    languages = {voice.language for voice in strategy.supported_voices}

    assert languages <= {"en-US", "en-GB"}


def test_kokoro_strategy_only_advertises_runtime_proven_quantization() -> None:
    strategy = TTSEngineFactory.create("kokoro")

    assert strategy.capabilities.supported_quantizations == ("bf16",)
    assert strategy.describe_engine_options()["quantization"]["enum"] == ["bf16"]


class _SequentialFallbackStrategy(TTSStrategy):
    def __init__(self) -> None:
        super().__init__(TTSConfig())
        self._voices = [SimpleNamespace(id="voice-1", name="Voice 1", language="en-US", gender="neutral", description="test voice")]

    @property
    def name(self) -> str:
        return "sequential-fallback"

    @property
    def display_name(self) -> str:
        return "Sequential Fallback"

    @property
    def version(self) -> str:
        return "test"

    @property
    def sample_rate(self) -> int:
        return 24000

    @property
    def supported_voices(self):  # noqa: ANN202
        return self._voices

    @property
    def supports_batching(self) -> bool:
        return False

    @property
    def supports_streaming(self) -> bool:
        return False

    def load(self) -> None:
        self._is_loaded = True

    def unload(self) -> None:
        self._is_loaded = False

    def synthesize(self, text: str, voice: str, speed: float = 1.0, output_path=None):  # noqa: ANN001
        _ = voice, speed, output_path
        if text == "explode":
            raise RuntimeError("boom")
        return np.full(3, len(text), dtype=np.float32)


def test_base_strategy_batch_falls_back_to_sequential_items() -> None:
    strategy = _SequentialFallbackStrategy()
    strategy.load()

    progress_updates: list[tuple[int, int]] = []
    results = strategy.synthesize_batch(
        items=[
            SynthesisItem(text="hello", voice="voice-1", speed=1.0, index=0),
            SynthesisItem(text="explode", voice="voice-1", speed=1.0, index=1),
            SynthesisItem(text="bye", voice="voice-1", speed=1.0, index=2),
        ],
        progress_callback=lambda completed, total: progress_updates.append((completed, total)),
    )

    assert [result.index for result in results] == [0, 1, 2]
    assert results[0].audio is not None
    assert results[0].error is None
    assert results[1].audio is None
    assert results[1].error == "boom"
    assert results[2].audio is not None
    assert progress_updates == [(1, 3), (2, 3), (3, 3)]
