"""Regression coverage for the Kokoro TTS engines, runtimes and model cache."""

from __future__ import annotations

import builtins
import gc
import sys
import threading
import time
import wave
import weakref
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from modules.errors import SynthesisError, TTSInstallationError, TTSModelError, VRAMOverflowError
from modules.tts import engine as engine_module
from modules.tts import engine_output, mlx_compat, model_cache
from modules.tts.chunker import TextChunker
from modules.tts.device_memory import (
    MLXDeviceMemoryPolicy,
    _read_byte_count,
    apply_mlx_memory_limit_from_environment,
)
from modules.tts.factory import TTSEngineFactory
from modules.tts.runtime_spec import TTSRuntimeSpec
from modules.tts.runtimes.errors import TTSRuntimeMemoryError, TTSRuntimeUnavailable
from modules.tts.runtimes.kokoro_base import KokoroRuntimeConfig
from modules.tts.runtimes.kokoro_package import PackageKokoroRuntime
from modules.tts.strategies.kokoro import KokoroConfig, KokoroTTSStrategy


@pytest.fixture(autouse=True)
def _empty_model_cache():
    model_cache.clear_model_cache()
    yield
    model_cache.clear_model_cache()


# ---------------------------------------------------------------- espeak paths


def _frozen_package(tmp_path, monkeypatch, platform, library_name, get_data_path):
    package_dir = tmp_path / "_internal" / "espeakng_loader"
    data_dir = package_dir / "espeak-ng-data"
    data_dir.mkdir(parents=True)
    (data_dir / "phontab").write_bytes(b"phonemes")
    (package_dir / library_name).write_bytes(b"library")
    loader = SimpleNamespace(get_library_path=lambda: "compile-time", get_data_path=get_data_path)
    monkeypatch.setitem(sys.modules, "espeakng_loader", loader)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    return loader, package_dir / library_name, data_dir


@pytest.mark.parametrize(
    ("platform", "library_name"),
    [("win32", "espeak-ng.dll"), ("linux", "libespeak-ng.so"), ("darwin", "libespeak-ng.dylib")],
)
def test_frozen_speech_loader_uses_the_platform_library_name(tmp_path, monkeypatch, platform, library_name):
    loader, library, data = _frozen_package(
        tmp_path, monkeypatch, platform, library_name, lambda: str(tmp_path / "absent"),
    )

    mlx_compat.patch_espeakng_loader_paths()

    assert loader.get_library_path() == str(library)
    assert loader.get_data_path() == str(data)


def test_frozen_speech_loader_replaces_upstream_missing_data_error(tmp_path, monkeypatch):
    def upstream_get_data_path():
        raise RuntimeError("data path not exists at /compile/time/espeak-ng-data")

    upstream_get_data_path.__module__ = "espeakng_loader"
    loader, library, data = _frozen_package(
        tmp_path, monkeypatch, "linux", "libespeak-ng.so", upstream_get_data_path,
    )

    mlx_compat.patch_espeakng_loader_paths()

    assert loader.get_library_path() == str(library)
    assert loader.get_data_path() == str(data)


def test_frozen_speech_loader_keeps_the_launcher_fail_closed_getter(tmp_path, monkeypatch):
    def unavailable():
        raise RuntimeError("Speech initialization is unavailable")

    loader, _library, _data = _frozen_package(
        tmp_path, monkeypatch, "win32", "espeak-ng.dll", unavailable,
    )

    mlx_compat.patch_espeakng_loader_paths()

    assert loader.get_data_path is unavailable


def test_source_windows_worker_replaces_short_non_ascii_data_path(tmp_path, monkeypatch):
    packaged = tmp_path / "café" / "espeak-ng-data"
    packaged.mkdir(parents=True)
    configured = tmp_path / "short"
    configured.mkdir()
    (configured / "phontab").write_bytes(b"phonemes")
    loader = SimpleNamespace(get_library_path=lambda: "lib", get_data_path=lambda: str(packaged))
    monkeypatch.setitem(sys.modules, "espeakng_loader", loader)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("ESPEAK_DATA_PATH", raising=False)
    monkeypatch.setenv("PHONEMIZER_ESPEAK_DATA_PATH", str(configured))

    mlx_compat.patch_espeakng_loader_paths()

    assert Path(loader.get_data_path()) == configured.resolve()


# ------------------------------------------------------------- MLX synthesis


class _FakeMLXModel:
    """Stands in for the loaded mlx-audio Kokoro model."""

    def __init__(self, segments=None, error: BaseException | None = None) -> None:
        self.segments = segments if segments is not None else [[0.5, -0.25], [1.5, -2.0, 0.1]]
        self.error = error
        self.calls: list[dict] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        for samples in self.segments:
            yield SimpleNamespace(audio=np.asarray(samples, dtype=np.float32), sample_rate=24000)


