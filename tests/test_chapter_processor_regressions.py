"""Regression coverage for chapter processor."""

from types import SimpleNamespace
from unittest.mock import patch

from ebooklib import epub
import numpy as np
import pytest
import soundfile as sf

from modules.pipeline.chapter_narration import (
    CHAPTER_END_PAUSE_SECONDS,
    CHAPTER_HEADING_PAUSE_SECONDS,
)
from modules.text.document_normalizer import normalize_extracted_document


@pytest.mark.parametrize("mode", ["incremental", "overlap"])
@pytest.mark.parametrize("outcome", ["cancelled", "failed"])
def test_partial_chapter_wav_is_cleaned_after_interrupted_synthesis(
    tmp_path, monkeypatch, mode, outcome,
):
    from modules.pipeline import ConversionPipeline, PipelineConfig
    from modules.tts.strategies.base import SynthesisResult

    pipeline = ConversionPipeline(PipelineConfig(
        output_dir=tmp_path / "output", temp_dir=tmp_path / "temp", enable_parallel=False,
    ))
    pipeline.config.max_in_memory_chapter_samples = 1
    previous_chapter = pipeline.config.temp_dir / "chapter_01_narrated.wav"
    previous_chapter.write_bytes(b"previous chapter")
    pipeline._runtime.track_temp_cleanup_path(previous_chapter)
    unrelated = pipeline.config.temp_dir / "chapter_99.wav"
    unrelated.write_bytes(b"another run")

    class Engine:
        supports_incremental_batch_results = True
        sample_rate = 24000
        is_loaded = True

        def synthesize(self, text, **_kwargs):
            if text == "second":
                if outcome == "cancelled":
                    pipeline.cancel()
                else:
                    raise RuntimeError("synthesis failed")
            return np.ones(4, dtype=np.float32)

        def iter_synthesize_batch(self, items, progress_callback=None):
            for item in items:
                try:
                    audio = self.synthesize(item.text)
                    yield SynthesisResult(audio=audio, index=item.index)
                except RuntimeError as exc:
                    yield SynthesisResult(audio=None, index=item.index, error=str(exc))

    pipeline._tts_engine = Engine()
    processor = pipeline.stages.chapter_processor
    chapter = SimpleNamespace(number=2, title="Second chapter")
    if mode == "incremental":
        audio = processor.synthesize_chapter_chunks(
            chapter=chapter, chapter_idx=1, total_chapters=2, chunks=["first", "second"],
        )
    else:
        monkeypatch.setattr(processor, "get_text_cleaner_for_chapter", lambda: object())
        monkeypatch.setattr(
            processor, "clean_chunk_safely", lambda **kwargs: (kwargs["chunk"], True, None),
        )
        for name in (
            "save_pre_clean_markdown", "maybe_capture_cleaner_stats", "log_cleaner_stats_delta",
            "save_cleaning_artifacts",
        ):
            monkeypatch.setattr(processor, name, lambda *_args, **_kwargs: None)
        audio, cancelled = processor.clean_and_synthesize_with_overlap(
            chapter=chapter, chapter_idx=1, total_chapters=2, chunks=["first", "second"],
            output_dir=tmp_path,
        )
        assert cancelled == (outcome == "cancelled")

    assert audio is None
    partial = pipeline.config.temp_dir / "chapter_02.wav"
    assert partial.exists()

    pipeline._cleanup_temp()

    assert not partial.exists()
    assert not previous_chapter.exists()
    assert unrelated.read_bytes() == b"another run"


def test_07_heading_only_part_generates_announcement(tmp_path):
    from modules.ingestion.epub_parser import EPUBParser
    from modules.pipeline import ConversionPipeline, PipelineConfig

    book = epub.EpubBook()
    book.set_identifier("part-divider")
    book.set_title("Voyage")
    book.set_language("en")
    part = epub.EpubHtml(title="Part I", file_name="part.xhtml")
    part.content = '<h1 id="part">Part I</h1><h1 id="arrival">Arrival</h1><p>' + "The ship approached the shore at dawn. " * 20 + "</p>"
    book.add_item(part)
    book.spine = [part]
    book.toc = [epub.Link("part.xhtml#part", "Part I", "part"), epub.Link("part.xhtml#arrival", "Arrival", "arrival")]
    book.add_item(epub.EpubNcx())
    path = tmp_path / "part.epub"
    epub.write_epub(str(path), book)
    document = normalize_extracted_document(EPUBParser(path).parse(), source_type="epub")
    chapter = document.chapters[0]
    assert chapter.title == "Part I"
    pipeline = ConversionPipeline(PipelineConfig(output_dir=tmp_path / "out", temp_dir=tmp_path / "temp"))
    processor = pipeline.stages.chapter_processor
    with (
        patch.object(processor, "ensure_chapter_tts_engine"),
        patch.object(type(processor.runtime), "synthesize_speech", return_value=np.ones(12000, dtype=np.float32)) as speak,
    ):
        result, output, cancelled = processor.prepare_chapter_audio(
            chapter=chapter, chapter_idx=0, total_chapters=2, output_dir=tmp_path / "out",
        )
    assert result.error is None
    assert not cancelled
    # Announcement plus the heading and end pauses; a heading-only part has no body.
    assert sf.info(output).frames == 12000 + int(
        (CHAPTER_HEADING_PAUSE_SECONDS + CHAPTER_END_PAUSE_SECONDS) * 24000
    )
    assert speak.call_args.kwargs["text"] == "Part number 1."
