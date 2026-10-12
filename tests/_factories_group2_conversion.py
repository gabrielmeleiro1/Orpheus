"""Shared fixtures for conversion regressions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import shutil
import subprocess
import wave

import numpy as np
import pytest

from modules.pipeline.models import PipelineConfig
from modules.pipeline.orchestrator import ConversionPipeline


SOURCE = Path(__file__).parent / "fixtures" / "tiny_conversion.md"


TITLES = ["Zebra Crossing", "Amber Window"]


class ShortWavTTS:
    sample_rate = 24000
    supports_file_batch_results = True
    is_loaded = False

    def __init__(self, directory):
        self.directory = directory
        self.texts = []
        self.before_batch = None

    def load(self):
        self.is_loaded = True

    def warmup(self, **kwargs):
        pass

    def unload(self, **kwargs):
        self.is_loaded = False

    def synthesize(self, *, text, **kwargs):
        self.texts.append(text)
        samples = np.arange(self.sample_rate // 10)
        return (0.1 * np.sin(2 * np.pi * 440 * samples / self.sample_rate)).astype(np.float32)

    def synthesize_batch(self, *, items, progress_callback):
        if self.before_batch is not None:
            self.before_batch()
        results = []
        for index, item in enumerate(items):
            audio = self.synthesize(text=item.text)
            path = item.output_path or self.directory / f"chunk_{index}.wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), "wb") as writer:
                writer.setparams((1, 2, self.sample_rate, 0, "NONE", "not compressed"))
                writer.writeframes((audio * 32767).astype("<i2").tobytes())
            results.append(SimpleNamespace(index=index, audio=None, audio_path=path, error=None))
        return results


@pytest.fixture
def conversion(tmp_path, monkeypatch):
    assert shutil.which("ffmpeg"), "ffmpeg is required for artifact characterization"
    assert shutil.which("ffprobe"), "ffprobe is required for artifact characterization"
    engine = ShortWavTTS(tmp_path / "temp")
    monkeypatch.setattr("modules.pipeline.model_lifecycle.TTSEngineFactory.create", lambda *a, **k: engine)
    source = tmp_path / "tiny_conversion.md"
    shutil.copyfile(SOURCE, source)

    def make_pipeline(**options):
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            cleaner_backend="basic",
            enable_parallel=False,
            enable_pipelining=False,
            keep_tts_engine_warm=False,
            normalize_volume=False,
            enable_tts_file_batch_results=True,
            **options,
        )
        return ConversionPipeline(config)

    return source, engine, make_pipeline


def _probe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_chapters", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True, text=True, timeout=2,
    )
    return json.loads(result.stdout)