def _mlx_engine(monkeypatch, model):
    monkeypatch.setattr(engine_module, "_ensure_mlx_runtime", lambda: None)
    monkeypatch.setattr(engine_module, "_ensure_tqdm_thread_lock", lambda: None)
    monkeypatch.setattr(engine_module, "generate_audio", None)
    monkeypatch.setattr(mlx_compat, "generate_audio", None)
    monkeypatch.setattr(mlx_compat, "installed_voice_snapshot", lambda path: (path, "snapshot"))
    monkeypatch.setattr(mlx_compat, "load_tts_model", lambda path: model)
    tts = engine_module.TTSEngine()
    tts.load_model()
    return tts


def _expected_pcm(segments) -> np.ndarray:
    joined = np.concatenate([np.asarray(s, dtype=np.float32) for s in segments])
    return (np.clip(joined, -1.0, 1.0) * 32767).astype(np.int16)


def test_mlx_synthesis_calls_the_loaded_model_and_matches_the_wav_round_trip(monkeypatch, tmp_path):
    model = _FakeMLXModel()
    tts = _mlx_engine(monkeypatch, model)
    expected = _expected_pcm(model.segments)

    audio = tts.synthesize("Hello. Bonjour.", voice="bf_emma")
    file_path = tts.synthesize_to_output_file("Hello.", tmp_path / "chunk.wav", voice="af_bella")
    path_audio = tts.synthesize("Hello.", voice="af_bella", output_path=tmp_path / "samples.wav")

    np.testing.assert_array_equal(audio, expected.astype(np.float32) / 32768)
    np.testing.assert_array_equal(path_audio, audio)
    for written in (file_path, tmp_path / "samples.wav"):
        with wave.open(str(written), "rb") as reader:
            assert reader.getparams()[:3] == (1, 2, 24000)
            frames = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
        np.testing.assert_array_equal(frames, expected)
    assert [call["lang_code"] for call in model.calls] == ["b", "a", "a"]
    assert not list(tmp_path.glob("*_0*.wav"))


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TTSInstallationError("Voice af_x is missing from the installed snapshot"), TTSInstallationError),
        (RuntimeError("[metal] Insufficient Memory while running the model"), VRAMOverflowError),
        (ValueError("phoneme lookup failed"), SynthesisError),
    ],
)
def test_mlx_synthesis_failures_reach_error_classification(monkeypatch, tmp_path, error, expected):
    tts = _mlx_engine(monkeypatch, _FakeMLXModel(error=error))

    with pytest.raises(expected):
        tts.synthesize("Hello.", voice="am_adam", max_retries=1)
    with pytest.raises(expected):
        tts.synthesize_to_output_file("Hello.", tmp_path / "chunk.wav", voice="am_adam", max_retries=1)


def test_mlx_warmup_surfaces_model_failures(monkeypatch):
    tts = _mlx_engine(monkeypatch, _FakeMLXModel(error=RuntimeError("out of memory")))

    with pytest.raises(RuntimeError, match="out of memory"):
        tts.warmup_model()
    assert tts._warmup_complete is False


def test_mlx_synthesis_without_segments_fails_fast(monkeypatch):
    tts = _mlx_engine(monkeypatch, _FakeMLXModel(segments=[]))

    started = time.perf_counter()
    with pytest.raises(SynthesisError, match="No audio"):
        tts.synthesize("Hello.", voice="am_adam", max_retries=1)
    assert time.perf_counter() - started < 0.5


