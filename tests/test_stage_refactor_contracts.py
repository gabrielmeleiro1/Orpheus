"""Contract tests for extracted cleaning and synthesis helpers."""

from __future__ import annotations

import time
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np


def test_timed_conversion_pipeline_populates_parse_and_package_metrics(
    tmp_path: Path,
) -> None:
    from modules.pipeline.models import PipelineConfig
    from modules.pipeline.stage_metrics import StageTimings
    from scripts.benchmark_nagel_pipeline import _create_timed_pipeline

    config = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        enable_parallel=False,
    )
    timings = StageTimings()
    pipeline = _create_timed_pipeline(config, timings)
    test_file = tmp_path / "test.txt"
    test_file.write_text("Contract stage metrics body text. " * 40, encoding="utf-8")

    with patch("modules.ingestion.text_parser.TextParser") as mock_parser, patch(
        "modules.tts.cleaner.TextCleaner"
    ) as mock_cleaner, patch("modules.tts.chunker.TextChunker") as mock_chunker, patch(
        "modules.tts.factory.TTSEngineFactory.create"
    ) as mock_tts_factory, patch("modules.tts.engine.concatenate_audio_files") as mock_concat, patch(
        "modules.audio.processor.AudioProcessor"
    ) as mock_proc, patch("modules.audio.encoder.AudioEncoder") as mock_enc, patch(
        "modules.audio.packager.M4BPackager"
    ) as mock_packager:
        from modules.ingestion.models import Chapter, Document
        from modules.tts.engine import BatchResult

        mock_parser.return_value.parse.return_value = Document(
            title="Contract Book",
            author="Tester",
            chapters=[
                Chapter(number=1, title="Chapter 1", content="Contract stage metrics body text. " * 40),
            ],
        )
        mock_cleaner_instance = MagicMock()
        mock_cleaner_instance.clean.side_effect = lambda text: text
        mock_cleaner.return_value = mock_cleaner_instance
        mock_chunker.return_value.chunk.return_value = ["chunk1"]

        def mock_synthesize_batch(items, progress_callback=None):
            results = []
            for index, _item in enumerate(items):
                results.append(
                    BatchResult(
                        audio=np.array([0.1, 0.2, 0.3], dtype=np.float32),
                        index=index,
                        error=None,
                        duration_ms=100,
                    )
                )
                if progress_callback:
                    progress_callback(index + 1, len(items))
            return results

        mock_tts_instance = MagicMock()
        mock_tts_instance.synthesize_batch.side_effect = mock_synthesize_batch
        mock_tts_instance.synthesize.return_value = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        mock_tts_instance.sample_rate = 24000
        mock_tts_factory.return_value = mock_tts_instance

        def mock_concat_side_effect(file_paths, output_path, use_memmap=True):
            _ = file_paths, use_memmap
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"fake wav content")
            return Path(output_path)

        mock_concat.side_effect = mock_concat_side_effect
        mock_proc.return_value.load.return_value = MagicMock()
        mock_proc.return_value.normalize_volume.return_value = MagicMock()
        mock_enc.return_value.get_duration.return_value = 10.0

        def wav_to_mp3_side_effect(wav_path, output_path, bitrate="128k"):
            _ = wav_path, bitrate
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"fake mp3 content")

        mock_enc.return_value.wav_to_mp3.side_effect = wav_to_mp3_side_effect

        def create_m4b_side_effect(*args, **kwargs):
            _ = args
            output_path = Path(kwargs["output_path"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"fake m4b content")
            return output_path

        mock_packager.return_value.create_m4b.side_effect = create_m4b_side_effect

        result = pipeline.convert(test_file)

    assert result.success
    assert timings.get("parse") > 0
    assert timings.get("package") > 0


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
