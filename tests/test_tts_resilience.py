"""
Test TTS synthesis resilience paths.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import soundfile as sf

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import modules.tts.engine as tts_engine_module
from modules.tts.engine import TTSConfig, TTSEngine


@pytest.fixture(autouse=True)
def _stub_lazy_mlx_runtime(monkeypatch):
    monkeypatch.setattr(tts_engine_module, "_ensure_mlx_runtime", lambda: None)
    monkeypatch.setattr(tts_engine_module, "_ensure_tqdm_thread_lock", lambda: None)
    monkeypatch.setattr(
        tts_engine_module.mlx_compat_backend,
        "load_tts_model",
        lambda _model_path: object(),
    )


def _test_retry_when_first_generation_writes_no_audio_file() -> None:
    engine = TTSEngine(TTSConfig(default_voice="af_bella"))
    engine.load_model()

    calls = {"count": 0}

    def fake_generate_audio(*, output_path: str, file_prefix: str, audio_format: str, **kwargs):  # noqa: ANN003
        _ = kwargs
        calls["count"] += 1
        if calls["count"] == 1:
            return

        out = Path(output_path) / f"{file_prefix}_000.{audio_format}"
        sf.write(str(out), np.zeros(2400, dtype=np.float32), 24000, subtype="PCM_16")

    with patch("modules.tts.engine.generate_audio", side_effect=fake_generate_audio):
        audio = engine.synthesize("hello world", voice="af_bella", speed=1.0, max_retries=1)

    assert calls["count"] >= 2
    assert len(audio) > 0
    print("✓ retries synthesis when first generation yields no audio file")


def _test_collects_multiple_generated_wav_segments() -> None:
    engine = TTSEngine(TTSConfig(default_voice="af_bella"))
    engine.load_model()

    def fake_generate_audio(*, output_path: str, file_prefix: str, audio_format: str, **kwargs):  # noqa: ANN003
        _ = kwargs
        p0 = Path(output_path) / f"{file_prefix}_000.{audio_format}"
        p1 = Path(output_path) / f"{file_prefix}_001.{audio_format}"
        sf.write(str(p0), np.ones(1200, dtype=np.float32), 24000, subtype="PCM_16")
        sf.write(str(p1), np.ones(800, dtype=np.float32), 24000, subtype="PCM_16")

    with patch("modules.tts.engine.generate_audio", side_effect=fake_generate_audio):
        audio = engine.synthesize("hello world", voice="af_bella", speed=1.0, max_retries=1)

    # Expected concatenated length across all generated chunks.
    assert len(audio) == 2000
    print("✓ concatenates multiple generated wav segments")


def _test_prefers_model_path_keyword_for_generate_audio() -> None:
    engine = TTSEngine(TTSConfig(default_voice="af_bella"))
    engine.load_model()

    captured = {"model_path": None}

    def fake_generate_audio(*, model_path: str, output_path: str, file_prefix: str, audio_format: str, **kwargs):  # noqa: ANN003
        _ = kwargs
        captured["model_path"] = model_path
        out = Path(output_path) / f"{file_prefix}_000.{audio_format}"
        sf.write(str(out), np.zeros(1000, dtype=np.float32), 24000, subtype="PCM_16")

    with patch("modules.tts.engine.generate_audio", side_effect=fake_generate_audio):
        audio = engine.synthesize("hello world", voice="af_bella", speed=1.0, max_retries=1)

    assert captured["model_path"] is not None
    assert str(captured["model_path"]).endswith("-bf16")
    assert len(audio) == 1000
    print("✓ uses model_path keyword for mlx-audio generate call")


def _test_handles_generate_audio_without_output_path_parameter() -> None:
    engine = TTSEngine(TTSConfig(default_voice="af_bella"))
    engine.load_model()

    captured = {"file_prefix": None}

    def fake_generate_audio(*, model_path: str, file_prefix: str, audio_format: str, **kwargs):  # noqa: ANN003
        _ = kwargs
        _ = model_path
        captured["file_prefix"] = file_prefix
        out = Path(f"{file_prefix}_000.{audio_format}")
        out.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out), np.zeros(500, dtype=np.float32), 24000, subtype="PCM_16")

    with patch("modules.tts.engine.generate_audio", side_effect=fake_generate_audio):
        audio = engine.synthesize("hello world", voice="af_bella", speed=1.0, max_retries=1)

    assert captured["file_prefix"] is not None
    assert len(audio) == 500
    print("✓ supports mlx-audio APIs without output_path parameter")


def test_tts_resilience() -> bool:
    print("\n" + "=" * 50)
    print("TTS RESILIENCE TEST SUITE")
    print("=" * 50 + "\n")

    _test_retry_when_first_generation_writes_no_audio_file()
    _test_collects_multiple_generated_wav_segments()
    _test_prefers_model_path_keyword_for_generate_audio()
    _test_handles_generate_audio_without_output_path_parameter()

    print("\n" + "=" * 50)
    print("ALL TTS RESILIENCE TESTS PASSED ✓")
    print("=" * 50 + "\n")
    return True


if __name__ == "__main__":
    success = test_tts_resilience()
    sys.exit(0 if success else 1)
