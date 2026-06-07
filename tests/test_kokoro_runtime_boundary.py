"""Kokoro provider-runtime boundary tests."""

from __future__ import annotations

import ast
import builtins
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from modules.pipeline.model_lifecycle import _create_tts_strategy, _tts_strategy_kwargs
from modules.pipeline.models import PipelineConfig
from modules.tts.runtime_spec import TTSRuntimeSpec
from modules.tts.runtimes import KokoroRuntimeConfig, KokoroRuntimeFactory
from modules.tts.runtimes.errors import TTSRuntimeUnavailable
from modules.tts.runtimes.kokoro_mlx import MLXKokoroRuntime
from modules.tts.strategies.base import SynthesisItem, SynthesisResult
from modules.tts.strategies.kokoro import KokoroConfig, KokoroTTSStrategy

REPO_ROOT = Path(__file__).resolve().parents[1]


def _imports_runtime_provider(module_path: Path) -> bool:
    tree = ast.parse(module_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "modules.tts.runtime_provider":
                    return True
        elif isinstance(node, ast.ImportFrom) and node.module == "modules.tts.runtime_provider":
            return True
    return False


def test_runtime_boundary_registry_registers_mlx_kokoro() -> None:
    assert ("mlx", "kokoro") in KokoroRuntimeFactory.registered_keys()

    config = KokoroRuntimeConfig(
        runtime_spec=TTSRuntimeSpec(
            provider="mlx",
            engine="kokoro",
            model="mlx-community/Kokoro-82M",
            quantization="bf16",
        ),
        model_name="mlx-community/Kokoro-82M",
        quantization="bf16",
        sample_rate=24000,
    )

    sentinel = object()
    calls: list[KokoroRuntimeConfig] = []

    def fake_builder(runtime_config: KokoroRuntimeConfig) -> object:
        calls.append(runtime_config)
        return sentinel

    key = ("mlx", "kokoro")
    original = KokoroRuntimeFactory._registry[key]
    KokoroRuntimeFactory._registry[key] = fake_builder
    try:
        runtime = KokoroRuntimeFactory().create_runtime(config)
    finally:
        KokoroRuntimeFactory._registry[key] = original

    assert runtime is sentinel
    assert calls == [config]


def test_kokoro_strategy_does_not_import_legacy_runtime_provider() -> None:
    kokoro_path = REPO_ROOT / "modules" / "tts" / "strategies" / "kokoro.py"

    assert not _imports_runtime_provider(kokoro_path)


def test_runtimes_package_import_does_not_load_engine(monkeypatch) -> None:
    sys.modules.pop("modules.tts.runtimes", None)
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: ANN001
        if name == "modules.tts.engine":
            raise AssertionError("runtimes package import crossed into MLX engine")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    importlib.import_module("modules.tts.runtimes")


def test_normalized_registry_lookup_accepts_mixed_case_provider_engine() -> None:
    runtime_spec = TTSRuntimeSpec(
        provider="MLX",
        engine="kokoro",
        model="mlx-community/Kokoro-82M",
        quantization="bf16",
    )
    config = KokoroRuntimeConfig(
        runtime_spec=runtime_spec,
        model_name="mlx-community/Kokoro-82M",
        quantization="bf16",
        sample_rate=24000,
    )

    sentinel = object()
    dispatched: list[tuple[str, str]] = []

    def fake_builder(runtime_config: KokoroRuntimeConfig) -> object:
        dispatched.append(
            (
                runtime_config.runtime_spec.provider,
                runtime_config.runtime_spec.engine,
            )
        )
        return sentinel

    key = ("mlx", "kokoro")
    original = KokoroRuntimeFactory._registry[key]
    KokoroRuntimeFactory._registry[key] = fake_builder
    try:
        runtime = KokoroRuntimeFactory().create_runtime(config)
    finally:
        KokoroRuntimeFactory._registry[key] = original

    assert runtime is sentinel
    assert dispatched == [("MLX", "kokoro")]


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
        self.configs: list[KokoroRuntimeConfig] = []

    def create_runtime(self, config: KokoroRuntimeConfig) -> _FakeRuntime:
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
    runtime = SimpleNamespace(
        config=PipelineConfig(
            tts_engine="kokoro",
            tts_model_name=None,
            tts_quantization="bf16",
            tts_runtime=runtime_spec,
        )
    )

    kwargs = _tts_strategy_kwargs(runtime)
    strategy = _create_tts_strategy(runtime)

    assert "model_name" not in kwargs
    assert strategy.config.model_name == "mlx-community/Kokoro-82M"
    assert strategy.config.runtime_spec is runtime_spec


def test_pipeline_default_voice_threads_into_runtime_config() -> None:
    runtime_spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        provider="mlx",
    )
    runtime = SimpleNamespace(
        config=PipelineConfig(
            tts_engine="kokoro",
            tts_model_name=None,
            tts_quantization="bf16",
            tts_runtime=runtime_spec,
            voice="af_bella",
        )
    )

    kwargs = _tts_strategy_kwargs(runtime)
    strategy = _create_tts_strategy(runtime)
    runtime_config = KokoroRuntimeConfig.from_strategy_config(strategy.config)

    assert kwargs["default_voice"] == "af_bella"
    assert strategy.config.default_voice == "af_bella"
    assert runtime_config.default_voice == "af_bella"


def test_mlx_runtime_uses_default_voice_from_runtime_config() -> None:
    runtime_spec = TTSRuntimeSpec.from_legacy("kokoro", "bf16", provider="mlx")
    config = KokoroConfig(
        default_voice="af_bella",
        runtime_spec=runtime_spec,
    )

    with patch("modules.tts.engine.TTSEngine") as mock_engine:
        MLXKokoroRuntime(KokoroRuntimeConfig.from_strategy_config(config))

    engine_config = mock_engine.call_args.args[0]
    assert engine_config.default_voice == "af_bella"
