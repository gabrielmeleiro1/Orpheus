"""Tests for the CUDA/CPU TTS performance optimizations.

Covers: package-runtime pipeline caching, opt-in torch.compile request
resolution, provider-aware file-batch capability, streamed chapter RMS
precompute parity, and in-memory-first batch result consumption.
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from modules.pipeline.stages.encoding import _measure_pcm16_wav_rms
from modules.pipeline.stages.synthesis import (
    ChapterAudioAccumulator,
    _consume_batch_result_audio,
)
from modules.tts import model_cache
from modules.tts.runtime_spec import TTSRuntimeSpec
from modules.tts.runtimes import KokoroRuntimeConfig
from modules.tts.runtimes.kokoro_package import (
    PackageKokoroRuntime,
    _torch_compile_requested,
)
from modules.tts.strategies.kokoro import KokoroConfig, KokoroTTSStrategy

TORCH_COMPILE_ENV = "AUDIOBOOK_TTS_TORCH_COMPILE"


@pytest.fixture(autouse=True)
def _clean_model_cache():
    model_cache.clear_model_cache()
    yield
    model_cache.clear_model_cache()


def _package_runtime_config(provider: str = "cpu") -> KokoroRuntimeConfig:
    return KokoroRuntimeConfig(
        runtime_spec=TTSRuntimeSpec(
            provider=provider,
            engine="kokoro",
            model="hexgrad/Kokoro-82M",
            quantization="bf16",
        ),
        model_name="hexgrad/Kokoro-82M",
        quantization="bf16",
        sample_rate=24000,
    )


class _FakeKPipeline:
    instances = 0

    def __init__(self, **kwargs: object) -> None:
        type(self).instances += 1
        self.kwargs = kwargs

    def __call__(self, text: str, *, voice: str, speed: float = 1.0):
        yield text, "phonemes", np.ones(4, dtype=np.float32)


@pytest.fixture
def fake_kokoro(monkeypatch: pytest.MonkeyPatch) -> type[_FakeKPipeline]:
    _FakeKPipeline.instances = 0
    monkeypatch.setitem(sys.modules, "kokoro", SimpleNamespace(KPipeline=_FakeKPipeline))
    return _FakeKPipeline


# ---------------------------------------------------------------------------
# torch.compile request resolution
# ---------------------------------------------------------------------------


def test_torch_compile_env_var_enables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TORCH_COMPILE_ENV, "1")
    assert _torch_compile_requested(None) is True
    assert _torch_compile_requested({}) is True


def test_torch_compile_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TORCH_COMPILE_ENV, raising=False)
    assert _torch_compile_requested(None) is False
    assert _torch_compile_requested({"other": True}) is False


def test_torch_compile_explicit_option_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TORCH_COMPILE_ENV, "1")
    assert _torch_compile_requested({"torch_compile": False}) is False
    assert _torch_compile_requested({"torch_compile": "0"}) is False

    monkeypatch.delenv(TORCH_COMPILE_ENV, raising=False)
    assert _torch_compile_requested({"torch_compile": True}) is True
    assert _torch_compile_requested({"torch_compile": "yes"}) is True


# ---------------------------------------------------------------------------
# Package runtime pipeline caching
# ---------------------------------------------------------------------------


def test_package_runtime_load_reuses_cached_pipeline(fake_kokoro) -> None:
    first = PackageKokoroRuntime(_package_runtime_config())
    first.load()
    assert fake_kokoro.instances == 1

    second = PackageKokoroRuntime(_package_runtime_config())
    second.load()
    assert fake_kokoro.instances == 1, "same-spec load must hit the model cache"


def test_package_runtime_unload_keep_in_cache_false_evicts(fake_kokoro) -> None:
    runtime = PackageKokoroRuntime(_package_runtime_config())
    runtime.load()
    runtime.unload(keep_in_cache=False)

    fresh = PackageKokoroRuntime(_package_runtime_config())
    fresh.load()
    assert fake_kokoro.instances == 2, "eviction must force a fresh pipeline build"


def test_package_runtime_unload_default_keeps_cache(fake_kokoro) -> None:
    runtime = PackageKokoroRuntime(_package_runtime_config())
    runtime.load()
    runtime.unload()

    fresh = PackageKokoroRuntime(_package_runtime_config())
    fresh.load()
    assert fake_kokoro.instances == 1


def test_package_runtime_cache_key_separates_compile_state(
    fake_kokoro, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(TORCH_COMPILE_ENV, raising=False)
    eager = PackageKokoroRuntime(_package_runtime_config())
    compiled = PackageKokoroRuntime(_package_runtime_config())
    compiled._config = KokoroRuntimeConfig(
        **{
            **eager._config.__dict__,
            "runtime_options": {"torch_compile": True},
        }
    )
    assert eager._cache_key() != compiled._cache_key()


def test_package_runtime_compile_failure_restores_eager_decoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pipeline whose compiled decoder cannot run must fall back to eager."""
    eager_decoder = object()

    class _FakeModelPipeline(_FakeKPipeline):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            self.model = SimpleNamespace(decoder=eager_decoder)

        def __call__(self, text: str, *, voice: str, speed: float = 1.0):
            if self.model.decoder is not eager_decoder:
                raise RuntimeError("BackendCompilerFailed: no usable backend")
            return super().__call__(text, voice=voice, speed=speed)

    monkeypatch.setitem(
        sys.modules, "kokoro", SimpleNamespace(KPipeline=_FakeModelPipeline)
    )
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(compile=lambda module, dynamic: object()),
    )
    monkeypatch.setenv(TORCH_COMPILE_ENV, "1")

    runtime = PackageKokoroRuntime(_package_runtime_config())
    runtime.load()

    assert runtime._pipeline.model.decoder is eager_decoder
    audio = runtime.synthesize("hello", "am_adam")
    assert audio.shape == (4,)


