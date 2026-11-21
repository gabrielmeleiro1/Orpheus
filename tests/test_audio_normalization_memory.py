"""Exact parity guard for the streamed PCM16 RMS used by normalization."""

from __future__ import annotations

import wave

import numpy as np

from modules.pipeline.stages.encoding import _measure_pcm16_wav_rms
from modules.pipeline.stages.synthesis import ChapterAudioAccumulator


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