def test_injected_generate_function_keeps_the_wrapper_path(monkeypatch, tmp_path):
    model = _FakeMLXModel()
    tts = _mlx_engine(monkeypatch, model)
    monkeypatch.setattr(mlx_compat, "installed_model_path", lambda path: str(tmp_path))
    calls = []

    def custom_generate(**kwargs):
        calls.append(kwargs)
        with wave.open(f"{kwargs['file_prefix']}.wav", "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(24000)
            writer.writeframes(np.array([7, 8], dtype=np.int16).tobytes())

    audio = engine_output.synthesize_to_temp_audio(tts, "Hi.", "am_adam", 1.0, generate_fn=custom_generate)

    assert len(calls) == 1 and model.calls == []
    np.testing.assert_array_equal(audio, np.array([7, 8], dtype=np.float32) / 32768)


@pytest.mark.parametrize(
    "message",
    [
        "failed to allocate memory while loading model weights",
        "CUDA out of memory while loading checkpoint",
    ],
)
def test_memory_failures_mentioning_the_model_are_vram_errors(monkeypatch, message):
    tts = _mlx_engine(monkeypatch, _FakeMLXModel())

    def fail():
        raise RuntimeError(message)

    with pytest.raises(VRAMOverflowError):
        tts._synthesize_with_retries(fail, max_retries=1)


def test_model_load_failures_without_memory_words_stay_model_errors(monkeypatch):
    tts = _mlx_engine(monkeypatch, _FakeMLXModel())

    def fail():
        raise RuntimeError("weight shape mismatch for model")

    with pytest.raises(TTSModelError):
        tts._synthesize_with_retries(fail, max_retries=1)


# ---------------------------------------------------------------- model cache


class _Model:
    """Weak-referenceable model stand-in."""

    def generate(self, **kwargs):
        yield SimpleNamespace(audio=np.zeros(4, dtype=np.float32), sample_rate=24000)


def _recording_accelerator(monkeypatch, model_ref):
    observed: list[bool] = []

    def clear_cache():
        observed.append(model_ref() is None)

    fake_mx = SimpleNamespace(clear_cache=clear_cache)
    monkeypatch.setitem(sys.modules, "mlx.core", fake_mx)
    monkeypatch.setattr(mlx_compat, "mx", fake_mx)
    monkeypatch.setattr(mlx_compat, "MLX_AVAILABLE", True)
    return observed


def test_mlx_engines_with_one_key_share_a_single_loaded_model(monkeypatch):
    loads = []

    def load(path):
        time.sleep(0.05)
        loads.append(path)
        return _Model()

    monkeypatch.setattr(engine_module, "_ensure_mlx_runtime", lambda: None)
    monkeypatch.setattr(engine_module, "_ensure_tqdm_thread_lock", lambda: None)
    monkeypatch.setattr(mlx_compat, "installed_voice_snapshot", lambda path: (path, "snapshot"))
    monkeypatch.setattr(mlx_compat, "load_tts_model", load)
    engines = [engine_module.TTSEngine() for _ in range(3)]

    threads = [threading.Thread(target=tts.load_model) for tts in engines]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(loads) == 1
    assert engines[0]._model is engines[1]._model is engines[2]._model


def test_mlx_hard_unload_releases_memory_after_dropping_every_reference(monkeypatch):
    monkeypatch.setattr(engine_module, "_ensure_mlx_runtime", lambda: None)
    monkeypatch.setattr(engine_module, "_ensure_tqdm_thread_lock", lambda: None)
    monkeypatch.setattr(mlx_compat, "installed_voice_snapshot", lambda path: (path, "snapshot"))
    monkeypatch.setattr(mlx_compat, "load_tts_model", lambda path: _Model())
    tts = engine_module.TTSEngine()
    tts.load_model()
    observed = _recording_accelerator(monkeypatch, weakref.ref(tts._model))

    tts.unload_model(keep_in_cache=False)

    assert observed and observed[-1] is True
    assert model_cache.get_cached_model(tts._model_cache_key) is None


def test_mlx_hard_unload_keeps_a_model_another_engine_still_uses(monkeypatch):
    monkeypatch.setattr(engine_module, "_ensure_mlx_runtime", lambda: None)
    monkeypatch.setattr(engine_module, "_ensure_tqdm_thread_lock", lambda: None)
    monkeypatch.setattr(mlx_compat, "installed_voice_snapshot", lambda path: (path, "snapshot"))
    monkeypatch.setattr(mlx_compat, "load_tts_model", lambda path: _Model())
    first, second = engine_module.TTSEngine(), engine_module.TTSEngine()
    first.load_model()
    second.load_model()
    observed = _recording_accelerator(monkeypatch, weakref.ref(first._model))

    first.unload_model(keep_in_cache=False)

    assert observed == [False]
    assert second.synthesize("Still speaking.", voice="am_adam").size == 4


def test_cache_eviction_releases_device_memory(monkeypatch):
    evicted = _Model()
    observed = _recording_accelerator(monkeypatch, weakref.ref(evicted))
    model_cache.set_cached_model("a", evicted)
    model_cache.set_cached_model("b", object())
    del evicted

    model_cache.set_cached_model("c", object())

    assert observed == [True]


def test_release_device_memory_leaves_uninitialized_cuda_alone(monkeypatch):
    emptied = []
    cuda = SimpleNamespace(is_initialized=lambda: False, empty_cache=lambda: emptied.append(1))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=cuda))
    model_cache.release_device_memory()
    assert emptied == []

    cuda.is_initialized = lambda: True
    model_cache.release_device_memory()
    assert emptied == [1]


# ------------------------------------------------------------ package runtime


