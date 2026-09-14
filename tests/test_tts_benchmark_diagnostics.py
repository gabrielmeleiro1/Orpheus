"""Unit coverage for the non-inference TTS diagnostic helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from modules.tts.chunker import TextChunker
from modules.tts.engine_config import TTSConfig
from orpheus.bench.tts import HISTORICAL_TEXT, run_tts_diagnostic, validate_audio


EFFECTIVE_TEXT = TextChunker().chunk(HISTORICAL_TEXT)[0]


@pytest.fixture(autouse=True)
def fake_device_metadata(monkeypatch):
    # These are CPU-only contracts; hardware metadata is checked by the real CLI smoke.
    monkeypatch.setattr("orpheus.bench.tts._mlx_info", lambda: {"available": False})


class _FakeEngine:
    def __init__(self, audio) -> None:
        self.audio = audio
        self.calls = 0
        self.loaded = False
        self.unloaded = False
        self._model_path = "fake/Kokoro-82M-bf16"
        self.config = TTSConfig(model_name="fake/Kokoro-82M")

    def load_model(self) -> None:
        self.loaded = True

    def synthesize(self, text: str, *, voice: str, speed: float):
        assert text == EFFECTIVE_TEXT
        assert voice == "af_bella"
        assert speed == 1.0
        self.calls += 1
        return self.audio

    def unload_model(self, *, keep_in_cache: bool) -> None:
        assert keep_in_cache is False
        self.unloaded = True


def test_diagnostic_uses_injected_clock_and_reports_median_and_spread(tmp_path: Path) -> None:
    engine = _FakeEngine(np.ones(12, dtype=np.float32))
    ticks = iter((10.0, 12.0, 20.0, 24.0, 30.0, 35.0))

    report = run_tts_diagnostic(
        repetitions=3,
        warmups=1,
        engine_factory=lambda: engine,
        clock=lambda: next(ticks),
        repo_root=tmp_path,
    )

    assert engine.loaded is True
    assert engine.unloaded is True
    assert engine.calls == 4
    assert [row["elapsed_seconds"] for row in report["raw_timings"]] == [2.0, 4.0, 5.0]
    assert report["throughput"]["median_chars_per_second"] == pytest.approx(
        len(EFFECTIVE_TEXT) / 4.0
    )
    assert report["throughput"]["spread_chars_per_second"] == pytest.approx(
        len(EFFECTIVE_TEXT) / 2.0 - len(EFFECTIVE_TEXT) / 5.0
    )
    assert report["workload"]["raw_text_chars"] == len(HISTORICAL_TEXT)
    assert report["workload"]["text_chars"] == len(EFFECTIVE_TEXT) == 589
    assert report["workload"]["chunk_count"] == 1
    assert report["workload"]["text_sha256"] == hashlib.sha256(
        EFFECTIVE_TEXT.encode()
    ).hexdigest()
    assert report["audio"]["valid_nonempty_finite_runs"] == 3
    assert report["audio"]["valid_sample_count_total"] == 36


@pytest.mark.parametrize(
    "audio, expected_error",
    [
        (np.array([], dtype=np.float32), "empty"),
        (np.array([np.nan], dtype=np.float32), "non-finite"),
        (np.array([[0.0]], dtype=np.float32), "not mono"),
        (np.array([1.0 + 0.0j]), "complex"),
    ],
)
def test_invalid_audio_is_rejected_from_throughput(audio, expected_error) -> None:
    valid, sample_count, error = validate_audio(audio)
    assert valid is False
    assert sample_count == len(audio)
    assert expected_error in (error or "")

    engine = _FakeEngine(audio)
    ticks = iter(float(value) for value in range(6))
    report = run_tts_diagnostic(
        repetitions=3,
        warmups=1,
        engine_factory=lambda: engine,
        clock=lambda: next(ticks),
    )

    assert report["diagnostic_status"] == "invalid"
    assert report["throughput"]["median_chars_per_second"] is None
    assert report["throughput"]["valid_repetitions"] == 0
    assert report["audio"]["invalid_runs"] == 3


def test_failed_timing_does_not_publish_a_partial_median() -> None:
    engine = _FakeEngine(np.ones(12, dtype=np.float32))
    ticks = iter((1.0, 1.0, 2.0, 3.0, 4.0, 5.0))
    report = run_tts_diagnostic(
        repetitions=3, warmups=1, engine_factory=lambda: engine,
        clock=lambda: next(ticks),
    )
    assert report["diagnostic_status"] == "invalid"
    assert report["throughput"]["valid_repetitions"] == 2
    assert report["throughput"]["median_chars_per_second"] is None
    assert "finite and positive" in report["failures"][0]["error"]
    assert engine.unloaded


def test_synthesis_failure_is_retained_and_other_samples_remain_visible() -> None:
    engine = _FakeEngine(np.ones(12, dtype=np.float32))
    synthesize = engine.synthesize

    def fail_once(text: str, *, voice: str, speed: float):
        audio = synthesize(text, voice=voice, speed=speed)
        if engine.calls == 3:
            raise RuntimeError("inference failed")
        return audio

    engine.synthesize = fail_once
    ticks = iter(float(value) for value in range(6))
    report = run_tts_diagnostic(
        repetitions=3, warmups=1, engine_factory=lambda: engine,
        clock=lambda: next(ticks),
    )
    assert report["diagnostic_status"] == "invalid"
    assert report["throughput"]["valid_repetitions"] == 2
    assert report["throughput"]["median_chars_per_second"] is None
    assert len(report["raw_timings"]) == 3
    assert report["failures"] == [{"repetition": 2, "error": "synthesis failed: inference failed"}]
    assert engine.unloaded
