"""Regression coverage for voice service."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from modules.app.config import AppConfig
from modules.app.services import voice_service
from modules.tts.runtime_spec import TTSRuntimeSpec


@pytest.mark.parametrize("partially_loaded", [False, True])
def test_preview_retry_uses_requested_runtime_after_failed_switch(monkeypatch, tmp_path, partially_loaded):
    original = TTSRuntimeSpec(provider="cpu", engine="kokoro", model=None, quantization="bf16")
    replacement = TTSRuntimeSpec(provider="cuda", engine="kokoro", model=None, quantization="bf16", device="cuda:0")
    engines = []
    synthesized = []

    def create(_engine, *, runtime_spec, **_kwargs):
        engine = Mock(is_loaded=False, sample_rate=24000)
        engine.runtime_spec = runtime_spec
        engine.validate_voice.return_value = True

        def load():
            engine.is_loaded = True
            if runtime_spec == replacement and engine.load.call_count == 1:
                engine.is_loaded = partially_loaded
                raise RuntimeError("replacement model failed to load")

        def unload(**_kwargs):
            engine.is_loaded = False

        def synthesize(*_args):
            synthesized.append(runtime_spec)
            return [0.0] * 8

        engine.load.side_effect = load
        engine.unload.side_effect = unload
        engine.synthesize.side_effect = synthesize
        engines.append(engine)
        return engine

    config = AppConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "out", temp_dir=tmp_path / "temp")
    service = voice_service.VoiceService(config, tts_factory=SimpleNamespace(create=create))
    monkeypatch.setattr(service, "_schedule_preview_idle_unload", lambda: None)
    monkeypatch.setattr(voice_service.sf, "write", lambda *_args: None)
    try:
        service.generate_voice_preview("am_adam", runtime_spec=original)
        with pytest.raises(RuntimeError, match="replacement model failed to load"):
            service.generate_voice_preview("am_adam", runtime_spec=replacement)
        service.generate_voice_preview("am_adam", runtime_spec=original)

        assert synthesized == [original, original]
        assert [engine.runtime_spec for engine in engines] == [original, replacement, original]
        engines[1].unload.assert_called_once_with(keep_in_cache=False)
    finally:
        service.cleanup()