class _FakeKModel:
    instances: list["_FakeKModel"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeKModel.instances.append(self)

    def to(self, device):
        return self

    def eval(self):
        return self


class _FakeKPipeline:
    def __init__(self, lang_code, model, **kwargs):
        self.lang_code = lang_code
        self.model = model

    def load_single_voice(self, path):
        return path

    def __call__(self, text, *, voice, speed):
        yield text, self.lang_code, np.ones(3, dtype=np.float32)


@pytest.fixture
def package_kokoro(monkeypatch, tmp_path):
    (tmp_path / "voices").mkdir()
    for name in ("af_bella", "bf_emma"):
        (tmp_path / "voices" / f"{name}.pt").write_bytes(b"voice")
    module = sys.modules[PackageKokoroRuntime.__module__]
    monkeypatch.setattr(module, "installed_model_path", lambda name: str(tmp_path))
    monkeypatch.setattr(module, "require_local_phonemizer", lambda: None)
    monkeypatch.setattr("modules.hf_cache.configure_hf_cache", lambda: None)
    _FakeKModel.instances = []
    events: list[str] = []
    monkeypatch.setattr(mlx_compat, "patch_espeakng_loader_paths", lambda: events.append("patched"))
    fake_kokoro = SimpleNamespace(KModel=_FakeKModel, KPipeline=_FakeKPipeline)
    original_import = builtins.__import__

    def tracking_import(name, *args, **kwargs):
        if name == "kokoro":
            events.append("kokoro")
            return fake_kokoro
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", tracking_import)
    return events


def _runtime(provider="cpu", voice="af_bella") -> PackageKokoroRuntime:
    spec = TTSRuntimeSpec.from_legacy("kokoro", "bf16", provider=provider)
    return PackageKokoroRuntime(KokoroRuntimeConfig(
        runtime_spec=spec, model_name=spec.resolved_model(), quantization="bf16",
        sample_rate=24000, default_voice=voice,
    ))


def test_package_load_patches_speech_paths_before_importing_kokoro(package_kokoro):
    _runtime().load()

    assert package_kokoro[:2] == ["patched", "kokoro"]


def test_package_runtimes_with_different_languages_share_the_model(package_kokoro):
    american, british = _runtime(voice="af_bella"), _runtime(voice="bf_emma")

    american.load()
    british.load()

    assert len(_FakeKModel.instances) == 1
    assert american._cache_key() == british._cache_key()
    assert american._pipeline.lang_code == "a"
    assert british._pipeline.lang_code == "b"
    assert british._pipeline.model is american._pipeline.model
    # Each runtime phonemizes with its voice's language.
    assert american.synthesize("Hi.", "bf_emma").size == 3
    assert american._pipeline_for_voice("bf_emma") is british._pipeline


def test_package_hard_unload_releases_memory_after_dropping_references(package_kokoro, monkeypatch):
    runtime = _runtime()
    runtime.load()
    model_ref = weakref.ref(_FakeKModel.instances[0])
    _FakeKModel.instances = []
    observed: list[bool] = []
    cuda = SimpleNamespace(is_initialized=lambda: True, empty_cache=lambda: observed.append(model_ref() is None))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=cuda))

    runtime.unload(keep_in_cache=False)
    gc.collect()

    assert observed and observed[-1] is True
    assert model_ref() is None


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (MemoryError(), TTSRuntimeMemoryError),
        (RuntimeError("CUDA out of memory. Tried to allocate 20.00 MiB"), TTSRuntimeMemoryError),
        (RuntimeError("CUBLAS_STATUS_ALLOC_FAILED when calling cublasCreate(handle)"), TTSRuntimeMemoryError),
        (RuntimeError("CUDA error: cudaErrorMemoryAllocation"), TTSRuntimeMemoryError),
        (RuntimeError("std::bad_alloc"), TTSRuntimeMemoryError),
        (RuntimeError("weights are corrupt"), TTSRuntimeUnavailable),
        (RuntimeError("CUDA driver not found"), TTSRuntimeUnavailable),
        (RuntimeError("Found no NVIDIA driver on your system"), TTSRuntimeUnavailable),
    ],
)
def test_package_load_failures_keep_memory_errors_distinct(package_kokoro, monkeypatch, error, expected):
    def failing_model(self, **kwargs):
        raise error

    monkeypatch.setattr(_FakeKModel, "__init__", failing_model)

    with pytest.raises(expected):
        _runtime(provider="cuda").load()


class _OutOfMemoryError(RuntimeError):
    pass


class _OOMPipeline:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def load_single_voice(self, path):
        return path

    def __call__(self, text, *, voice, speed):
        self.calls += 1
        if self.calls <= self.failures:
            raise _OutOfMemoryError("CUDA out of memory")
        yield text, "", np.ones(5, dtype=np.float32)