# ---------------------------------------------------------------------------
# Provider-aware file-batch capability
# ---------------------------------------------------------------------------


def _strategy_with_provider(provider: str | None) -> KokoroTTSStrategy:
    config = KokoroConfig()
    if provider is not None:
        config.runtime_spec = TTSRuntimeSpec(
            provider=provider,
            engine="kokoro",
            model="hexgrad/Kokoro-82M",
            quantization="bf16",
        )
    return KokoroTTSStrategy(config)


def test_file_batch_results_only_for_mlx_provider() -> None:
    assert _strategy_with_provider("mlx").supports_file_batch_results is True
    assert _strategy_with_provider("cuda").supports_file_batch_results is False
    assert _strategy_with_provider("cpu").supports_file_batch_results is False
    # Unresolved spec defaults to MLX, matching the capabilities property.
    assert _strategy_with_provider(None).supports_file_batch_results is True


# ---------------------------------------------------------------------------
# Streamed chapter RMS precompute
# ---------------------------------------------------------------------------


def test_streamed_accumulator_rms_matches_wav_scan(tmp_path: Path) -> None:
    chapter_wav = tmp_path / "chapter.wav"
    accumulator = ChapterAudioAccumulator(
        output_path=chapter_wav,
        sample_rate=24000,
        max_in_memory_samples=10,  # force streaming after the first segment
    )
    rng = np.random.default_rng(7)
    for _ in range(4):
        accumulator.add_segment(rng.uniform(-0.8, 0.8, size=4001).astype(np.float32))
    result = accumulator.finalize()
    accumulator.close()

    assert result == chapter_wav
    assert accumulator.pcm16_rms() == pytest.approx(
        _measure_pcm16_wav_rms(chapter_wav), abs=0.0
    )


def test_streamed_accumulator_rms_includes_file_segments(tmp_path: Path) -> None:
    chunk_wav = tmp_path / "chunk.wav"
    samples = (np.linspace(-0.5, 0.5, 4800) * 32767.0).astype(np.int16)
    with wave.open(str(chunk_wav), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(samples.tobytes())

    chapter_wav = tmp_path / "chapter.wav"
    accumulator = ChapterAudioAccumulator(
        output_path=chapter_wav,
        sample_rate=24000,
        max_in_memory_samples=10,
    )
    accumulator.add_segment(np.full(64, 0.25, dtype=np.float32))
    accumulator.add_file(chunk_wav)
    accumulator.finalize()
    accumulator.close()

    assert accumulator.pcm16_rms() == pytest.approx(
        _measure_pcm16_wav_rms(chapter_wav), abs=0.0
    )


def test_in_memory_accumulator_reports_no_rms(tmp_path: Path) -> None:
    accumulator = ChapterAudioAccumulator(
        output_path=tmp_path / "chapter.wav",
        sample_rate=24000,
        max_in_memory_samples=1_000_000,
    )
    accumulator.add_segment(np.full(100, 0.5, dtype=np.float32))
    result = accumulator.finalize()
    accumulator.close()

    assert isinstance(result, np.ndarray)
    assert accumulator.pcm16_rms() is None


# ---------------------------------------------------------------------------
# In-memory-first batch result consumption
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, audio, audio_path) -> None:
        self.audio = audio
        self.audio_path = audio_path


def _accumulator(tmp_path: Path) -> ChapterAudioAccumulator:
    return ChapterAudioAccumulator(
        output_path=tmp_path / "chapter.wav",
        sample_rate=24000,
        max_in_memory_samples=1_000_000,
    )


def test_consume_prefers_in_memory_audio_and_discards_file(tmp_path: Path) -> None:
    chunk_wav = tmp_path / "chunk.wav"
    chunk_wav.write_bytes(b"not read")
    accumulator = _accumulator(tmp_path)

    result = _Result(np.full(8, 0.1, dtype=np.float32), chunk_wav)
    _consume_batch_result_audio(accumulator, result)

    assert result.audio is None
    assert result.audio_path is None
    assert not chunk_wav.exists(), "byproduct chunk file should be removed"
    assert isinstance(accumulator.finalize(), np.ndarray)
    accumulator.close()


def test_consume_falls_back_to_file_when_no_audio(tmp_path: Path) -> None:
    chunk_wav = tmp_path / "chunk.wav"
    samples = np.full(16, 1000, dtype=np.int16)
    with wave.open(str(chunk_wav), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(samples.tobytes())

    accumulator = _accumulator(tmp_path)
    result = _Result(None, chunk_wav)
    _consume_batch_result_audio(accumulator, result)

    assert not chunk_wav.exists()
    final = accumulator.finalize()
    accumulator.close()
    assert final == accumulator.output_path
