"""Audible chapter boundaries must survive encoding and exported playback."""

from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
import soundfile as sf

from modules.ingestion.models import Chapter
from modules.pipeline.chapter_narration import (
    add_chapter_narration, chapter_announcement, chapter_body,
)


@pytest.mark.parametrize(("title", "mode", "expected"), [
    ("Arrival", "audiobook", "Chapter number 1. Arrival."),
    ("Arrival", "document_audio", "Section number 1. Arrival."),
    ("Chapter 7: Arrival", "audiobook", "Chapter number 7. Arrival."),
    ("Section 2 — The Coast", "audiobook", "Section number 2. The Coast."),
    ("Chapter IV. Arrival", "audiobook", "Chapter number 4. Arrival."),
    ("Chapter One", "audiobook", "Chapter number One."),
    ("Chapter Twenty-One: The Return", "audiobook", "Chapter number Twenty-One. The Return."),
    ("Section 2.3: The Coast", "audiobook", "Section number 2.3. The Coast."),
    ("Chapter 1", "audiobook", "Chapter number 1."),
    ("", "audiobook", "Chapter number 1."),
])
def test_spoken_label_preserves_source_number_without_duplicate_prefix(title, mode, expected):
    assert chapter_announcement(Chapter(1, title, "Body."), mode) == expected


@pytest.mark.parametrize("content", [
    "# Chapter 1: Arrival\n\nThe ship arrived.",
    "Chapter 1\n\nArrival\nThe ship arrived.",
    "The ship arrived.",
])
def test_only_matching_opening_heading_is_removed(content):
    heading = content.removesuffix("The ship arrived.").rstrip()
    assert chapter_body(Chapter(1, "Chapter 1: Arrival", content, heading_text=heading)) == "The ship arrived."


def test_title_words_in_prose_are_not_removed():
    content = "Arrival was delayed.\n\nArrival\nThe ship arrived."
    assert chapter_body(Chapter(1, "Arrival", content)) == content


def test_document_section_label_survives_worker_configuration_roundtrip():
    from modules.pipeline.models import PipelineConfig
    from modules.pipeline.worker_config import build_worker_init_config, build_worker_pipeline_config

    serialized = build_worker_init_config(PipelineConfig(conversion_mode="document_audio"))
    restored = build_worker_pipeline_config(serialized["pipeline_config"])
    assert chapter_announcement(Chapter(1, "Arrival", "Body"), restored.conversion_mode) == "Section number 1. Arrival."


@pytest.mark.parametrize("file_backed", [False, True])
@pytest.mark.parametrize("sample_rate", [24000, 16000])
def test_audio_has_exact_three_second_gaps_and_updated_rms(tmp_path, file_backed, sample_rate):
    intro = np.full(sample_rate // 2, 0.25, dtype=np.float32)
    body = np.full(sample_rate, -0.25, dtype=np.float32)
    source = body
    if file_backed:
        source = tmp_path / "body.wav"
        sf.write(source, body, sample_rate, subtype="PCM_16")
    runtime = SimpleNamespace(
        config=SimpleNamespace(temp_dir=tmp_path, conversion_mode="audiobook", voice="test", speed=1.4),
        check_cancelled=Mock(return_value=False),
        synthesize_speech=Mock(return_value=intro),
        tts_sample_rate=lambda: sample_rate,
        track_temp_cleanup_path=Mock(),
    )
    result = SimpleNamespace(pcm_rms=999)
    output = add_chapter_narration(runtime, Chapter(1, "Arrival", "Body."), source, result)
    audio, rate = sf.read(output, dtype="float32")
    gap = 3 * sample_rate
    assert rate == sample_rate
    assert len(audio) == len(intro) + gap + len(body) + gap
    np.testing.assert_allclose(audio[:len(intro)], intro, atol=1 / 32768)
    np.testing.assert_array_equal(audio[len(intro):len(intro) + gap], 0)
    np.testing.assert_allclose(audio[len(intro) + gap:-gap], body, atol=1 / 32768)
    np.testing.assert_array_equal(audio[-gap:], 0)
    assert result.pcm_rms == pytest.approx(float(np.sqrt(np.mean(audio.astype(float) ** 2))))
    runtime.synthesize_speech.assert_called_once_with(
        text="Chapter number 1. Arrival.", voice="test", speed=1.4,
    )


def test_cancelled_intro_does_not_publish_partial_audio(tmp_path):
    runtime = SimpleNamespace(check_cancelled=lambda: True)
    assert add_chapter_narration(runtime, Chapter(1, "A", "Body"), np.ones(2), None) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("overlap", [False, True])
def test_processor_includes_introduction_for_both_synthesis_paths(tmp_path, overlap):
    from unittest.mock import patch
    from modules.pipeline import ConversionPipeline, PipelineConfig

    pipeline = ConversionPipeline(PipelineConfig(
        output_dir=tmp_path / "out", temp_dir=tmp_path / "temp", enable_chunk_overlap=overlap,
    ))
    processor = pipeline.stages.chapter_processor
    body = np.full(24000, 0.2, dtype=np.float32)
    with (
        patch.object(processor, "ensure_chapter_tts_engine"),
        patch.object(processor, "chunk_chapter_content", return_value=["First.", "Second."]),
        patch.object(processor, "clean_chapter_content", return_value=(["First.", "Second."], False)),
        patch.object(processor, "synthesize_chapter_chunks", return_value=body) as strict,
        patch.object(processor, "clean_and_synthesize_with_overlap", return_value=(body, False)) as overlapped,
        patch.object(type(processor.runtime), "synthesize_speech", return_value=np.ones(12000, dtype=np.float32)) as speak,
    ):
        result, output, cancelled = processor.prepare_chapter_audio(
            chapter=Chapter(1, "Arrival", "First. Second."), chapter_idx=0,
            total_chapters=1, output_dir=tmp_path / "out",
        )
    assert result.error is None
    assert not cancelled
    assert sf.info(output).frames == 180000  # 0.5s intro + 3s + 1s body + 3s
    assert overlapped.called is overlap
    assert strict.called is not overlap
    assert speak.call_args.kwargs["text"] == "Chapter number 1. Arrival."