@pytest.fixture
def fake_cuda(monkeypatch, package_kokoro):
    emptied: list[int] = []
    cuda = SimpleNamespace(
        OutOfMemoryError=_OutOfMemoryError,
        is_initialized=lambda: True,
        empty_cache=lambda: emptied.append(1),
    )
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=cuda))
    return emptied


def test_package_synthesis_retries_one_cuda_oom(fake_cuda):
    runtime = _runtime(provider="cuda")
    runtime._pipeline = _OOMPipeline(failures=1)

    assert runtime.synthesize("Retry me.", "af_bella").size == 5
    assert fake_cuda == [1]


def test_package_synthesis_reports_a_repeated_cuda_oom_as_memory_error(fake_cuda):
    runtime = _runtime(provider="cuda")
    runtime._pipeline = _OOMPipeline(failures=2)

    with pytest.raises(TTSRuntimeMemoryError, match="CUDA out of memory"):
        runtime.synthesize("Still no memory.", "af_bella")
    assert runtime._pipeline.calls == 2
    assert fake_cuda == [1]


class _ScriptedPipeline:
    def __init__(self, outputs) -> None:
        self.outputs = outputs

    def load_single_voice(self, path):
        return path

    def __call__(self, text, *, voice, speed):
        for audio in self.outputs:
            yield text, "", audio


class _CpuTensor:
    def __init__(self, values) -> None:
        self.values = np.asarray(values, dtype=np.float32)

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.values


def test_package_synthesis_skips_missing_segments_and_converts_tensors(package_kokoro):
    runtime = _runtime()
    runtime._pipeline = _ScriptedPipeline([None, _CpuTensor([1.0, 2.0]), np.array([3.0], dtype=np.float32)])

    np.testing.assert_array_equal(runtime.synthesize("Hi.", "af_bella"), [1.0, 2.0, 3.0])


def test_package_synthesis_without_audio_is_an_error_not_an_empty_file(package_kokoro, tmp_path):
    runtime = _runtime()
    runtime._pipeline = _ScriptedPipeline([])
    output_path = tmp_path / "chunk.wav"

    with pytest.raises(SynthesisError, match="No audio"):
        runtime.synthesize("Hi.", "af_bella", output_path=output_path)
    assert not output_path.exists()


# ------------------------------------------------------------- device memory


def test_mlx_worker_memory_budget_is_applied_through_mlx(monkeypatch):
    env = MLXDeviceMemoryPolicy().configure_worker_environment(
        available_bytes=12 * 1024**3, active_workers=3,
    )
    limits: list[int] = []
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    applied = apply_mlx_memory_limit_from_environment(SimpleNamespace(set_memory_limit=limits.append))

    assert applied == int(4 * 1024**3 * 0.9)
    assert limits == [applied]


@pytest.mark.parametrize("value", ["", "0", "not-a-number"])
def test_mlx_memory_budget_ignores_unusable_values(monkeypatch, value):
    limits: list[int] = []
    monkeypatch.setenv("MLX_METAL_MEMORY", value)

    assert apply_mlx_memory_limit_from_environment(SimpleNamespace(set_memory_limit=limits.append)) is None
    assert limits == []


