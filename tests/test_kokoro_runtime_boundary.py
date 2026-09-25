"""Kokoro provider-runtime boundary tests."""

from __future__ import annotations

import builtins
import importlib
import sys
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

from modules.pipeline.model_lifecycle import _create_tts_strategy, _tts_strategy_kwargs
from modules.pipeline.models import PipelineConfig
from modules.tts.runtime_spec import TTSRuntimeSpec
from modules.tts.runtimes import KokoroRuntimeConfig, KokoroRuntimeFactory
from modules.tts.runtimes.kokoro_package import PackageKokoroRuntime
from modules.tts.runtimes.kokoro_mlx import MLXKokoroRuntime
from modules.tts.strategies.base import SynthesisItem, SynthesisResult
from modules.tts.strategies.kokoro import KokoroConfig, KokoroTTSStrategy

@pytest.fixture(autouse=True)
def _isolate_installed_models(monkeypatch):
    monkeypatch.setattr(
        importlib.import_module("modules.tts.runtimes.kokoro_package"),
        "installed_model_path", lambda name: name,
    )

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


def test_runtime_boundary_registry_registers_package_kokoro_providers() -> None:
    assert ("cpu", "kokoro") in KokoroRuntimeFactory.registered_keys()
    assert ("cuda", "kokoro") in KokoroRuntimeFactory.registered_keys()


@pytest.mark.parametrize("provider", ["cpu", "cuda"])
@pytest.mark.parametrize("use_repository", [False, True])
def test_package_kokoro_runtime_loads_local_model_dir_without_repo_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
    use_repository: bool,
) -> None:
    model_dir = tmp_path / "kokoro"
    voices_dir = model_dir / "voices"
    voices_dir.mkdir(parents=True)
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "kokoro-v1_0.pth").write_bytes(b"model")
    (voices_dir / "am_adam.pt").write_bytes(b"voice")

    calls: dict[str, Any] = {}

    class FakeKModel:
        def __init__(self, **kwargs: object) -> None:
            calls["model"] = kwargs

        def to(self, device):
            calls["device"] = device
            return self

        def eval(self):
            calls["eval"] = True
            return self

    class FakeKPipeline:
        def __init__(self, **kwargs: object) -> None:
            calls["pipeline"] = kwargs

        def __call__(self, text: str, *, voice: str, speed: float):
            calls["synthesis"] = {"text": text, "voice": voice, "speed": speed}
            yield text, "phonemes", np.ones(2, dtype=np.float32)

    fake_kokoro = SimpleNamespace(KModel=FakeKModel, KPipeline=FakeKPipeline)
    monkeypatch.setitem(sys.modules, "kokoro", fake_kokoro)
    model_name = "hexgrad/Kokoro-82M" if use_repository else str(model_dir)
    monkeypatch.setattr(
        importlib.import_module("modules.tts.runtimes.kokoro_package"),
        "installed_model_path", lambda name: str(model_dir),
    )
    from modules.tts import model_cache
    model_cache.clear_model_cache()

    runtime = PackageKokoroRuntime(
        KokoroRuntimeConfig(
            runtime_spec=TTSRuntimeSpec(
                provider=provider,
                engine="kokoro",
                model=model_name,
                quantization="bf16",
            ),
            model_name=str(model_dir),
            quantization="bf16",
            sample_rate=24000,
        )
    )

    runtime.load()
    audio = runtime.synthesize("hello", "am_adam")

    assert audio.shape == (2,)
    assert calls["model"] == {
        "repo_id": "hexgrad/Kokoro-82M",
        "config": str(model_dir / "config.json"),
        "model": str(model_dir / "kokoro-v1_0.pth"),
    }
    assert "repo_id" not in calls["pipeline"]
    assert calls["device"] == provider
    assert calls["pipeline"]["device"] == provider
    assert calls["eval"] is True
    assert calls["synthesis"]["voice"] == str(voices_dir / "am_adam.pt")
    model_cache.clear_model_cache()


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


def test_unsupported_cuda_runtime_provider_builds_spec_until_runtime_load() -> None:
    spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        provider="cuda",
        device="cuda:0",
    )

    assert spec.provider == "cuda"
    assert spec.device == "cuda:0"


