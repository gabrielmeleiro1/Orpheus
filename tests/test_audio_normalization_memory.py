"""Exact audio parity and peak allocation guards for RMS normalization."""

from __future__ import annotations

import tracemalloc
import wave

import numpy as np
import pytest

from modules.pipeline.stages.encoding import (
    _measure_pcm16_wav_rms,
    _normalize_audio_array,
)
from modules.pipeline.stages.synthesis import ChapterAudioAccumulator


def _reference_normalize(audio: np.ndarray, target_dbfs: float) -> np.ndarray:
    """Pre-optimization algorithm, including its float64 reduction order."""
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if samples.size == 0:
        return samples
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))
    if rms <= 0.0:
        return samples
    gain_db = target_dbfs - 20.0 * np.log10(rms)
    if abs(gain_db) < 1e-6:
        return samples
    gain = float(np.power(10.0, gain_db / 20.0))
    return np.clip(samples * gain, -1.0, 1.0).astype(np.float32, copy=False)


@pytest.mark.parametrize("target_dbfs", [-60.0, -16.0, 0.0, 6.0])
def test_normalization_matches_reference_bit_for_bit(target_dbfs: float) -> None:
    rng = np.random.default_rng(91827)
    for samples in (
        np.array([], dtype=np.float32),
        np.zeros(1024, dtype=np.float32),
        np.array([0.0, -0.0, 1.0, -1.0, 1e-40, -1e-40], dtype=np.float32),
        rng.uniform(-2.0, 2.0, 100_003).astype(np.float32),
        rng.uniform(-1.0, 1.0, 20_000).astype(np.float32)[::3],
        rng.uniform(-1.0, 1.0, (123, 27)),
    ):
        original = samples.tobytes()
        expected = _reference_normalize(samples, target_dbfs)
        actual = _normalize_audio_array(samples, target_dBFS=target_dbfs)
        assert actual.dtype == expected.dtype
        assert actual.shape == expected.shape
        assert actual.tobytes() == expected.tobytes()
        assert samples.tobytes() == original


def test_normalization_peak_allocation_is_below_three_float32_buffers() -> None:
    samples = np.linspace(-0.8, 0.8, 1_000_000, dtype=np.float32)
    tracemalloc.start()
    try:
        _normalize_audio_array(samples, target_dBFS=-16.0)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # One float64 RMS buffer, or float32 multiply+clip buffers, plus overhead.
    # Two simultaneous float64 buffers would require 4x the input size.
    assert peak_bytes < samples.nbytes * 3


def test_pcm16_rms_matches_reference_for_every_sample_value(tmp_path) -> None:
    samples = np.arange(-32768, 32768, dtype=np.int16)
    square_sum = float(np.sum(np.square(samples.astype(np.float64))))
    expected = float(np.sqrt(square_sum / samples.size) / 32768.0)
    path = tmp_path / "all-pcm16.wav"
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(samples.tobytes())
    assert _measure_pcm16_wav_rms(path) == expected
    accumulator = ChapterAudioAccumulator(
        output_path=tmp_path / "chapter.wav",
        sample_rate=24000,
        max_in_memory_samples=1,
    )
    try:
        accumulator.add_file(path)
        assert accumulator.pcm16_rms() == expected
        accumulator.finalize()
    finally:
        accumulator.close()
    assert (tmp_path / "chapter.wav").read_bytes() == path.read_bytes()