def _stub_mlx_runtime_imports(monkeypatch):
    """Stand in for the MLX packages so the initializer runs without loading them.

    Importing the native runtime inside the test process is not repeatable once
    another test has swapped its entries in ``sys.modules``.
    """
    mx_module = SimpleNamespace()
    nn_module = SimpleNamespace(Module=type("Module", (), {}))
    generate = SimpleNamespace(generate_audio=lambda **_kwargs: None)
    base = SimpleNamespace(GenerationResult=type("GenerationResult", (), {}))
    models = SimpleNamespace(base=base)
    tts = SimpleNamespace(generate=generate, models=models)
    modules = {
        "mlx": SimpleNamespace(core=mx_module, nn=nn_module),
        "mlx.core": mx_module,
        "mlx.nn": nn_module,
        "mlx_audio": SimpleNamespace(tts=tts),
        "mlx_audio.tts": tts,
        "mlx_audio.tts.generate": generate,
        "mlx_audio.tts.models": models,
        "mlx_audio.tts.models.base": base,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(mlx_compat, "patch_espeakng_loader_paths", lambda: None)
    monkeypatch.setattr(mlx_compat, "patch_mlx_load_weights_compat", lambda _module: None)
    monkeypatch.setattr(mlx_compat, "patch_mlx_generation_result_compat", lambda _result: None)
    monkeypatch.setattr(mlx_compat, "_MLX_RUNTIME_CHECKED", False)
    monkeypatch.setattr(mlx_compat, "MLX_AVAILABLE", False)
    monkeypatch.setattr(mlx_compat, "MLX_IMPORT_ERROR", None)
    monkeypatch.setattr(mlx_compat, "mx", None)
    monkeypatch.setattr(mlx_compat, "generate_audio", None)
    return mx_module


def test_mlx_runtime_import_applies_the_worker_budget(monkeypatch):
    applied: list[object] = []
    monkeypatch.setattr(
        "modules.tts.device_memory.apply_mlx_memory_limit_from_environment", applied.append,
    )
    _stub_mlx_runtime_imports(monkeypatch)

    mx_module, _generate = mlx_compat.ensure_mlx_runtime()

    assert applied == [mx_module]


def test_mlx_runtime_initializes_once_under_concurrent_callers(monkeypatch):
    _stub_mlx_runtime_imports(monkeypatch)
    patched: list[int] = []

    def slow_patch():
        patched.append(1)
        time.sleep(0.05)

    monkeypatch.setattr(mlx_compat, "patch_espeakng_loader_paths", slow_patch)
    results: list[object] = []
    threads = [
        threading.Thread(target=lambda: results.append(mlx_compat.ensure_mlx_runtime()))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert patched == [1]
    assert len(results) == 4 and all(result[0] is not None for result in results)


def test_byte_count_falls_back_to_the_next_probe():
    def broken():
        raise AttributeError("deprecated")

    source = SimpleNamespace(get_active_memory=broken, active_memory=lambda: 42)

    assert _read_byte_count(source, "get_active_memory", "active_memory") == 42


def test_mlx_policy_clear_cache_tolerates_runtime_failures():
    def broken():
        raise RuntimeError("metal device lost")

    MLXDeviceMemoryPolicy(mlx_core=SimpleNamespace(clear_cache=broken)).clear_cache()


# --------------------------------------------------------- runtime identity


def test_serialized_auto_device_means_default_device(monkeypatch):
    spec = TTSRuntimeSpec.from_dict(
        {"provider": "cuda", "engine": "kokoro", "quantization": "bf16", "device": "AUTO"},
    )

    assert spec.device is None


def test_cuda_probe_runs_once_per_visible_device_set(monkeypatch):
    launches: list[int] = []

    class _Tensor:
        def add_(self, value):
            return self

        def item(self):
            return 2

    def ones(*args, **kwargs):
        launches.append(1)
        return _Tensor()

    monkeypatch.setitem(
        sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True), ones=ones),
    )
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr("modules.tts.runtime_spec._cuda_probe_runs_in_process", lambda: True)
    TTSRuntimeSpec.reset_cuda_probe_cache()
    try:
        assert all(TTSRuntimeSpec.cuda_available_on_current_platform() for _ in range(3))
        assert launches == [1]
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")
        assert TTSRuntimeSpec.cuda_available_on_current_platform()
        assert launches == [1, 1]
        TTSRuntimeSpec.reset_cuda_probe_cache()
        assert TTSRuntimeSpec.cuda_available_on_current_platform()
        assert launches == [1, 1, 1]
    finally:
        TTSRuntimeSpec.reset_cuda_probe_cache()


@pytest.mark.parametrize(("cuda", "provider"), [(True, "cuda"), (False, "cpu")])
def test_unloaded_kokoro_reports_the_host_provider(monkeypatch, cuda, provider):
    monkeypatch.setattr(TTSRuntimeSpec, "mlx_supported_on_current_platform", staticmethod(lambda: False))
    monkeypatch.setattr(TTSRuntimeSpec, "cuda_available_on_current_platform", staticmethod(lambda: cuda))
    strategy = KokoroTTSStrategy(KokoroConfig())

    assert strategy.capabilities.provider == provider
    assert strategy.supports_file_batch_results is False
    assert TTSEngineFactory._static_kokoro_info()["capabilities"]["provider"] == provider


def test_unloaded_kokoro_on_apple_silicon_still_reports_mlx(monkeypatch):
    monkeypatch.setattr(TTSRuntimeSpec, "mlx_supported_on_current_platform", staticmethod(lambda: True))
    strategy = KokoroTTSStrategy(KokoroConfig())

    assert strategy.capabilities.provider == "mlx"
    assert strategy.supports_file_batch_results is True


# ------------------------------------------------------------------- chunker


def test_chunker_splits_sentences_ending_inside_typographic_quotes():
    chunker = TextChunker()
    text = "“Wait here.” She waited. ‘Why?’ He left."

    boundaries = chunker._find_sentence_boundaries(text)

    assert [text[: b + 1].rstrip() for b in boundaries] == [
        "“Wait here.”",
        "“Wait here.” She waited.",
        "“Wait here.” She waited. ‘Why?’",
        text,
    ]