def test_package_kokoro_runtime_delegates_to_kpipeline(monkeypatch, tmp_path: Path) -> None:
    from modules.tts.runtimes.kokoro_package import PackageKokoroRuntime

    class _FakePipeline:
        instances: list["_FakePipeline"] = []

        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.calls: list[dict[str, Any]] = []
            _FakePipeline.instances.append(self)

        def __call__(self, text: str, **kwargs: Any):  # noqa: ANN202
            self.calls.append({"text": text, **kwargs})
            yield "gs", "ps", np.array([0.25, -0.25], dtype=np.float32)

    fake_kokoro = SimpleNamespace(KPipeline=_FakePipeline)
    fake_soundfile = SimpleNamespace(write=lambda path, audio, rate: Path(path).write_text(f"{rate}:{audio.tolist()}", encoding="utf-8"))
    monkeypatch.setitem(sys.modules, "kokoro", fake_kokoro)
    monkeypatch.setitem(sys.modules, "soundfile", fake_soundfile)

    strategy = KokoroTTSStrategy(
        KokoroConfig(
            runtime_spec=TTSRuntimeSpec(
                provider="cuda",
                engine="kokoro",
                model="hexgrad/Kokoro-82M",
                quantization="bf16",
                device="cuda:0",
            )
        )
    )

    output_path = tmp_path / "audio.wav"
    runtime = PackageKokoroRuntime(
        KokoroRuntimeConfig.from_strategy_config(strategy.config)
    )
    runtime.load()
    audio = runtime.synthesize("Hello", voice="am_adam", speed=1.25, output_path=output_path)

    assert audio.tolist() == [0.25, -0.25]
    assert output_path.read_text(encoding="utf-8") == "24000:[0.25, -0.25]"
    assert _FakePipeline.instances[0].kwargs["repo_id"] == "hexgrad/Kokoro-82M"
    assert _FakePipeline.instances[0].kwargs["device"] == "cuda:0"
    assert _FakePipeline.instances[0].calls == [
        {"text": "Hello", "voice": "am_adam", "speed": 1.25}
    ]


def test_package_kokoro_runtime_writes_silence_for_blank_text(tmp_path: Path) -> None:
    runtime = _package_runtime()
    runtime._pipeline = object()  # noqa: SLF001 - bypass model loading for the edge case
    output_path = tmp_path / "blank.wav"

    audio = runtime.synthesize(" \t", voice="am_adam", output_path=output_path)

    assert audio.size > 0
    assert np.all(audio == 0)
    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getnframes() == audio.size
        assert wav_file.getframerate() == runtime.sample_rate


def test_package_kokoro_runtime_warns_when_retry_drops_repo_and_device(
    caplog,
    monkeypatch,
) -> None:
    from modules.tts import model_cache
    from modules.tts.runtimes.kokoro_package import PackageKokoroRuntime

    model_cache.clear_model_cache()

    class _FakePipeline:
        calls: list[dict[str, Any]] = []

        def __init__(self, **kwargs: Any) -> None:
            if "repo_id" in kwargs or "device" in kwargs:
                raise TypeError("unexpected kwarg")
            self.calls.append(kwargs)

    monkeypatch.setitem(sys.modules, "kokoro", SimpleNamespace(KPipeline=_FakePipeline))
    strategy = KokoroTTSStrategy(
        KokoroConfig(
            runtime_spec=TTSRuntimeSpec(
                provider="cuda",
                engine="kokoro",
                model="hexgrad/Kokoro-82M",
                quantization="bf16",
                device="cuda:0",
            )
        )
    )
    runtime = PackageKokoroRuntime(
        KokoroRuntimeConfig.from_strategy_config(strategy.config)
    )

    with caplog.at_level("WARNING", logger="modules.tts.runtimes.kokoro_package"):
        runtime.load()

    assert _FakePipeline.calls == [{"lang_code": "a"}]
    assert "repo_id, device" in caplog.text
    model_cache.clear_model_cache()


def test_package_kokoro_runtime_writes_output_without_soundfile(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import builtins
    import wave

    from modules.tts.runtimes.kokoro_package import PackageKokoroRuntime

    class _FakePipeline:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def __call__(self, _text: str, **_kwargs: Any):  # noqa: ANN202
            yield "gs", "ps", np.array([0.25, -0.25], dtype=np.float32)

    fake_kokoro = SimpleNamespace(KPipeline=_FakePipeline)
    monkeypatch.setitem(sys.modules, "kokoro", fake_kokoro)
    monkeypatch.delitem(sys.modules, "soundfile", raising=False)
    original_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any):  # noqa: ANN401
        if name == "soundfile":
            raise ModuleNotFoundError("No module named 'soundfile'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    strategy = KokoroTTSStrategy(
        KokoroConfig(
            runtime_spec=TTSRuntimeSpec(
                provider="cuda",
                engine="kokoro",
                model="hexgrad/Kokoro-82M",
                quantization="bf16",
                device="cuda:0",
            )
        )
    )

    output_path = tmp_path / "audio.wav"
    runtime = PackageKokoroRuntime(
        KokoroRuntimeConfig.from_strategy_config(strategy.config)
    )
    runtime.load()
    audio = runtime.synthesize("Hello", voice="am_adam", speed=1.0, output_path=output_path)

    assert audio.tolist() == [0.25, -0.25]
    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 24000
        assert wav_file.getnframes() == 2


