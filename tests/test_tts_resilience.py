"""
Test TTS synthesis resilience paths.
"""

from __future__ import annotations

import sys
import builtins
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import soundfile as sf

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import modules.tts.engine as tts_engine_module
from modules.tts.engine import TTSConfig, TTSEngine


def test_installed_voice_context_does_not_import_an_unavailable_mlx_runtime(monkeypatch):
    from modules.tts import mlx_compat

    monkeypatch.setattr(mlx_compat, "MLX_AVAILABLE", False)
    monkeypatch.setattr(mlx_compat, "installed_model_path", lambda _: "/installed/snapshot")
    original_import = builtins.__import__

    def import_without_mlx(name, *args, **kwargs):
        if name == "mlx_audio" or name.startswith("mlx_audio."):
            raise ModuleNotFoundError("No module named 'mlx_audio'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_mlx)
    with mlx_compat.use_installed_voices("mlx-community/Kokoro-82M-bf16"):
        pass


@pytest.fixture(autouse=True)
def _stub_lazy_mlx_runtime(monkeypatch, tmp_path, request):
    from huggingface_hub import constants
    from scripts.download_models import pinned_revision_for, write_download_receipt

    cache = tmp_path / "huggingface" / "hub"
    monkeypatch.setenv("HF_HOME", str(cache.parent))
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(cache))
    if getattr(request, "param", "repo") == "snapshot":
        pytest.importorskip("mlx_audio")
        model_id = "mlx-community/Kokoro-82M-bf16"
        revision = pinned_revision_for(model_id)
        snapshot = cache / "models--mlx-community--Kokoro-82M-bf16" / "snapshots" / revision
        (snapshot / "voices").mkdir(parents=True)
        (snapshot / "config.json").write_text("{}")
        (snapshot / "voices" / "af_bella.safetensors").write_bytes(b"voice")
        write_download_receipt(snapshot, model_id=model_id, revision=revision)

    else:
        monkeypatch.setattr(
            tts_engine_module.mlx_compat_backend, "installed_model_path", lambda _: str(tmp_path),
        )

    monkeypatch.setattr(tts_engine_module, "_ensure_mlx_runtime", lambda: None)
    monkeypatch.setattr(tts_engine_module.mlx_compat_backend, "MLX_AVAILABLE", False)
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
    model_path = Path(captured["model_path"])
    assert model_path.is_dir()
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


@pytest.mark.parametrize("_stub_lazy_mlx_runtime", ["repo", "snapshot"], indirect=True)
def test_tts_resilience() -> bool:
    print("\n" + "=" * 50)
    print("TTS RESILIENCE TEST SUITE")
    print("=" * 50 + "\n")

    _test_retry_when_first_generation_writes_no_audio_file()
    _test_prefers_model_path_keyword_for_generate_audio()
    _test_handles_generate_audio_without_output_path_parameter()

    print("\n" + "=" * 50)
    print("ALL TTS RESILIENCE TESTS PASSED ✓")
    print("=" * 50 + "\n")
    return True


if __name__ == "__main__":
    success = test_tts_resilience()
    sys.exit(0 if success else 1)