def test_chunker_prefers_typographic_sentence_breaks_over_word_splits():
    sentence = "“The lantern swung slowly in the wind,” she said. "
    chunks = TextChunker().chunk(sentence * 12, max_tokens=40)

    assert all(chunk.endswith("said.") for chunk in chunks)



class _ExplodingCudaTorch:
    """A torch stand-in whose CUDA use in this process fails the test."""

    class cuda:  # noqa: N801 - mirrors torch.cuda
        @staticmethod
        def is_initialized():
            return False

        @staticmethod
        def is_available():
            raise AssertionError("the backend process must not touch CUDA")

    @staticmethod
    def ones(*args, **kwargs):
        raise AssertionError("the backend process must not create a CUDA context")


def _backend_process(monkeypatch):
    from modules.tts import runtime_spec

    monkeypatch.setattr(runtime_spec.multiprocessing, "parent_process", lambda: None)
    monkeypatch.setattr(runtime_spec.sys, "argv", ["orpheus-backend"])
    monkeypatch.setattr(runtime_spec.sys, "frozen", False, raising=False)
    TTSRuntimeSpec.reset_cuda_probe_cache()
    return runtime_spec


def _fake_probe_run(calls, *, stdout='{"available": true}\n', returncode=0, error=None):
    import os
    import subprocess

    def run(argv, **kwargs):
        calls.append((list(argv), os.environ.get("CUDA_VISIBLE_DEVICES"), kwargs.get("timeout")))
        if error is not None:
            raise error
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="boom")

    return run


def test_backend_process_asks_a_child_process_about_cuda_once_per_device_set(monkeypatch):
    runtime_spec = _backend_process(monkeypatch)
    calls: list = []
    monkeypatch.setitem(sys.modules, "torch", _ExplodingCudaTorch)
    monkeypatch.setattr(runtime_spec.subprocess, "run", _fake_probe_run(calls))
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    try:
        assert all(TTSRuntimeSpec.cuda_available_on_current_platform() for _ in range(3))
        assert len(calls) == 1
        argv, visible, timeout = calls[0]
        assert argv[:2] == [sys.executable, "-c"]
        assert "torch.ones(1, device=\"cuda\").add_(1).item() == 2" in argv[2]
        assert visible is None and timeout and timeout > 0

        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")
        assert TTSRuntimeSpec.cuda_available_on_current_platform()
        assert [call[1] for call in calls] == [None, "1"]

        monkeypatch.setattr(TTSRuntimeSpec, "mlx_supported_on_current_platform", staticmethod(lambda: False))
        assert TTSRuntimeSpec.default_provider("kokoro") == "cuda"
        assert len(calls) == 2
    finally:
        TTSRuntimeSpec.reset_cuda_probe_cache()


@pytest.mark.parametrize(
    "probe",
    [
        {"stdout": '{"available": false}\n'},
        {"stdout": "", "returncode": 3},
        {"stdout": "Traceback: not json\n", "returncode": 1},
        {"stdout": '{"available": "yes"}\n'},
        {"error": __import__("subprocess").TimeoutExpired(["python"], 120)},
        {"error": OSError("cannot start interpreter")},
    ],
)
def test_backend_cuda_probe_failures_mean_cpu_and_are_remembered(monkeypatch, probe):
    runtime_spec = _backend_process(monkeypatch)
    calls: list = []
    monkeypatch.setitem(sys.modules, "torch", _ExplodingCudaTorch)
    monkeypatch.setattr(runtime_spec.subprocess, "run", _fake_probe_run(calls, **probe))
    try:
        assert TTSRuntimeSpec.cuda_available_on_current_platform() is False
        assert TTSRuntimeSpec.cuda_available_on_current_platform() is False
        assert len(calls) == 1
    finally:
        TTSRuntimeSpec.reset_cuda_probe_cache()