@pytest.mark.parametrize("strategy_runtime_options", [None, {}])
def test_runtime_config_inherits_options_from_runtime_spec_when_strategy_options_absent(
    strategy_runtime_options,
) -> None:
    runtime_spec = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model="Kokoro-82M",
        quantization="bf16",
        device="cuda:0",
        runtime_options=TTSRuntimeSpec.canonicalize_runtime_options({"dtype": "fp16"}),
    )

    runtime_config = KokoroRuntimeConfig.from_strategy_config(
        KokoroConfig(runtime_spec=runtime_spec, runtime_options=strategy_runtime_options)
    )

    assert runtime_config.runtime_options == {"dtype": "fp16"}


@pytest.mark.parametrize("provider", ["cpu", "cuda"])
def test_kokoro_default_config_resolves_package_model_for_package_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    monkeypatch.setattr(
        TTSRuntimeSpec,
        "default_provider",
        classmethod(lambda cls, engine: provider),
    )

    runtime_config = KokoroRuntimeConfig.from_strategy_config(KokoroConfig())

    assert runtime_config.runtime_spec.provider == provider
    assert runtime_config.runtime_spec.model is None
    assert runtime_config.runtime_spec.resolved_model() == "hexgrad/Kokoro-82M"
    assert runtime_config.model_name == "hexgrad/Kokoro-82M"


def test_kokoro_explicit_model_override_is_preserved_for_cpu_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        TTSRuntimeSpec,
        "default_provider",
        classmethod(lambda cls, engine: "cpu"),
    )

    runtime_config = KokoroRuntimeConfig.from_strategy_config(
        KokoroConfig(model_name="custom/kokoro")
    )

    assert runtime_config.runtime_spec.provider == "cpu"
    assert runtime_config.runtime_spec.model == "custom/kokoro"
    assert runtime_config.runtime_spec.resolved_model() == "custom/kokoro"


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


def _package_runtime() -> PackageKokoroRuntime:
    return PackageKokoroRuntime(
        KokoroRuntimeConfig(
            runtime_spec=TTSRuntimeSpec(
                provider="cpu",
                engine="kokoro",
                model="hexgrad/Kokoro-82M",
                quantization="fp32",
            ),
            model_name="hexgrad/Kokoro-82M",
            quantization="fp32",
            sample_rate=24000,
        )
    )


def test_package_runtime_hard_unload_preserves_newer_same_key_cache_entry() -> None:
    from modules.tts import model_cache

    model_cache.clear_model_cache()
    runtime = _package_runtime()
    retired_pipeline = object()
    replacement_pipeline = object()
    runtime._pipeline = retired_pipeline  # noqa: SLF001
    model_cache.set_cached_model(runtime._cache_key(), replacement_pipeline)  # noqa: SLF001

    try:
        runtime.unload(keep_in_cache=False)
        assert model_cache.get_cached_model(runtime._cache_key()) is replacement_pipeline  # noqa: SLF001
    finally:
        model_cache.clear_model_cache()


def test_package_runtime_detached_hard_unload_releases_owned_cache_reference() -> None:
    from modules.tts import model_cache

    model_cache.clear_model_cache()
    runtime = _package_runtime()
    retired_pipeline = object()
    replacement_pipeline = object()
    runtime._pipeline = retired_pipeline  # noqa: SLF001
    model_cache.set_cached_model(runtime._cache_key(), retired_pipeline)  # noqa: SLF001
    runtime.unload(keep_in_cache=True)
    model_cache.set_cached_model(runtime._cache_key(), replacement_pipeline)  # noqa: SLF001

    try:
        runtime.unload(keep_in_cache=False)
        assert runtime._last_cached_pipeline is None  # noqa: SLF001
        assert model_cache.get_cached_model(runtime._cache_key()) is replacement_pipeline  # noqa: SLF001
    finally:
        model_cache.clear_model_cache()


def test_package_kokoro_serializes_calls_into_a_shared_pipeline() -> None:
    """Two runtimes sharing one cached KPipeline must not call it concurrently.

    model_cache hands the same pipeline object to every holder, and KPipeline
    mutates per-call state, so an unsynchronized voice preview issued during a
    conversion corrupts the in-flight chunk.
    """
    import threading

    in_flight = 0
    max_in_flight = 0
    counter_lock = threading.Lock()

    class _StatefulPipeline:
        def __call__(self, text, voice, speed):  # noqa: ANN001, ANN204
            nonlocal in_flight, max_in_flight
            with counter_lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            try:
                time.sleep(0.02)
                yield ("g", "p", np.zeros(4, dtype=np.float32))
            finally:
                with counter_lock:
                    in_flight -= 1

    shared_pipeline = _StatefulPipeline()
    first = _package_runtime()
    second = _package_runtime()
    assert first._cache_key() == second._cache_key()
    first._pipeline = shared_pipeline
    second._pipeline = shared_pipeline

    errors: list[BaseException] = []

    def run(runtime: PackageKokoroRuntime) -> None:
        try:
            runtime.synthesize("hello there", voice="af_bella")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(first,)),
        threading.Thread(target=run, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert max_in_flight == 1, "shared KPipeline was entered concurrently"
