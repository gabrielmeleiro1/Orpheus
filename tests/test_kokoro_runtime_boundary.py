"""Kokoro provider-runtime boundary tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from modules.pipeline.model_lifecycle import _create_tts_strategy, _tts_strategy_kwargs
from modules.pipeline.models import PipelineConfig
from modules.tts.runtime_spec import TTSRuntimeSpec
from modules.tts.runtimes import KokoroRuntimeConfig, KokoroRuntimeFactory
from modules.tts.runtimes.errors import TTSRuntimeUnavailable
from modules.tts.strategies.base import SynthesisItem, SynthesisResult
from modules.tts.strategies.kokoro import KokoroConfig, KokoroTTSStrategy


def test_runtime_boundary_modules_are_the_strategy_contract() -> None:
    from modules.tts.runtimes.kokoro_base import (
        KokoroRuntime,
        KokoroRuntimeConfig,
        KokoroRuntimeFactory,
    )
    from modules.tts.runtimes.kokoro_mlx import MLXKokoroRuntime

    assert KokoroRuntime is not None
    assert KokoroRuntimeConfig is not None
    assert KokoroRuntimeFactory is not None
    assert MLXKokoroRuntime.provider == "mlx"


def test_kokoro_strategy_does_not_import_legacy_runtime_provider() -> None:
    source = Path("modules/tts/strategies/kokoro.py").read_text()

    assert "modules.tts.runtime_provider" not in source


class _FakeRuntime:
    provider = "cuda"
    sample_rate = 24000
    average_chunk_time_ms = 12.5

    def __init__(self) -> None:
        self.is_loaded = False
        self.unload_flags: list[bool] = []
        self.synthesis_calls: list[tuple[str, str, float, Path | None]] = []

    @property
    def chunks_processed(self) -> int:
        return len(self.synthesis_calls)

    def load(self) -> None:
        self.is_loaded = True

    def unload(self, *, keep_in_cache: bool = True) -> None:
        self.unload_flags.append(keep_in_cache)
        self.is_loaded = False

    def warmup(
        self,
        *,
        voice: str | None = None,
        speed: float = 1.0,
        probe_text: str = "warmup check",
    ) -> None:
        self.synthesis_calls.append((probe_text, voice or "am_adam", speed, None))

    def synthesize(
        self,
        text: str,
        voice: str,
        speed: float = 1.0,
        output_path: Path | None = None,
    ) -> np.ndarray:
        self.synthesis_calls.append((text, voice, speed, output_path))
        return np.ones(4, dtype=np.float32)

    def iter_synthesize_batch(self, items, progress_callback=None):  # noqa: ANN001
        total = len(items)
        for offset, item in enumerate(items, start=1):
            if progress_callback is not None:
                progress_callback(offset, total)
            yield SynthesisResult(
                audio=self.synthesize(item.text, item.voice, item.speed, item.output_path),
                index=item.index,
            )

    def get_metrics(self) -> dict[str, object]:
        return {"provider": self.provider}


class _FakeRuntimeFactory:
    def __init__(self, runtime: _FakeRuntime) -> None:
        self.runtime = runtime
        self.configs: list[KokoroConfig] = []

    def create_runtime(self, config: KokoroConfig) -> _FakeRuntime:
        self.configs.append(config)
        return self.runtime


def test_kokoro_strategy_delegates_lifecycle_and_synthesis_to_injected_runtime() -> None:
    runtime = _FakeRuntime()
    strategy = KokoroTTSStrategy(
        KokoroConfig(
            runtime_spec=TTSRuntimeSpec(
                provider="cuda",
                engine="kokoro",
                model="Kokoro-82M",
                quantization="bf16",
                device="cuda:0",
            )
        ),
        runtime_factory=_FakeRuntimeFactory(runtime),
    )

    strategy.load()
    audio = strategy.synthesize("Hello", voice="am_adam", speed=1.5)
    progress: list[tuple[int, int]] = []
    results = list(
        strategy.iter_synthesize_batch(
            [SynthesisItem(text="Batch", voice="am_adam", index=7)],
            progress_callback=lambda done, total: progress.append((done, total)),
        )
    )

    assert audio.tolist() == [1.0, 1.0, 1.0, 1.0]
    assert results[0].index == 7
    assert progress == [(1, 1)]
    assert strategy.capabilities.provider == "cuda"
    assert strategy.get_stats()["metrics"] == {"provider": "cuda"}
    strategy.unload(keep_in_cache=False)
    assert runtime.unload_flags == [False]


def test_unsupported_cuda_runtime_provider_fails_at_spec_birth() -> None:
    with pytest.raises(ValueError, match="Kokoro provider 'cuda' is not supported"):
        TTSRuntimeSpec.from_legacy(
            "kokoro",
            "bf16",
            provider="cuda",
            device="cuda:0",
        )


def test_unsupported_cuda_runtime_provider_fails_with_runtime_unavailable() -> None:
    strategy = KokoroTTSStrategy(
        KokoroConfig(
            runtime_spec=TTSRuntimeSpec(
                provider="cuda",
                engine="kokoro",
                model=None,
                quantization="bf16",
                device="cuda:0",
            )
        )
    )

    with pytest.raises(TTSRuntimeUnavailable, match="cuda"):
        strategy.load()


def test_mlx_runtime_engine_config_identity_prefers_runtime_spec() -> None:
    runtime_spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        model="local/spec-kokoro",
        provider="mlx",
        options={"dtype": "fp16"},
    )
    config = KokoroConfig(
        model_name="legacy/default-kokoro",
        quantization="bf16",
        runtime_spec=runtime_spec,
    )

    with patch("modules.tts.engine.TTSEngine") as mock_engine:
        KokoroRuntimeFactory().create_runtime(
            KokoroRuntimeConfig.from_strategy_config(config)
        )

    engine_config = mock_engine.call_args.args[0]
    assert engine_config.model_name == "local/spec-kokoro"
    assert engine_config.quantization == "bf16"
    assert engine_config.runtime_cache_key == repr(runtime_spec.cache_key())


def test_mlx_runtime_engine_config_uses_resolved_kokoro_default_when_model_absent() -> None:
    runtime_spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        provider="mlx",
    )
    config = KokoroConfig(
        model_name="legacy/default-kokoro",
        quantization="bf16",
        runtime_spec=runtime_spec,
    )

    with patch("modules.tts.engine.TTSEngine") as mock_engine:
        KokoroRuntimeFactory().create_runtime(
            KokoroRuntimeConfig.from_strategy_config(config)
        )

    engine_config = mock_engine.call_args.args[0]
    assert engine_config.model_name == "mlx-community/Kokoro-82M"
    assert engine_config.quantization == "bf16"
    assert engine_config.runtime_cache_key == repr(runtime_spec.cache_key())


def test_pipeline_strategy_kwargs_keep_kokoro_default_when_spec_model_absent() -> None:
    runtime_spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        provider="mlx",
    )
    runtime = type(
        "Runtime",
        (),
        {
            "config": PipelineConfig(
                tts_engine="kokoro",
                tts_model_name=None,
                tts_quantization="bf16",
                tts_runtime=runtime_spec,
            )
        },
    )()

    kwargs = _tts_strategy_kwargs(runtime)
    strategy = _create_tts_strategy(runtime)

    assert "model_name" not in kwargs
    assert strategy.config.model_name == "mlx-community/Kokoro-82M"
    assert strategy.config.runtime_spec is runtime_spec
