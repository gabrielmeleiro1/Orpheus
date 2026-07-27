"""Service-level TTS engine session cache tests."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

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


def test_idle_unload_blocks_successor_attach_until_engine_quiesces(tmp_path) -> None:
    unload_started = threading.Event()
    release_unload = threading.Event()

    class _BlockingEngine:
        def unload(self, *, keep_in_cache: bool = True) -> None:
            assert keep_in_cache is False
            unload_started.set()
            assert release_unload.wait(timeout=3)

    config = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        keep_tts_engine_warm=True,
        tts_idle_timeout_seconds=300.0,
    )
    session = TtsEngineSession()
    retained = SimpleNamespace(
        config=config,
        _tts_engine=_BlockingEngine(),
        _selected_parallel_chapters=1,
        stages=SimpleNamespace(cancel_tts_idle_unload=lambda: None),
    )
    successor = SimpleNamespace(config=config, _tts_engine=None)
    session.adopt(retained)

    unload_thread = threading.Thread(target=session.cleanup, daemon=True)
    attach_thread = threading.Thread(target=lambda: session.attach(successor), daemon=True)
    unload_thread.start()
    assert unload_started.wait(timeout=2)

    attach_thread.start()
    attach_thread.join(timeout=0.1)
    assert attach_thread.is_alive()

    release_unload.set()
    unload_thread.join(timeout=2)
    attach_thread.join(timeout=2)
    assert not unload_thread.is_alive()
    assert not attach_thread.is_alive()
    assert successor._tts_engine is None


def test_cancelled_idle_timer_cannot_unload_successor_engine(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timers = []

    class _FakeTimer:
        def __init__(self, _timeout, callback):
            self.callback = callback
            self.cancelled = False
            timers.append(self)

        def start(self) -> None:
            return

        def cancel(self) -> None:
            self.cancelled = True

    class _Engine:
        def __init__(self) -> None:
            self.unloads = 0

        def unload(self, *, keep_in_cache: bool = True) -> None:
            assert keep_in_cache is False
            self.unloads += 1

    monkeypatch.setattr(
        "modules.app.services.tts_session.threading.Timer",
        _FakeTimer,
    )
    config = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        keep_tts_engine_warm=True,
        tts_idle_timeout_seconds=60.0,
    )
    session = TtsEngineSession()
    first_engine = _Engine()
    first = SimpleNamespace(
        config=config,
        _tts_engine=first_engine,
        _selected_parallel_chapters=1,
        stages=SimpleNamespace(cancel_tts_idle_unload=lambda: None),
    )
    session.adopt(first)
    stale_timer = timers[-1]

    attached = SimpleNamespace(config=config, _tts_engine=None)
    session.attach(attached)
    assert attached._tts_engine is first_engine
    successor_engine = _Engine()
    successor = SimpleNamespace(
        config=config,
        _tts_engine=successor_engine,
        _selected_parallel_chapters=1,
        stages=SimpleNamespace(cancel_tts_idle_unload=lambda: None),
    )
    session.adopt(successor)

    stale_timer.callback()

    assert stale_timer.cancelled is True
    assert successor_engine.unloads == 0
    session.cleanup()
