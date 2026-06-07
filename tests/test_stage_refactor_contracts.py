"""Contract tests for extracted cleaning and synthesis helpers."""

from __future__ import annotations

import time
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np


def test_build_cleaned_chunk_result_normalizes_trace_payload() -> None:
    from modules.pipeline.stages.cleaning import _build_cleaned_chunk_result

    trace = type(
        "Trace",
        (),
        {
            "mode": "llm_success",
            "llm_error": None,
            "original_text": "raw chunk",
            "rule_based_text": "rule text",
            "cleaned_text": "clean text",
            "artifact_score": {"total": 0.42, "cleaning_path": "light_llm"},
        },
    )()

    result = _build_cleaned_chunk_result(
        chunk_idx=0,
        total_chunks=3,
        original_chunk="raw chunk",
        cleaned_chunk="clean text",
        trace=trace,
        include_debug_row=True,
    )

    assert result.cleaned_text == "clean text"
    assert result.llm_input_text == "rule text"
    assert result.trace_row == {
        "chunk_index": 1,
        "total_chunks": 3,
        "mode": "llm_success",
        "llm_error": None,
        "original_text": "raw chunk",
        "rule_based_text": "rule text",
        "cleaned_text": "clean text",
        "artifact_score": {"total": 0.42, "cleaning_path": "light_llm"},
        "llm_used": True,
        "raw_chars": 9,
        "rule_clean_chars": 9,
        "cleaned_chars": 10,
    }


def test_chapter_audio_accumulator_spills_to_wav_when_threshold_exceeded(tmp_path: Path) -> None:
    from modules.pipeline.stages.synthesis import ChapterAudioAccumulator

    accumulator = ChapterAudioAccumulator(
        output_path=tmp_path / "chapter.wav",
        sample_rate=24000,
        max_in_memory_samples=3,
    )

    accumulator.add_segment(np.array([0.0, 0.25], dtype=np.float32))
    accumulator.add_segment(np.array([0.5, 0.75], dtype=np.float32))
    output = accumulator.finalize()

    assert isinstance(output, Path)
    with wave.open(str(output), "rb") as wav_file:
        assert wav_file.getnframes() == 4
        assert wav_file.getframerate() == 24000


def test_make_fallback_trace_updates_counters_and_preserves_error() -> None:
    from modules.tts.cleaner_trace import make_fallback_trace

    cleaner = SimpleNamespace(
        _llm_fallback_count=0,
        _total_chunks_cleaned=0,
        _total_time_ms=0.0,
    )

    trace = make_fallback_trace(
        cleaner,
        SimpleNamespace,
        original_text="raw",
        rule_based_text="rule",
        start_time=time.time() - 0.01,
        llm_error="network boom",
    )

    assert trace.original_text == "raw"
    assert trace.rule_based_text == "rule"
    assert trace.cleaned_text == "rule"
    assert trace.mode == "llm_fallback"
    assert trace.llm_error == "network boom"
    assert cleaner._llm_fallback_count == 1
    assert cleaner._total_chunks_cleaned == 1
    assert cleaner._total_time_ms > 0
