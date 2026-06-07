"""Thread-safety regression tests for shared VoiceService state."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from modules.app.config import AppConfig
from modules.app.services.voice_service import VoiceService
from modules.tts.runtime_spec import TTSRuntimeSpec


class _RuntimeValidationState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.create_calls = 0
        self.load_calls = 0
        self.unload_calls = 0
        self.unload_keep_in_cache: list[bool] = []
        self.synthesize_calls = 0
        self.first_synthesize_entered = threading.Event()
        self.release_first_synthesize = threading.Event()


class _RuntimeValidationStrategy:
    def __init__(self, state: _RuntimeValidationState) -> None:
        self._state = state
        self._loaded = False
        self.supported_voices = [SimpleNamespace(id="am_adam")]

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def sample_rate(self) -> int:
        return 24000

    def load(self) -> None:
        with self._state.lock:
            self._state.load_calls += 1
        self._loaded = True

    def unload(self, keep_in_cache: bool = True) -> None:
        with self._state.lock:
            self._state.unload_calls += 1
            self._state.unload_keep_in_cache.append(keep_in_cache)
        self._loaded = False

    def validate_voice(self, voice: str) -> bool:  # noqa: ARG002
        return True

    def synthesize(self, text: str, voice: str, speed: float = 1.0):  # noqa: ARG002
        with self._state.lock:
            self._state.synthesize_calls += 1
            call_number = self._state.synthesize_calls

        if call_number == 1:
            self._state.first_synthesize_entered.set()
            if not self._state.release_first_synthesize.wait(timeout=3.0):
                raise TimeoutError("First runtime validation synth timed out")

        return np.zeros(8, dtype=np.float32)


class _RuntimeValidationFactory:
    def __init__(self, state: _RuntimeValidationState) -> None:
        self._state = state

    def create(self, engine_name: str, quantization: str, **_kwargs):  # noqa: ARG002
        with self._state.lock:
            self._state.create_calls += 1
        return _RuntimeValidationStrategy(self._state)


class _RuntimeSpecValidationFactory(_RuntimeValidationFactory):
    def __init__(self, state: _RuntimeValidationState) -> None:
        super().__init__(state)
        self.runtime_specs: list[TTSRuntimeSpec] = []

    def create(
        self,
        engine_name: str,
        quantization: str,
        runtime_spec: TTSRuntimeSpec | None = None,
        **_kwargs,
    ):
        if runtime_spec is not None:
            self.runtime_specs.append(runtime_spec)
        return super().create(engine_name, quantization)


class _StrictTwoArgOnlyFactory:
    """Legacy factory that rejects identity kwargs — must not false-positive validate."""

    def __init__(self, state: _RuntimeValidationState) -> None:
        self._state = state

    def create(self, engine_name: str, quantization: str):  # noqa: ARG002
        with self._state.lock:
            self._state.create_calls += 1
        return _RuntimeValidationStrategy(self._state)


class _RuntimeSpecTypeErrorFactory(_RuntimeValidationFactory):
    def __init__(self, state: _RuntimeValidationState) -> None:
        super().__init__(state)
        self.runtime_specs: list[TTSRuntimeSpec | None] = []

    def create(
        self,
        engine_name: str,
        quantization: str,
        runtime_spec: TTSRuntimeSpec | None = None,
        **_kwargs,
    ):
        self.runtime_specs.append(runtime_spec)
        if runtime_spec is not None:
            raise TypeError("provider/device/model runtime mismatch")
        return super().create(engine_name, quantization)


class _PreviewState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.create_calls = 0
        self.primary_runtime_key: object | None = None
        self.first_preview_synthesize_entered = threading.Event()
        self.release_first_preview_synthesize = threading.Event()


class _PreviewStrategy:
    def __init__(self, key: object, state: _PreviewState) -> None:
        self._key = key
        self._state = state
        self._loaded = False
        self._synthesizing = False
        self.unload_calls = 0
        self.unload_during_synthesize = False
        self.supported_voices = [SimpleNamespace(id="am_adam")]

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def sample_rate(self) -> int:
        return 24000

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self.unload_calls += 1
        if self._synthesizing:
            self.unload_during_synthesize = True
        self._loaded = False

    def validate_voice(self, voice: str) -> bool:  # noqa: ARG002
        return True

    def synthesize(self, text: str, voice: str, speed: float = 1.0):  # noqa: ARG002
        self._synthesizing = True
        try:
            if self._key == self._state.primary_runtime_key:
                self._state.first_preview_synthesize_entered.set()
                if not self._state.release_first_preview_synthesize.wait(timeout=3.0):
                    raise TimeoutError("First preview synth timed out")
            return np.zeros(8, dtype=np.float32)
        finally:
            self._synthesizing = False


class _PreviewFactory:
    def __init__(self, state: _PreviewState) -> None:
        self._state = state
        self.engines: dict[object, _PreviewStrategy] = {}

    def create(self, engine_name: str, quantization: str, runtime_spec: TTSRuntimeSpec | None = None, **_kwargs):
        runtime_key = runtime_spec.cache_key() if runtime_spec is not None else (engine_name, quantization)
        strategy = _PreviewStrategy(runtime_key, self._state)
        with self._state.lock:
            self._state.create_calls += 1
            self.engines[runtime_key] = strategy
        return strategy


class _PreviewRuntimeSpecFactory(_PreviewFactory):
    def __init__(self, state: _PreviewState) -> None:
        super().__init__(state)
        self.runtime_keys: list[tuple[object, ...]] = []

    def create(
        self,
        engine_name: str,
        quantization: str,
        runtime_spec: TTSRuntimeSpec | None = None,
        **_kwargs,
    ):
        if runtime_spec is not None:
            self.runtime_keys.append(runtime_spec.cache_key())
        return super().create(engine_name, quantization)


def _build_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
    )


def test_validate_tts_runtime_deduplicates_concurrent_misses(tmp_path: Path) -> None:
    state = _RuntimeValidationState()
    service = VoiceService(_build_config(tmp_path), tts_factory=_RuntimeValidationFactory(state))

    errors: list[Exception] = []

    def _worker() -> None:
        try:
            service.validate_tts_runtime("kokoro", "bf16")
        except Exception as exc:  # pragma: no cover - explicit assertion below
            errors.append(exc)

    first = threading.Thread(target=_worker)
    second = threading.Thread(target=_worker)
    second_started = False

    try:
        first.start()
        assert state.first_synthesize_entered.wait(timeout=2.0)

        second.start()
        second_started = True
        time.sleep(0.1)
        # With proper locking, second worker should still be blocked before create/load.
        assert state.create_calls == 1
    finally:
        state.release_first_synthesize.set()
        first.join(timeout=2.0)
        if second_started:
            second.join(timeout=2.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not errors
    assert state.create_calls == 1
    assert state.load_calls == 1
    assert state.synthesize_calls == 1
    assert state.unload_calls == 1
    assert state.unload_keep_in_cache == [False]

    metrics = service.metrics_snapshot()
    assert metrics["runtime_validation_misses"] == 1
    assert metrics["runtime_validation_hits"] == 1
    assert service.runtime_validation_cache_size == 1


def test_validate_tts_runtime_unloads_one_off_strategy_without_cache(
    tmp_path: Path,
) -> None:
    state = _RuntimeValidationState()
    state.release_first_synthesize.set()
    service = VoiceService(_build_config(tmp_path), tts_factory=_RuntimeValidationFactory(state))

    service.validate_tts_runtime("kokoro", "bf16")

    assert state.unload_keep_in_cache == [False]


def test_validate_tts_runtime_reuses_loaded_preview_engine_without_unload(
    tmp_path: Path,
) -> None:
    state = _PreviewState()
    state.release_first_preview_synthesize.set()
    factory = _PreviewFactory(state)
    service = VoiceService(_build_config(tmp_path), tts_factory=factory)

    runtime_spec = TTSRuntimeSpec.from_legacy("kokoro", "bf16")
    preview_engine = service.ensure_preview_engine("kokoro", "bf16")
    service.validate_tts_runtime("kokoro", "bf16")

    assert factory.engines[runtime_spec.cache_key()] is preview_engine
    assert preview_engine.unload_calls == 0


def test_validate_tts_runtime_cache_distinguishes_runtime_spec_provider(
    tmp_path: Path,
) -> None:
    state = _RuntimeValidationState()
    state.release_first_synthesize.set()
    factory = _RuntimeSpecValidationFactory(state)
    service = VoiceService(_build_config(tmp_path), tts_factory=factory)
    mlx_spec = TTSRuntimeSpec.from_legacy("kokoro", "bf16", provider="mlx")
    mlx_device_spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        provider="mlx",
        device="gpu:0",
    )

    service.validate_tts_runtime("kokoro", "bf16", runtime_spec=mlx_spec)
    service.validate_tts_runtime("kokoro", "bf16", runtime_spec=mlx_device_spec)

    assert state.create_calls == 2
    assert service.runtime_validation_cache_size == 2
    assert factory.runtime_specs == [mlx_spec, mlx_device_spec]


def test_validate_tts_runtime_legacy_params_accept_provider_device_and_options(
    tmp_path: Path,
) -> None:
    state = _RuntimeValidationState()
    state.release_first_synthesize.set()
    factory = _RuntimeSpecValidationFactory(state)
    service = VoiceService(_build_config(tmp_path), tts_factory=factory)

    with pytest.raises(ValueError, match="Kokoro provider 'cuda' is not supported"):
        service.validate_tts_runtime(
            "kokoro",
            "bf16",
            tts_provider="cuda",
            tts_device="cuda:0",
            tts_options={"dtype": "fp16", "model_name": "Kokoro-82M"},
        )

    assert factory.runtime_specs == []


def test_create_tts_strategy_reraises_factory_type_error(
    tmp_path: Path,
) -> None:
    state = _RuntimeValidationState()
    factory = _RuntimeSpecTypeErrorFactory(state)
    service = VoiceService(_build_config(tmp_path), tts_factory=factory)
    spec = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model="Kokoro-82M",
        quantization="bf16",
        device="cuda:0",
        runtime_options=TTSRuntimeSpec.canonicalize_runtime_options({"dtype": "fp16"}),
    )

    with pytest.raises(TypeError, match="provider/device/model runtime mismatch"):
        service.ensure_preview_engine(runtime_spec=spec)

    assert factory.runtime_specs == [spec]
    assert state.create_calls == 0


def test_validate_tts_runtime_strict_two_arg_factory_does_not_false_positive_cache(
    tmp_path: Path,
) -> None:
    state = _RuntimeValidationState()
    factory = _StrictTwoArgOnlyFactory(state)
    service = VoiceService(_build_config(tmp_path), tts_factory=factory)
    spec = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model="Kokoro-82M",
        quantization="bf16",
        device="cuda:0",
        runtime_options=TTSRuntimeSpec.canonicalize_runtime_options({"dtype": "fp16"}),
    )

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        service.validate_tts_runtime("kokoro", "bf16", runtime_spec=spec)

    assert state.create_calls == 0
    assert service.runtime_validation_cache_size == 0
    metrics = service.metrics_snapshot()
    assert metrics["runtime_validation_misses"] == 1
    assert metrics["runtime_validation_hits"] == 0


def test_preview_engine_is_not_swapped_while_synthesizing(tmp_path: Path) -> None:
    state = _PreviewState()
    factory = _PreviewFactory(state)
    service = VoiceService(_build_config(tmp_path), tts_factory=factory)
    first_spec = TTSRuntimeSpec.from_legacy("kokoro", "bf16", provider="mlx")
    second_spec = TTSRuntimeSpec.from_legacy(
        "kokoro",
        "bf16",
        provider="mlx",
        device="mps",
    )
    state.primary_runtime_key = first_spec.cache_key()

    errors: list[Exception] = []
    preview_paths: list[Path] = []

    def _worker(runtime_spec: TTSRuntimeSpec) -> None:
        try:
            preview_path = service.generate_voice_preview(
                voice="am_adam",
                text="thread safety preview",
                runtime_spec=runtime_spec,
            )
            preview_paths.append(preview_path)
        except Exception as exc:  # pragma: no cover - explicit assertion below
            errors.append(exc)

    first = threading.Thread(target=_worker, args=(first_spec,))
    second = threading.Thread(target=_worker, args=(second_spec,))
    second_started = False

    try:
        first.start()
        assert state.first_preview_synthesize_entered.wait(timeout=2.0)

        second.start()
        second_started = True
        time.sleep(0.1)
        first_engine = factory.engines[first_spec.cache_key()]
        assert first_engine.unload_during_synthesize is False
    finally:
        state.release_first_preview_synthesize.set()
        first.join(timeout=2.0)
        if second_started:
            second.join(timeout=2.0)

    for preview_path in preview_paths:
        preview_path.unlink(missing_ok=True)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not errors

    first_engine = factory.engines[first_spec.cache_key()]
    assert first_engine.unload_calls == 1
    assert first_engine.unload_during_synthesize is False

    assert state.create_calls == 2
    assert len(preview_paths) == 2

    metrics = service.metrics_snapshot()
    assert metrics["preview_generation_count"] == 2


def test_preview_engine_cache_distinguishes_runtime_spec_options(tmp_path: Path) -> None:
    state = _PreviewState()
    state.release_first_preview_synthesize.set()
    factory = _PreviewRuntimeSpecFactory(state)
    service = VoiceService(_build_config(tmp_path), tts_factory=factory)
    first_spec = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model=None,
        quantization="bf16",
        device="cuda:0",
        runtime_options=TTSRuntimeSpec.canonicalize_runtime_options({"dtype": "fp16"}),
    )
    second_spec = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model=None,
        quantization="bf16",
        device="cuda:0",
        runtime_options=TTSRuntimeSpec.canonicalize_runtime_options({"dtype": "bf16"}),
    )

    first_path = service.generate_voice_preview(
        voice="am_adam",
        text="runtime spec preview",
        runtime_spec=first_spec,
    )
    second_path = service.generate_voice_preview(
        voice="am_adam",
        text="runtime spec preview",
        runtime_spec=second_spec,
    )

    first_path.unlink(missing_ok=True)
    second_path.unlink(missing_ok=True)
    assert state.create_calls == 2
    assert factory.runtime_keys == [first_spec.cache_key(), second_spec.cache_key()]


def test_preview_engine_cache_distinguishes_provider_model_and_device(
    tmp_path: Path,
) -> None:
    state = _PreviewState()
    state.release_first_preview_synthesize.set()
    factory = _PreviewRuntimeSpecFactory(state)
    service = VoiceService(_build_config(tmp_path), tts_factory=factory)
    runtime_specs = [
        TTSRuntimeSpec.from_legacy(
            "kokoro",
            "bf16",
            model="Kokoro-82M",
            provider="mlx",
        ),
        TTSRuntimeSpec(
            provider="cuda",
            engine="kokoro",
            model="Kokoro-82M",
            quantization="bf16",
            device="cuda:0",
        ),
        TTSRuntimeSpec(
            provider="cuda",
            engine="kokoro",
            model="Kokoro-82M",
            quantization="bf16",
            device="cuda:1",
        ),
        TTSRuntimeSpec(
            provider="cuda",
            engine="kokoro",
            model="Kokoro-82M-v2",
            quantization="bf16",
            device="cuda:1",
        ),
    ]

    preview_paths = [
        service.generate_voice_preview(
            voice="am_adam",
            text="runtime identity preview",
            runtime_spec=runtime_spec,
        )
        for runtime_spec in runtime_specs
    ]

    for preview_path in preview_paths:
        preview_path.unlink(missing_ok=True)
    assert state.create_calls == len(runtime_specs)
    assert factory.runtime_keys == [
        runtime_spec.cache_key() for runtime_spec in runtime_specs
    ]


def test_ensure_preview_engine_legacy_params_accept_provider_device_and_options(
    tmp_path: Path,
) -> None:
    state = _PreviewState()
    state.release_first_preview_synthesize.set()
    factory = _PreviewRuntimeSpecFactory(state)
    service = VoiceService(_build_config(tmp_path), tts_factory=factory)

    with pytest.raises(ValueError, match="Kokoro provider 'cuda' is not supported"):
        service.ensure_preview_engine(
            "kokoro",
            "bf16",
            tts_provider="cuda",
            tts_device="cuda:0",
            tts_options={"dtype": "fp16", "model_name": "Kokoro-82M"},
        )

    assert factory.runtime_keys == []


def test_voice_preview_closes_temp_file_handle_before_writing(monkeypatch, tmp_path: Path) -> None:
    state = _PreviewState()
    service = VoiceService(_build_config(tmp_path), tts_factory=_PreviewFactory(state))
    preview_path = tmp_path / "temp" / "preview.wav"
    closed = {"value": False}

    class _TempFile:
        name = str(preview_path)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            self.close()
            return False

        def close(self) -> None:
            closed["value"] = True

    def fake_named_temp_file(**kwargs):  # noqa: ANN001
        _ = kwargs
        return _TempFile()

    writes: list[Path] = []

    def fake_write(path, audio, sample_rate):  # noqa: ANN001
        assert closed["value"] is True
        _ = audio, sample_rate
        writes.append(Path(path))

    monkeypatch.setattr("modules.app.services.voice_service.tempfile.NamedTemporaryFile", fake_named_temp_file)
    monkeypatch.setattr("modules.app.services.voice_service.sf.write", fake_write)

    result = service.generate_voice_preview(
        voice="am_adam",
        text="preview text",
        engine_name="kokoro",
        tts_quantization="bf16",
    )

    assert result == preview_path
    assert writes == [preview_path]


def test_get_available_tts_engines_exposes_supported_conversion_engines(tmp_path: Path) -> None:
    class _Factory:
        @staticmethod
        def available_engines() -> list[str]:
            return ["kokoro"]

        @staticmethod
        def get_engine_info(engine_name: str) -> dict[str, object]:
            return {
                "id": engine_name,
                "display_name": engine_name.replace("_", " ").title(),
                "voices": [],
                "capabilities": {
                    "supports_batching": engine_name == "kokoro",
                    "provider": "mlx-audio",
                    "supports_conversion": True,
                    "worker_auto_mode": "sequential",
                    "process_safe": False,
                },
                "supported_quantizations": ["bf16"],
                "options_schema": {},
            }

    service = VoiceService(_build_config(tmp_path), tts_factory=_Factory())

    engines = service.get_available_tts_engines()

    assert [engine["id"] for engine in engines] == ["kokoro"]
    assert all(engine["available_for_conversion"] is True for engine in engines)
    assert engines[0]["capabilities"]["worker_auto_mode"] == "sequential"
    assert engines[0]["capabilities"]["process_safe"] is False
