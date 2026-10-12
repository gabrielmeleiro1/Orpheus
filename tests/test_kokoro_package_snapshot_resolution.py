"""The package Kokoro runtime verifies its model snapshot once per load, not per chunk."""

from __future__ import annotations

import sys

import numpy as np
import pytest

from modules.errors import TTSInstallationError
from modules.tts.runtime_spec import TTSRuntimeSpec
from modules.tts.runtimes.kokoro_base import KokoroRuntimeConfig
from modules.tts.runtimes.kokoro_package import PackageKokoroRuntime


class _FakePipeline:
    def __init__(self) -> None:
        self.loaded_voices: list[str] = []

    def load_single_voice(self, path: str) -> str:
        self.loaded_voices.append(path)
        return path

    def __call__(self, text, *, voice, speed):
        yield text, "", np.zeros(240, dtype=np.float32)


def _runtime(monkeypatch, tmp_path):
    (tmp_path / "voices").mkdir()
    (tmp_path / "voices" / "am_adam.pt").write_bytes(b"voice")
    resolutions: list[str] = []

    def installed_model_path(model_name: str) -> str:
        resolutions.append(model_name)
        return str(tmp_path)

    monkeypatch.setattr(
        sys.modules[PackageKokoroRuntime.__module__], "installed_model_path", installed_model_path,
    )
    spec = TTSRuntimeSpec.from_legacy("kokoro", "bf16", provider="cpu")
    runtime = PackageKokoroRuntime(KokoroRuntimeConfig(
        runtime_spec=spec, model_name=spec.resolved_model(), quantization="bf16", sample_rate=24000,
    ))
    runtime._pipeline = _FakePipeline()
    return runtime, resolutions


def test_snapshot_is_resolved_once_across_chunks(monkeypatch, tmp_path):
    runtime, resolutions = _runtime(monkeypatch, tmp_path)

    for _ in range(25):
        audio = runtime.synthesize("A short chunk.", "am_adam")
        assert audio.size == 240

    assert len(resolutions) == 1


def test_unload_drops_the_resolved_snapshot(monkeypatch, tmp_path):
    runtime, resolutions = _runtime(monkeypatch, tmp_path)
    runtime.synthesize("Before unload.", "am_adam")
    runtime.unload(keep_in_cache=True)
    runtime._pipeline = _FakePipeline()
    runtime.synthesize("After reload.", "am_adam")

    assert len(resolutions) == 2


def test_missing_voice_is_still_reported_per_chunk(monkeypatch, tmp_path):
    runtime, _ = _runtime(monkeypatch, tmp_path)
    runtime.synthesize("Warm.", "am_adam")
    (tmp_path / "voices" / "am_adam.pt").unlink()

    with pytest.raises(TTSInstallationError, match="am_adam"):
        runtime.synthesize("Voice removed.", "am_adam")