def test_backend_cuda_probe_does_not_import_torch_or_spawn_when_torch_is_absent(monkeypatch):
    runtime_spec = _backend_process(monkeypatch)
    calls: list = []
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(runtime_spec.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(runtime_spec.subprocess, "run", _fake_probe_run(calls))
    try:
        assert TTSRuntimeSpec.cuda_available_on_current_platform() is False
        assert calls == []
        assert "torch" not in sys.modules
    finally:
        TTSRuntimeSpec.reset_cuda_probe_cache()


def test_backend_cuda_probe_leaves_an_installed_torch_unimported(monkeypatch):
    runtime_spec = _backend_process(monkeypatch)
    calls: list = []
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(runtime_spec.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(runtime_spec.subprocess, "run", _fake_probe_run(calls))
    try:
        assert TTSRuntimeSpec.cuda_available_on_current_platform() is True
        assert len(calls) == 1
        assert "torch" not in sys.modules
    finally:
        TTSRuntimeSpec.reset_cuda_probe_cache()


def test_frozen_backend_probes_cuda_in_a_spawned_process(monkeypatch):
    runtime_spec = _backend_process(monkeypatch)
    timeouts: list[float] = []
    monkeypatch.setattr(runtime_spec.sys, "frozen", True, raising=False)
    monkeypatch.setitem(sys.modules, "torch", _ExplodingCudaTorch)
    monkeypatch.setattr(runtime_spec.subprocess, "run", _fake_probe_run([], error=AssertionError("no -c")))
    monkeypatch.setattr(
        runtime_spec, "_probe_cuda_in_spawned_process", lambda timeout: timeouts.append(timeout) or True
    )
    try:
        assert TTSRuntimeSpec.cuda_available_on_current_platform() is True
        assert TTSRuntimeSpec.cuda_available_on_current_platform() is True
        assert len(timeouts) == 1
    finally:
        TTSRuntimeSpec.reset_cuda_probe_cache()


def test_spawned_cuda_probe_child_answers_through_the_pipe(monkeypatch):
    from modules.tts import runtime_spec

    sent: list[object] = []
    closed: list[bool] = []
    connection = SimpleNamespace(send=sent.append, close=lambda: closed.append(True))

    class _Tensor:
        def add_(self, value):
            return self

        def item(self):
            return 2

    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True), ones=lambda *a, **k: _Tensor()),
    )
    runtime_spec._cuda_probe_child(connection)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    runtime_spec._cuda_probe_child(connection)

    assert sent == [True, False]
    assert closed == [True, True]


def test_spawned_cuda_probe_reports_false_without_torch():
    from modules.tts import runtime_spec

    # torch is not installed in this environment, so the real spawned child
    # exercises the import-failure path end to end.
    if runtime_spec._torch_installed():
        pytest.skip("torch is installed; the child would launch a real kernel")
    assert runtime_spec._probe_cuda_in_spawned_process(60.0) is False


@pytest.mark.parametrize("process", ["worker", "python -c", "cuda already initialized"])
def test_processes_already_committed_to_cuda_probe_in_process(monkeypatch, process):
    runtime_spec = _backend_process(monkeypatch)
    launches: list[int] = []

    class _Tensor:
        def add_(self, value):
            return self

        def item(self):
            return 2

    def ones(*args, **kwargs):
        launches.append(1)
        return _Tensor()

    cuda = SimpleNamespace(is_available=lambda: True, is_initialized=lambda: process == "cuda already initialized")
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=cuda, ones=ones))
    if process == "worker":
        monkeypatch.setattr(runtime_spec.multiprocessing, "parent_process", lambda: object())
    elif process == "python -c":
        monkeypatch.setattr(runtime_spec.sys, "argv", ["-c"])
    monkeypatch.setattr(
        runtime_spec.subprocess, "run", _fake_probe_run([], error=AssertionError("no child probe"))
    )
    try:
        assert TTSRuntimeSpec.cuda_available_on_current_platform() is True
        assert TTSRuntimeSpec.cuda_available_on_current_platform() is True
        assert launches == [1]
    finally:
        TTSRuntimeSpec.reset_cuda_probe_cache()


def test_cuda_probe_script_matches_the_in_process_probe(monkeypatch, capsys):
    from modules.tts import runtime_spec

    for available, item, expected in [(True, 2, True), (True, 3, False), (False, 2, False)]:
        tensor = SimpleNamespace(add_=lambda value, _item=item: SimpleNamespace(item=lambda: _item))
        fake = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda _a=available: _a), ones=lambda *a, **k: tensor)
        monkeypatch.setitem(sys.modules, "torch", fake)
        exec(runtime_spec._CUDA_PROBE_SCRIPT, {"__name__": "__probe__"})  # noqa: S102
        assert capsys.readouterr().out.strip() == f'{{"available": {str(expected).lower()}}}'
        assert runtime_spec._launch_cuda_probe_kernel(fake) is expected


@pytest.mark.parametrize(
    ("provider", "message", "memory"),
    [
        ("cuda", "CUBLAS_STATUS_ALLOC_FAILED when calling cublasCreate(handle)", True),
        ("cuda", "CUDA error: cudaErrorMemoryAllocation", True),
        ("cuda", "std::bad_alloc", True),
        ("cuda", "CUDA driver not found", False),
        ("cuda", "index out of range in self", False),
        ("cpu", "std::bad_alloc", False),
    ],
)
def test_package_synthesis_recognises_cuda_allocation_failures_by_message(
    fake_cuda, provider, message, memory
):
    assert _runtime(provider=provider)._is_cuda_oom(RuntimeError(message)) is memory
