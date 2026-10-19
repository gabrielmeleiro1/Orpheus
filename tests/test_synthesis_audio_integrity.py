"""Audio integrity at chapter accumulation, batch validation, and finalization."""

from __future__ import annotations

import errno
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from modules.pipeline.stages import synthesis


class FailingCloseWriter:
    def __init__(self, error=None):
        self.close_calls = 0
        self.error = error or OSError(errno.ENOSPC, "synthetic disk full on final flush")

    def close(self):
        self.close_calls += 1
        raise self.error


class RecordingWriter:
    def __init__(self):
        self.writes = []

    def writeframes(self, payload):
        self.writes.append(payload)

    def close(self):
        pass


def accumulator(tmp_path, limit=1):
    return synthesis.ChapterAudioAccumulator(
        output_path=tmp_path / "synthetic.wav",
        sample_rate=24000,
        max_in_memory_samples=limit,
    )


def runtime():
    return SimpleNamespace(log_verbose=lambda *_args: None)


def batch_result(audio):
    return SimpleNamespace(audio=audio, audio_path=None, index=0, error=None)


def test_streamed_finalize_propagates_final_flush_failure(tmp_path):
    assembled = accumulator(tmp_path)
    writer = FailingCloseWriter()
    assembled._writer = writer
    with pytest.raises(OSError) as raised:
        assembled.finalize()
    assert raised.value.errno == errno.ENOSPC
    assert writer.close_calls == 1


def test_pipeline_finalize_does_not_retry_failed_stream_or_replace_its_file(tmp_path, monkeypatch):
    assembled = accumulator(tmp_path)
    sentinel = b"existing partial WAV must not be replaced by an empty retry"
    assembled.output_path.write_bytes(sentinel)
    writer = FailingCloseWriter()
    assembled._writer = writer

    def forbid_reopen(*_args):
        pytest.fail("A streamed flush error must not fall back to reopening/truncating the file")

    monkeypatch.setattr(synthesis, "_open_chapter_wav_writer", forbid_reopen)
    with pytest.raises(OSError) as raised:
        synthesis._finalize_chapter_audio(runtime(), assembled)
    assert raised.value.errno == errno.ENOSPC
    assert assembled.output_path.read_bytes() == sentinel
    assert writer.close_calls == 1


def test_stream_close_memory_error_does_not_trigger_concatenation_fallback(tmp_path, monkeypatch):
    assembled = accumulator(tmp_path)
    writer = FailingCloseWriter(MemoryError("synthetic writer allocation failure"))
    assembled._writer = writer

    def forbid_reopen(*_args):
        pytest.fail("A streamed close failure is not an in-memory concatenation failure")

    monkeypatch.setattr(synthesis, "_open_chapter_wav_writer", forbid_reopen)
    with pytest.raises(MemoryError, match="writer allocation failure"):
        synthesis._finalize_chapter_audio(runtime(), assembled)
    assert writer.close_calls == 1


def test_cleanup_is_best_effort_and_does_not_retry_a_failed_close(tmp_path):
    assembled = accumulator(tmp_path)
    writer = FailingCloseWriter()
    assembled._writer = writer
    assembled.close()
    assembled.close()
    assert writer.close_calls == 1


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
def test_list_batch_validation_rejects_nonfinite_body_samples(invalid):
    result = batch_result(np.array([0.1, invalid, 0.2], dtype=np.float32))
    assert not synthesis._validate_complete_batch_results(
        runtime(), batch_results=[result], expected_count=1,
    )


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
def test_incremental_validation_rejects_nonfinite_body_samples(invalid):
    result = batch_result(np.array([0.1, invalid, 0.2], dtype=np.float32))
    assert not synthesis._validate_incremental_batch_result(
        runtime(), batch_result=result, position=0, expected_count=1, seen_indexes=set(),
    )


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
def test_pcm_writer_rejects_nonfinite_audio_before_writing(invalid):
    writer = RecordingWriter()
    with pytest.raises(ValueError):
        synthesis._write_audio_segment(writer, np.array([0.1, invalid], dtype=np.float32))
    assert writer.writes == []


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
def test_accumulator_rejects_nonfinite_body_in_memory_and_streamed(tmp_path, streaming, invalid):
    assembled = accumulator(tmp_path, limit=100)
    writer = RecordingWriter()
    if streaming:
        assembled._writer = writer
    try:
        with pytest.raises(ValueError):
            assembled.add_segment(np.array([0.1, invalid], dtype=np.float32))
        assert writer.writes == []
    finally:
        assembled.close()


def test_finite_silence_and_clipping_remain_supported():
    writer = RecordingWriter()
    pcm = synthesis._write_audio_segment(
        writer, np.array([-2.0, -0.5, 0.0, 0.5, 2.0], dtype=np.float32),
    )
    assert pcm.tolist() == [-32767, -16383, 0, 16383, 32767]
    assert len(writer.writes) == 1
    assert synthesis._validate_complete_batch_results(
        runtime(), batch_results=[batch_result(np.zeros(24, dtype=np.float32))], expected_count=1,
    )


def test_in_memory_allocation_failure_still_falls_back_to_streaming(tmp_path, monkeypatch):
    assembled = accumulator(tmp_path, limit=100)
    assembled.add_segment(np.array([0.25, -0.25], dtype=np.float32))
    assembled.add_segment(np.array([0.5, -0.5], dtype=np.float32))

    def out_of_memory(*_args, **_kwargs):
        raise MemoryError("synthetic allocation failure")

    monkeypatch.setattr(synthesis.np, "concatenate", out_of_memory)
    output = synthesis._finalize_chapter_audio(runtime(), assembled)
    assert output == assembled.output_path
    with wave.open(str(output), "rb") as reader:
        assert reader.getnframes() == 4
        actual = np.frombuffer(reader.readframes(4), dtype=np.int16)
    assert actual.tolist() == [8191, -8191, 16383, -16383]


def test_concatenation_value_error_is_not_retried_as_a_memory_failure(tmp_path, monkeypatch):
    assembled = accumulator(tmp_path, limit=100)
    assembled.add_segment(np.array([0.25], dtype=np.float32))
    assembled.add_segment(np.array([0.5], dtype=np.float32))

    def invalid_shape(*_args, **_kwargs):
        raise ValueError("synthetic incompatible shapes")

    monkeypatch.setattr(synthesis.np, "concatenate", invalid_shape)
    with pytest.raises(ValueError, match="incompatible shapes"):
        synthesis._finalize_chapter_audio(runtime(), assembled)
    assert not assembled.output_path.exists()
