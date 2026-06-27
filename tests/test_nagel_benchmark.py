from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def _create_text_pdf(path: Path, text: str) -> None:
    import fitz

    doc = fitz.open()
    line = " ".join(text.split())[:72]
    for _ in range(2):
        page = doc.new_page()
        for index in range(36):
            page.insert_text((72, 72 + (index * 16)), line, fontsize=10)
    doc.set_metadata({"title": "Benchmark Fixture", "author": "Tester"})
    doc.save(path)
    doc.close()


def _create_image_only_pdf(path: Path, *, pages: int = 2) -> None:
    import fitz

    doc = fitz.open()
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 10, 10), False)
    pixmap.clear_with(230)
    for _ in range(pages):
        page = doc.new_page()
        page.insert_image(page.rect, pixmap=pixmap)
    doc.save(path)
    doc.close()


def test_parse_only_benchmark_reports_pipeline_metrics(tmp_path: Path) -> None:
    from scripts.benchmark_nagel_pipeline import BENCHMARK_SCHEMA_KEYS, run_benchmark

    source = tmp_path / "fixture.pdf"
    _create_text_pdf(source, "Benchmark body text for parse-only metrics. " * 200)

    metrics = run_benchmark(
        source_path=source,
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        parse_only=True,
        min_extracted_chars=100,
    )

    assert set(metrics) == set(BENCHMARK_SCHEMA_KEYS)
    persisted = json.loads((tmp_path / "output" / "benchmark_summary.json").read_text())
    assert persisted == metrics
    assert metrics["success"] is True
    assert metrics["schema_version"] >= 2
    assert metrics["mode"] == "parse_only"
    assert metrics["source_path"] == str(source.resolve())
    assert metrics["source_hash"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert metrics["tts_engine"] == "kokoro"
    assert metrics["cleaner_backend"] == "mlx"
    assert metrics["parallel_requested"] is True
    assert metrics["requested_parallel_chapters"] == 2
    assert metrics["effective_parallel_chapters"] == 2
    assert metrics["memory_safe_mode"] is False
    assert metrics["safe_mode_reason"] is None
    assert metrics["warmup_memory_delta_bytes"] is None
    assert metrics["estimated_per_worker_memory_bytes"] is None
    assert metrics["selected_worker_count_reason"] == "parallel requested without full pipeline calibration"
    assert metrics["pdf_extraction_backend"] == "fitz"
    assert metrics["parse_seconds"] >= 0
    assert metrics["extracted_chars"] >= 100
    assert metrics["chunks"] >= 1
    assert metrics["clean_seconds_total"] == 0
    assert metrics["clean_seconds_per_chunk"] is None
    assert metrics["tts_seconds_total"] == 0
    assert metrics["tts_seconds_per_chunk"] is None
    assert metrics["encode_seconds_total"] == 0
    assert metrics["package_seconds_total"] == 0
    assert metrics["wall_time_seconds"] >= metrics["parse_seconds"]
    assert metrics["processing_time_seconds"] >= metrics["parse_seconds"]
    assert metrics["total_duration_ms"] == 0
    assert metrics["output_path"] is None
    assert metrics["pipeline_parse_seconds"] == 0
    assert metrics["chapter_errors"] == []
    assert metrics["error"] is None
    assert metrics["errors"] == []
    assert metrics["ocr_required"] is False
    assert metrics["source_classification"] == "native_text"
    assert metrics["source_classification_confidence"] >= 0.8


def test_parse_only_benchmark_marks_partial_extraction_invalid(tmp_path: Path) -> None:
    from scripts import benchmark_nagel_pipeline

    source = tmp_path / "partial.txt"
    source.write_text("too short", encoding="utf-8")

    metrics = benchmark_nagel_pipeline.run_benchmark(
        source_path=source,
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        parse_only=True,
        min_extracted_chars=1000,
    )

    assert metrics["success"] is False
    assert metrics["extracted_chars"] == len("too short")
    assert "below minimum" in metrics["error"]
    assert metrics["errors"] == [metrics["error"]]


def test_parse_only_benchmark_can_limit_real_book_chapters(tmp_path: Path) -> None:
    from scripts import benchmark_nagel_pipeline

    source = tmp_path / "fixture.md"
    source.write_text(
        "# One\n\n" + ("First chapter body. " * 40) + "\n\n"
        "# Two\n\n" + ("Second chapter body. " * 40) + "\n\n"
        "# Three\n\n" + ("Third chapter body. " * 40),
        encoding="utf-8",
    )

    metrics = benchmark_nagel_pipeline.run_benchmark(
        source_path=source,
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        parse_only=True,
        min_extracted_chars=100,
        chapter_limit=2,
    )

    assert metrics["success"] is True
    assert metrics["chapter_limit"] == 2
    assert metrics["chapters"] == 2
    assert "Third chapter body" not in " ".join(
        issue.get("excerpt", "")
        for issue in metrics["artifact_quality"].get("issues", [])
    )


def test_parse_document_honors_non_prose_skip_opt_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ingestion.models import Chapter, Document
    from modules.pipeline.models import PipelineConfig
    from scripts import benchmark_nagel_pipeline

    class FakeMarkdownParser:
        def __init__(self, _source_path: Path) -> None:
            pass

        def parse(self) -> Document:
            return Document(
                title="Benchmark Source",
                chapters=[
                    Chapter(
                        number=1,
                        title="Chapter 1",
                        content="This narrated paragraph belongs in the benchmark. " * 8,
                    ),
                    Chapter(
                        number=2,
                        title="Index",
                        content="\n".join(
                            [
                                "Anderson, Mary 12, 44, 109",
                                "Barnes, Thomas 15, 61",
                                "Carter, Evelyn 18, 72, 119",
                                "Dawson, River 31, 88",
                            ]
                        ),
                    ),
                ],
            )

    source = tmp_path / "fixture.md"
    source.write_text("# fixture", encoding="utf-8")
    monkeypatch.setattr(benchmark_nagel_pipeline, "MarkdownParser", FakeMarkdownParser)

    skip_config = PipelineConfig(skip_non_prose_sections=True)
    keep_config = PipelineConfig(skip_non_prose_sections=False)

    skipped_document = benchmark_nagel_pipeline._parse_document(source, skip_config)
    kept_document = benchmark_nagel_pipeline._parse_document(source, keep_config)

    assert [chapter.title for chapter in skipped_document.chapters] == ["Chapter 1"]
    assert [chapter.title for chapter in kept_document.chapters] == [
        "Chapter 1",
        "Index",
    ]


def test_parse_only_benchmark_marks_empty_extraction_invalid(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import benchmark_nagel_pipeline

    source = tmp_path / "fixture.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")

    class _EmptyPDFParser:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def parse(self):
            return SimpleNamespace(
                title="Empty",
                author=None,
                total_pages=1,
                chapters=[SimpleNamespace(content="")],
            )

    monkeypatch.setattr(benchmark_nagel_pipeline, "PDFParser", _EmptyPDFParser)

    metrics = benchmark_nagel_pipeline.run_benchmark(
        source_path=source,
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        parse_only=True,
        min_extracted_chars=100,
    )

    assert metrics["success"] is False
    assert metrics["extracted_chars"] == 0
    assert metrics["chunks"] == 0
    assert "no extractable text" in metrics["error"]
    assert metrics["errors"] == [metrics["error"]]


def test_parse_only_benchmark_classifies_image_only_pdf_as_ocr_required(tmp_path: Path) -> None:
    from scripts import benchmark_nagel_pipeline

    source = tmp_path / "scanned.pdf"
    _create_image_only_pdf(source)

    metrics = benchmark_nagel_pipeline.run_benchmark(
        source_path=source,
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        parse_only=True,
        min_extracted_chars=100,
    )

    assert set(metrics) == set(benchmark_nagel_pipeline.BENCHMARK_SCHEMA_KEYS)
    assert metrics["success"] is False
    assert metrics["ocr_required"] is True
    assert metrics["source_classification"] == "ocr_required"
    assert metrics["source_classification_confidence"] >= 0.9
    assert metrics["extracted_chars"] == 0
    assert metrics["chunks"] == 0
    assert "OCR required" in metrics["error"]
    assert "not text-extractable" in metrics["error"]
    assert metrics["errors"] == [metrics["error"]]


def test_full_benchmark_reports_stage_timings_without_hardcoded_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from modules.pipeline.models import ChapterResult, ConversionResult
    from scripts import benchmark_nagel_pipeline

    source = tmp_path / "fixture.txt"
    source.write_text("Benchmark full mode body text. " * 40, encoding="utf-8")

    class _FakePipeline:
        def __init__(self, config) -> None:
            self.config = config
            self.config.memory_policy = SimpleNamespace(
                safe_mode_reason=None,
                measured_warmup_delta_bytes=512,
                estimated_per_worker_memory_bytes=1024,
                selected_worker_count_reason="warmup estimate with live headroom",
            )

        def _ingest(self, _source_path):
            return SimpleNamespace(
                title="Fake",
                author="Tester",
                chapters=[
                    SimpleNamespace(
                        number=1,
                        title="Chapter 1",
                        content="Benchmark full mode body text. " * 40,
                    )
                ],
            )

        def _clean_chapter_content(self, *_args, **_kwargs):
            time.sleep(0.001)
            return ["Benchmark full mode body text. " * 40], False

        def _synthesize_chapter_chunks(self, *_args, **_kwargs):
            time.sleep(0.001)
            return tmp_path / "chapter.wav"

        def _encode_chapter_audio(self, *_args, **_kwargs):
            time.sleep(0.001)
            mp3_path = tmp_path / "chapter.mp3"
            mp3_path.write_bytes(b"mp3")
            return mp3_path, 1234

        def _package(self, *_args, **_kwargs):
            time.sleep(0.001)
            output_path = tmp_path / "book.m4b"
            output_path.write_bytes(b"m4b")
            return output_path

        def _cleanup_models(self):
            pass

        def _cleanup_temp(self):
            pass

        def convert(self, _source_path):
            document = self._ingest(_source_path)
            chapter = document.chapters[0]
            chapters_dir = self.config.output_dir / "Fake" / "chapters"
            chapters_dir.mkdir(parents=True)
            (chapters_dir / "chapter_01_before_cleaning.md").write_text(
                "# Chapter 1\n\n" + chapter.content,
                encoding="utf-8",
            )
            chunks, _ = self._clean_chapter_content(
                chapter,
                0,
                1,
                ["raw text"],
                tmp_path,
            )
            (chapters_dir / "chapter_01_cleaned.md").write_text(
                "# Chapter 1\n\n" + "\n\n".join(chunks),
                encoding="utf-8",
            )
            self._synthesize_chapter_chunks(chapter, 0, 1, chunks)
            mp3_path, duration_ms = self._encode_chapter_audio(
                chapter,
                0,
                1,
                tmp_path,
                tmp_path / "chapter.wav",
            )
            output_path = self._package([], document.title, document.author, tmp_path)
            return ConversionResult(
                success=True,
                title=document.title,
                author=document.author,
                output_path=output_path,
                chapters=[
                    ChapterResult(
                        chapter_number=1,
                        chapter_title=chapter.title,
                        mp3_path=mp3_path,
                        duration_ms=duration_ms,
                    )
                ],
                total_duration_ms=duration_ms,
                processing_time_seconds=0.01,
            )

    monkeypatch.setattr(benchmark_nagel_pipeline, "ConversionPipeline", _FakePipeline)

    metrics = benchmark_nagel_pipeline.run_benchmark(
        source_path=source,
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        parse_only=False,
        tts_engine="kokoro",
        tts_model_name="custom/local-kokoro",
        tts_quantization="8bit",
        cleaner_backend="mlx",
        cleaner_model_name="custom/local-cleaner",
        cleaner_skip_llm_when_not_needed=True,
        voice="af_bella",
        chunk_size=250,
        min_extracted_chars=100,
    )

    assert set(metrics) == set(benchmark_nagel_pipeline.BENCHMARK_SCHEMA_KEYS)
    assert metrics["success"] is True
    assert metrics["mode"] == "full"
    assert metrics["cleaner_backend"] == "mlx"
    assert metrics["cleaner_model_name"] == "custom/local-cleaner"
    assert metrics["cleaner_skip_llm_when_not_needed"] is True
    assert metrics["tts_model_name"] == "custom/local-kokoro"
    assert metrics["tts_quantization"] == "8bit"
    assert metrics["voice"] == "af_bella"
    assert metrics["chunk_size"] == 250
    assert metrics["emit_chapter_mp3_artifacts"] is False
    assert metrics["parallel_requested"] is True
    assert metrics["requested_parallel_chapters"] == 2
    assert metrics["effective_parallel_chapters"] == 2
    assert metrics["memory_safe_mode"] is False
    assert metrics["safe_mode_reason"] is None
    assert metrics["warmup_memory_delta_bytes"] == 512
    assert metrics["estimated_per_worker_memory_bytes"] == 1024
    assert metrics["selected_worker_count_reason"] == "warmup estimate with live headroom"
    assert metrics["clean_seconds_total"] > 0
    assert metrics["clean_seconds_per_chunk"] is not None
    assert metrics["tts_seconds_total"] > 0
    assert metrics["tts_seconds_per_chunk"] is not None
    assert metrics["encode_seconds_total"] > 0
    assert metrics["package_seconds_total"] > 0
    assert metrics["total_duration_ms"] == 1234
    assert metrics["output_path"] == str(tmp_path / "book.m4b")
    assert metrics["artifact_quality"]["success"] is True
    assert metrics["artifact_quality"]["checked_files"]
    assert metrics["tts_chunk_profile"] == {
        "enabled": False,
        "path": None,
        "rows": 0,
        "errors": 0,
    }


def test_full_benchmark_writes_tts_chunk_profile_when_enabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import wave

    import numpy as np

    from modules.pipeline.models import ChapterResult, ConversionResult
    from scripts import benchmark_nagel_pipeline

    source = tmp_path / "fixture.txt"
    source.write_text("Benchmark full mode body text. " * 40, encoding="utf-8")

    class _FakeTTSEngine:
        sample_rate = 24000

        def __init__(self) -> None:
            self._retry_count = 0
            self._batch_pause_total_ms = 0.0

        def synthesize(self, text: str, voice: str, speed: float):
            assert voice == "af_bella"
            assert speed == 1.1
            return np.ones(2400, dtype=np.float32)

        def synthesize_to_output_file(self, text: str, output_path: Path, voice: str, speed: float):
            assert text == "second chunk"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(output_path), "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(self.sample_rate)
                writer.writeframes(b"\0\0" * 1200)
            return output_path

    class _FakePipeline:
        def __init__(self, config) -> None:
            self.config = config
            self._tts_engine = None

        def _ingest(self, _source_path):
            return SimpleNamespace(
                title="Fake",
                author="Tester",
                chapters=[
                    SimpleNamespace(
                        number=1,
                        title="Chapter 1",
                        content="Benchmark full mode body text. " * 40,
                    )
                ],
            )

        def _ensure_chapter_tts_engine(self):
            if self._tts_engine is None:
                self._tts_engine = _FakeTTSEngine()

        def convert(self, _source_path):
            document = self._ingest(_source_path)
            chapters_dir = self.config.output_dir / "Fake" / "chapters"
            chapters_dir.mkdir(parents=True)
            (chapters_dir / "chapter_01_before_cleaning.md").write_text(
                "# Chapter 1\n\n" + document.chapters[0].content,
                encoding="utf-8",
            )
            (chapters_dir / "chapter_01_cleaned.md").write_text(
                "# Chapter 1\n\n" + document.chapters[0].content,
                encoding="utf-8",
            )

            self._ensure_chapter_tts_engine()
            self._tts_engine.synthesize("first chunk", voice="af_bella", speed=1.1)
            chunk_path = self.config.temp_dir / "chunk.wav"
            self._tts_engine.synthesize_to_output_file(
                "second chunk",
                chunk_path,
                voice="af_bella",
                speed=1.1,
            )
            output_path = tmp_path / "book.m4b"
            output_path.write_bytes(b"m4b")
            chapter = ChapterResult(
                chapter_number=1,
                chapter_title="Chapter 1",
                duration_ms=150,
                stage_timings={"tts": 0.2},
            )
            return ConversionResult(
                success=True,
                title="Fake",
                author="Tester",
                output_path=output_path,
                chapters=[chapter],
                total_duration_ms=150,
                processing_time_seconds=0.3,
            )

    monkeypatch.setattr(benchmark_nagel_pipeline, "ConversionPipeline", _FakePipeline)

    metrics = benchmark_nagel_pipeline.run_benchmark(
        source_path=source,
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        parse_only=False,
        voice="af_bella",
        min_extracted_chars=100,
        profile_tts_chunks=True,
    )

    profile = metrics["tts_chunk_profile"]
    assert profile["enabled"] is True
    assert profile["rows"] == 2
    assert profile["errors"] == 0

    profile_path = Path(profile["path"])
    assert profile_path == tmp_path / "output" / "tts_chunk_profile.jsonl"
    rows = [
        json.loads(line)
        for line in profile_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert [row["call_index"] for row in rows] == [1, 2]
    assert rows[0]["method"] == "synthesize"
    assert rows[0]["output_mode"] == "array"
    assert rows[0]["text_chars"] == len("first chunk")
    assert rows[0]["voice"] == "af_bella"
    assert rows[0]["speed"] == 1.1
    assert rows[0]["audio_samples"] == 2400
    assert rows[0]["duration_ms"] == 100
    assert rows[0]["elapsed_seconds"] >= 0
    assert rows[0]["error"] is None

    assert rows[1]["method"] == "synthesize_to_output_file"
    assert rows[1]["output_mode"] == "file"
    assert rows[1]["text_chars"] == len("second chunk")
    assert rows[1]["audio_samples"] is None
    assert rows[1]["duration_ms"] == 50
    assert rows[1]["audio_path"] == str(tmp_path / "temp" / "chunk.wav")


def test_tts_chunk_profile_wraps_loaded_strategy_engine(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import numpy as np

    from modules.pipeline.models import ChapterResult, ConversionResult
    from modules.tts.strategies.base import SynthesisItem, SynthesisResult
    from scripts import benchmark_nagel_pipeline

    source = tmp_path / "fixture.txt"
    source.write_text("Benchmark full mode body text. " * 40, encoding="utf-8")

    class _InnerEngine:
        sample_rate = 24000

        def __init__(self) -> None:
            self._retry_count = 0
            self._batch_pause_total_ms = 0.0

        def synthesize(self, text: str, voice: str, speed: float):
            return np.ones(1200, dtype=np.float32)

        def iter_synthesize_batch(self, items, progress_callback=None):
            total = len(items)
            for completed, item in enumerate(items, start=1):
                audio = self.synthesize(item.text, item.voice, item.speed)
                if progress_callback is not None:
                    progress_callback(completed, total)
                yield SynthesisResult(
                    audio=audio,
                    index=item.index,
                    error=None,
                    duration_ms=50,
                )

    class _Strategy:
        sample_rate = 24000

        def __init__(self) -> None:
            self._engine = _InnerEngine()

        def iter_synthesize_batch(self, items, progress_callback=None):
            yield from self._engine.iter_synthesize_batch(items, progress_callback)

    class _FakePipeline:
        def __init__(self, config) -> None:
            self.config = config
            self._tts_engine = None

        def _ingest(self, _source_path):
            return SimpleNamespace(
                title="Fake",
                author="Tester",
                chapters=[
                    SimpleNamespace(
                        number=1,
                        title="Chapter 1",
                        content="Benchmark full mode body text. " * 40,
                    )
                ],
            )

        def _ensure_chapter_tts_engine(self):
            if self._tts_engine is None:
                self._tts_engine = _Strategy()

        def convert(self, _source_path):
            document = self._ingest(_source_path)
            chapters_dir = self.config.output_dir / "Fake" / "chapters"
            chapters_dir.mkdir(parents=True)
            (chapters_dir / "chapter_01_before_cleaning.md").write_text(
                "# Chapter 1\n\n" + document.chapters[0].content,
                encoding="utf-8",
            )
            (chapters_dir / "chapter_01_cleaned.md").write_text(
                "# Chapter 1\n\n" + document.chapters[0].content,
                encoding="utf-8",
            )
            self._ensure_chapter_tts_engine()
            list(
                self._tts_engine.iter_synthesize_batch(
                    [
                        SynthesisItem(text="first chunk", voice="af_bella", speed=1.0, index=0),
                        SynthesisItem(text="second chunk", voice="af_bella", speed=1.0, index=1),
                    ]
                )
            )
            output_path = tmp_path / "book.m4b"
            output_path.write_bytes(b"m4b")
            return ConversionResult(
                success=True,
                title="Fake",
                author="Tester",
                output_path=output_path,
                chapters=[
                    ChapterResult(
                        chapter_number=1,
                        chapter_title="Chapter 1",
                        duration_ms=100,
                        stage_timings={"tts": 0.1},
                    )
                ],
                total_duration_ms=100,
                processing_time_seconds=0.2,
            )

    monkeypatch.setattr(benchmark_nagel_pipeline, "ConversionPipeline", _FakePipeline)

    metrics = benchmark_nagel_pipeline.run_benchmark(
        source_path=source,
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        parse_only=False,
        min_extracted_chars=100,
        profile_tts_chunks=True,
    )

    profile_path = Path(metrics["tts_chunk_profile"]["path"])
    rows = [
        json.loads(line)
        for line in profile_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert metrics["tts_chunk_profile"]["rows"] == 2
    assert [row["text_chars"] for row in rows] == [
        len("first chunk"),
        len("second chunk"),
    ]


def test_full_benchmark_reports_chapter_mp3_artifact_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from modules.pipeline.models import ConversionResult
    from scripts import benchmark_nagel_pipeline

    source = tmp_path / "fixture.txt"
    source.write_text("Benchmark full mode body text. " * 40, encoding="utf-8")

    class _FakePipeline:
        def __init__(self, config) -> None:
            self.config = config

        def convert(self, _source_path):
            assert self.config.emit_chapter_mp3_artifacts is True
            chapters_dir = self.config.output_dir / "Fake" / "chapters"
            chapters_dir.mkdir(parents=True)
            (chapters_dir / "chapter_01_before_cleaning.md").write_text(
                "# Chapter 1\n\n" + ("Benchmark full mode body text. " * 40),
                encoding="utf-8",
            )
            (chapters_dir / "chapter_01_cleaned.md").write_text(
                "# Chapter 1\n\n" + ("Benchmark full mode body text. " * 40),
                encoding="utf-8",
            )
            output_path = tmp_path / "book.m4b"
            output_path.write_bytes(b"m4b")
            return ConversionResult(
                success=True,
                title="Fake",
                author=None,
                output_path=output_path,
                total_duration_ms=1000,
                processing_time_seconds=0.01,
            )

    monkeypatch.setattr(benchmark_nagel_pipeline, "ConversionPipeline", _FakePipeline)

    metrics = benchmark_nagel_pipeline.run_benchmark(
        source_path=source,
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        parse_only=False,
        min_extracted_chars=100,
        emit_chapter_mp3_artifacts=True,
    )

    assert metrics["success"] is True
    assert metrics["emit_chapter_mp3_artifacts"] is True


def test_full_benchmark_includes_worker_stage_timings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from modules.pipeline.models import ChapterResult, ConversionResult
    from scripts import benchmark_nagel_pipeline

    source = tmp_path / "fixture.txt"
    source.write_text("Benchmark full mode body text. " * 40, encoding="utf-8")

    class _FakePipeline:
        def __init__(self, config) -> None:
            self.config = config

        def _ingest(self, _source_path):
            return SimpleNamespace(
                title="Fake",
                author="Tester",
                chapters=[
                    SimpleNamespace(
                        number=1,
                        title="Chapter 1",
                        content="Benchmark full mode body text. " * 40,
                    )
                ],
            )

        def convert(self, _source_path):
            chapters_dir = self.config.output_dir / "Fake" / "chapters"
            chapters_dir.mkdir(parents=True)
            (chapters_dir / "chapter_01_before_cleaning.md").write_text(
                "# Chapter 1\n\n" + ("Benchmark full mode body text. " * 40),
                encoding="utf-8",
            )
            (chapters_dir / "chapter_01_cleaned.md").write_text(
                "# Chapter 1\n\n" + ("Benchmark full mode body text. " * 40),
                encoding="utf-8",
            )
            output_path = tmp_path / "book.m4b"
            output_path.write_bytes(b"m4b")
            chapter = ChapterResult(
                chapter_number=1,
                chapter_title="Chapter 1",
                duration_ms=1234,
            )
            chapter.stage_timings = {
                "clean": 2.0,
                "tts": 3.0,
                "encode": 4.0,
                "tts_model_load": 5.0,
            }
            return ConversionResult(
                success=True,
                title="Fake",
                author="Tester",
                output_path=output_path,
                chapters=[chapter],
                total_duration_ms=1234,
                processing_time_seconds=14.0,
            )

        def _cleanup_models(self):
            pass

        def _cleanup_temp(self):
            pass

    monkeypatch.setattr(benchmark_nagel_pipeline, "ConversionPipeline", _FakePipeline)

    metrics = benchmark_nagel_pipeline.run_benchmark(
        source_path=source,
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        parse_only=False,
        min_extracted_chars=100,
    )

    assert metrics["success"] is True
    assert metrics["clean_seconds_total"] == 2.0
    assert metrics["tts_seconds_total"] == 3.0
    assert metrics["encode_seconds_total"] == 4.0
    assert metrics["tts_model_load_seconds_total"] == 5.0


def test_full_benchmark_fails_when_handoff_artifacts_are_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from modules.pipeline.models import ConversionResult
    from scripts import benchmark_nagel_pipeline

    source = tmp_path / "fixture.txt"
    source.write_text("Benchmark full mode body text. " * 40, encoding="utf-8")

    class _FakePipeline:
        def __init__(self, config) -> None:
            self.config = config

        def convert(self, _source_path):
            output_path = tmp_path / "book.m4b"
            output_path.write_bytes(b"m4b")
            return ConversionResult(
                success=True,
                title="Fake",
                author=None,
                output_path=output_path,
                total_duration_ms=1234,
                processing_time_seconds=0.01,
            )

    monkeypatch.setattr(benchmark_nagel_pipeline, "ConversionPipeline", _FakePipeline)

    metrics = benchmark_nagel_pipeline.run_benchmark(
        source_path=source,
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        parse_only=False,
        min_extracted_chars=100,
    )

    assert metrics["success"] is False
    assert metrics["artifact_quality"]["success"] is False
    assert any(
        issue["type"] == "missing_handoff_artifacts"
        for issue in metrics["artifact_quality"]["issues"]
    )


def test_full_benchmark_marks_interrupted_runs_unsuccessful(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import benchmark_nagel_pipeline

    source = tmp_path / "fixture.txt"
    source.write_text("Benchmark full mode body text. " * 40, encoding="utf-8")

    class _InterruptedPipeline:
        def __init__(self, config) -> None:
            self.config = config

        def convert(self, _source_path):
            raise KeyboardInterrupt

    monkeypatch.setattr(
        benchmark_nagel_pipeline,
        "ConversionPipeline",
        _InterruptedPipeline,
    )

    with pytest.raises(KeyboardInterrupt):
        benchmark_nagel_pipeline.run_benchmark(
            source_path=source,
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            parse_only=False,
            min_extracted_chars=100,
        )

    metrics = json.loads((tmp_path / "output" / "benchmark_summary.json").read_text())
    assert metrics["success"] is False
    assert metrics["error"] == "interrupted"
    assert metrics["errors"] == ["interrupted"]


def test_worker_selection_comparison_runs_sequential_static_and_warmup_modes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import benchmark_nagel_pipeline

    source = tmp_path / "fixture.epub"
    source.write_bytes(b"epub")
    calls: list[dict[str, object]] = []

    def fake_run_benchmark(**kwargs):
        calls.append(kwargs)
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        metrics = {
            "mode": "full",
            "success": True,
            "wall_time_seconds": float(len(calls)),
            "clean_seconds_total": 0.1,
            "tts_seconds_total": 0.2,
            "encode_seconds_total": 0.3,
            "package_seconds_total": 0.4,
            "effective_parallel_chapters": kwargs["max_parallel_chapters"],
            "estimated_per_worker_memory_bytes": None,
            "artifact_quality": {"success": True},
        }
        (output_dir / "benchmark_summary.json").write_text(json.dumps(metrics))
        return metrics

    monkeypatch.setattr(benchmark_nagel_pipeline, "run_benchmark", fake_run_benchmark)

    summary = benchmark_nagel_pipeline.run_worker_selection_comparison(
        source_path=source,
        output_dir=tmp_path / "comparison",
        temp_dir=tmp_path / "temp",
        max_parallel_chapters=3,
        chapter_limit=2,
        parallel_worker_mode="manual",
    )

    assert [case["name"] for case in summary["cases"]] == [
        "sequential_baseline",
        "static_conservative",
        "warmup_estimated",
    ]
    assert [call["enable_parallel"] for call in calls] == [False, True, True]
    assert calls[0]["max_parallel_chapters"] == 1
    assert calls[1]["worker_selection_mode"] == "static"
    assert calls[2]["worker_selection_mode"] == "warmup"
    assert [call["parallel_worker_mode"] for call in calls] == ["auto", "auto", "auto"]


def test_artifact_quality_scan_flags_cleaner_handoff_leakage(tmp_path: Path) -> None:
    from scripts.benchmark_nagel_pipeline import scan_artifact_quality

    chapters_dir = tmp_path / "Book" / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "chapter_01_cleaned.md").write_text(
        "# Chapter 1\n\n---\nCleaned text:\nI corrected the formatting for narration.",
        encoding="utf-8",
    )

    quality = scan_artifact_quality(tmp_path)

    assert quality["success"] is False
    issue_types = {issue["type"] for issue in quality["issues"]}
    assert "prompt_delimiter_leakage" in issue_types
    assert "model_commentary_leakage" in issue_types


def test_artifact_quality_scan_allows_first_person_removed_prose(
    tmp_path: Path,
) -> None:
    from scripts.benchmark_nagel_pipeline import scan_artifact_quality

    chapters_dir = tmp_path / "Book" / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "chapter_01_cleaned.md").write_text(
        "# Chapter 1\n\nI removed the bed and examined the corpse.",
        encoding="utf-8",
    )

    quality = scan_artifact_quality(tmp_path)

    assert quality["success"] is True
    assert not any(
        issue["type"] == "model_commentary_leakage"
        for issue in quality["issues"]
    )


def test_artifact_quality_scan_flags_suspiciously_short_cleaned_output(
    tmp_path: Path,
) -> None:
    from scripts.benchmark_nagel_pipeline import scan_artifact_quality

    chapters_dir = tmp_path / "Book" / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "chapter_01_before_cleaning.md").write_text(
        "# Chapter 1\n\n" + ("Original narration sentence. " * 200),
        encoding="utf-8",
    )
    (chapters_dir / "chapter_01_cleaned.md").write_text(
        "# Chapter 1\n\nToo short.",
        encoding="utf-8",
    )

    quality = scan_artifact_quality(tmp_path)

    assert quality["success"] is False
    assert any(issue["type"] == "suspiciously_short_output" for issue in quality["issues"])


def test_artifact_quality_scan_flags_pdf_hyphenation_leakage(tmp_path: Path) -> None:
    from scripts.benchmark_nagel_pipeline import scan_artifact_quality

    chapters_dir = tmp_path / "Book" / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "chapter_01_cleaned.md").write_text(
        "# Chapter 1\n\nThe bat has expe- rience and a sur- rounding world.",
        encoding="utf-8",
    )

    quality = scan_artifact_quality(tmp_path)

    assert quality["success"] is False
    assert any(issue["type"] == "pdf_hyphenation_leakage" for issue in quality["issues"])


def test_artifact_quality_scan_allows_suspended_hyphenation(
    tmp_path: Path,
) -> None:
    from scripts.benchmark_nagel_pipeline import scan_artifact_quality

    chapters_dir = tmp_path / "Book" / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "chapter_01_cleaned.md").write_text(
        "# Chapter 1\n\nThis street contained two- and three-story wooden dwellings.",
        encoding="utf-8",
    )

    quality = scan_artifact_quality(tmp_path)

    assert quality["success"] is True
    assert not any(
        issue["type"] == "pdf_hyphenation_leakage"
        for issue in quality["issues"]
    )


def test_artifact_quality_scan_flags_pdf_page_footnote_leakage(tmp_path: Path) -> None:
    from scripts.benchmark_nagel_pipeline import scan_artifact_quality

    chapters_dir = tmp_path / "Book" / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "chapter_01_cleaned.md").write_text(
        (
            "# Chapter 1\n\n"
            "The point of view is not one accessible only to a single individual, "
            "so the 44I comprehension of such facts is not limited. "
            "It can be apprehended from I The problem I am going to raise can "
            "therefore be posed even if the distinction many different points of view."
        ),
        encoding="utf-8",
    )

    quality = scan_artifact_quality(tmp_path)

    assert quality["success"] is False
    assert any(issue["type"] == "pdf_page_footnote_leakage" for issue in quality["issues"])


def test_artifact_quality_scan_flags_joined_pdf_dash_leakage(tmp_path: Path) -> None:
    from scripts.benchmark_nagel_pipeline import scan_artifact_quality

    chapters_dir = tmp_path / "Book" / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "chapter_01_cleaned.md").write_text(
        "# Chapter 1\n\nThey refer beyond themselvestoward the phenomenal point of view.",
        encoding="utf-8",
    )

    quality = scan_artifact_quality(tmp_path)

    assert quality["success"] is False
    assert any(issue["type"] == "pdf_joined_dash_leakage" for issue in quality["issues"])


def test_artifact_quality_scan_flags_epub_social_boilerplate(tmp_path: Path) -> None:
    from scripts.benchmark_nagel_pipeline import scan_artifact_quality

    chapters_dir = tmp_path / "Book" / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "chapter_01_cleaned.md").write_text(
        (
            "# Follow Penguin\n\n"
            "Follow the Penguin at youtube.com/penguinbooks and "
            "facebook.com/penguinclassics."
        ),
        encoding="utf-8",
    )

    quality = scan_artifact_quality(tmp_path)

    assert quality["success"] is False
    assert any(issue["type"] == "epub_social_boilerplate" for issue in quality["issues"])


def test_artifact_quality_scan_flags_joined_word_leakage(tmp_path: Path) -> None:
    from scripts.benchmark_nagel_pipeline import scan_artifact_quality

    chapters_dir = tmp_path / "Book" / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "chapter_01_cleaned.md").write_text(
        "# Chapter 1\n\nIam tired, Ibegin again, and Iwish the wall would stop moving.",
        encoding="utf-8",
    )

    quality = scan_artifact_quality(tmp_path)

    assert quality["success"] is False
    assert any(issue["type"] == "joined_word_leakage" for issue in quality["issues"])


def test_artifact_quality_scan_flags_garbled_repeated_token_leakage(
    tmp_path: Path,
) -> None:
    from scripts.benchmark_nagel_pipeline import scan_artifact_quality

    chapters_dir = tmp_path / "Book" / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "chapter_01_cleaned.md").write_text(
        "# Chapter 1\n\nFirstopppppmry stayed in the handoff text.",
        encoding="utf-8",
    )

    quality = scan_artifact_quality(tmp_path)

    assert quality["success"] is False
    assert any(
        issue["type"] == "garbled_repeated_token_leakage"
        for issue in quality["issues"]
    )


@pytest.mark.parametrize(
    "token",
    [
        "lltllll",
        "iVttLiiii",
        "Peeeeeeee",
        "bhedmeeeeta",
        "evidelllly",
        "iiiiiiiiii",
    ],
)
def test_artifact_quality_scan_flags_review_repeated_garbage_tokens(
    tmp_path: Path,
    token: str,
) -> None:
    from scripts.benchmark_nagel_pipeline import scan_artifact_quality

    chapters_dir = tmp_path / "Book" / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "chapter_01_cleaned.md").write_text(
        f"# Chapter 1\n\nAthenian Forts {token} Ships stayed in the handoff text.",
        encoding="utf-8",
    )

    quality = scan_artifact_quality(tmp_path)

    assert quality["success"] is False
    assert any(
        issue["type"] == "garbled_repeated_token_leakage"
        for issue in quality["issues"]
    )


def test_artifact_quality_scan_does_not_generalize_review_repeated_i_token(
    tmp_path: Path,
) -> None:
    from scripts.benchmark_nagel_pipeline import scan_artifact_quality

    chapters_dir = tmp_path / "Book" / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "chapter_01_cleaned.md").write_text(
        "# Chapter 1\n\nThe unreviewed token iiiiiiiii is not enough by itself.",
        encoding="utf-8",
    )

    quality = scan_artifact_quality(tmp_path)

    assert quality["success"] is True
    assert not any(
        issue["type"] == "garbled_repeated_token_leakage"
        for issue in quality["issues"]
    )


def test_artifact_quality_scan_allows_review_repeated_speech_effects(
    tmp_path: Path,
) -> None:
    from scripts.benchmark_nagel_pipeline import scan_artifact_quality

    chapters_dir = tmp_path / "Book" / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "chapter_01_cleaned.md").write_text(
        (
            "# Chapter 1\n\n"
            "Mommy said, “ssssssshhhh!” Then he said, “waiiiiit for it.” "
            "The answer was “noooooooo,” followed by “Hoooooo-boy.”"
        ),
        encoding="utf-8",
    )

    quality = scan_artifact_quality(tmp_path)

    assert quality["success"] is True
    assert not any(
        issue["type"] == "garbled_repeated_token_leakage"
        for issue in quality["issues"]
    )


def test_cleaner_gate_benchmark_reports_rule_only_and_llm_needed_chunks(
    tmp_path: Path,
) -> None:
    from scripts import benchmark_nagel_pipeline

    source = tmp_path / "fixture.txt"
    source.write_text(
        (
            "This ordinary narration sentence has no extraction artifacts. "
            "It should be safe for deterministic cleanup only. "
            "The next sentence contains 44I comprehension residue from a PDF page."
        ),
        encoding="utf-8",
    )

    metrics = benchmark_nagel_pipeline.run_benchmark(
        source_path=source,
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        parse_only=True,
        cleaner_gate=True,
        chunk_size=25,
        min_extracted_chars=100,
    )

    assert metrics["mode"] == "cleaner_gate"
    assert metrics["cleaner_gate_rule_only_chunks"] >= 1
    assert metrics["cleaner_gate_llm_needed_chunks"] >= 1
    assert (
        metrics["cleaner_gate_rule_only_chunks"]
        + metrics["cleaner_gate_llm_needed_chunks"]
        == metrics["chunks"]
    )
    assert metrics["clean_seconds_total"] == 0
    assert metrics["tts_seconds_total"] == 0
    assert metrics["cleaner_gate_artifact_issue_count"] >= 1
    assert "pdf_page_footnote_leakage" in metrics["cleaner_gate_artifact_issue_types"]
    assert metrics["cleaner_gate_artifact_issues"]
    assert any(
        issue["type"] == "pdf_page_footnote_leakage"
        for issue in metrics["cleaner_gate_artifact_issues"]
    )
    assert metrics["artifact_quality"]["success"] is True


def test_cleaner_gate_benchmark_reuses_parse_metrics_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from scripts import benchmark_nagel_pipeline

    source = tmp_path / "fixture.txt"
    source.write_text("source content", encoding="utf-8")
    parse_calls = 0

    def fake_parse_document(source_path: Path, config) -> SimpleNamespace:  # noqa: ANN001
        nonlocal parse_calls
        parse_calls += 1
        return SimpleNamespace(
            title=source_path.stem,
            author=None,
            total_pages=None,
            chapters=[
                SimpleNamespace(
                    content=(
                        "This ordinary narration sentence has no extraction artifacts. "
                        "The next sentence contains 44I comprehension residue from a PDF page."
                    )
                )
            ],
        )

    monkeypatch.setattr(benchmark_nagel_pipeline, "_parse_document", fake_parse_document)

    metrics = benchmark_nagel_pipeline.run_benchmark(
        source_path=source,
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        parse_only=True,
        cleaner_gate=True,
        chunk_size=25,
        min_extracted_chars=1,
    )

    assert metrics["success"] is True
    assert parse_calls == 1


def test_build_config_cuda_backend_resolves_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import benchmark_nagel_pipeline
    from modules.tts.runtime_spec import TTSRuntimeSpec

    monkeypatch.setattr(
        TTSRuntimeSpec,
        "mlx_supported_on_current_platform",
        staticmethod(lambda: False),
    )
    monkeypatch.setattr(
        TTSRuntimeSpec,
        "cuda_available_on_current_platform",
        staticmethod(lambda: True),
    )

    config = benchmark_nagel_pipeline._build_config(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        cleaner_backend="cuda",
        cleaner_cuda_device="cuda:0",
        cleaner_cuda_quantization="4bit",
    )

    assert config.cleaner_model_name == "Qwen/Qwen2.5-1.5B-Instruct"
    assert config.tts_model_name == "hexgrad/Kokoro-82M"
    assert config.tts_runtime is not None
    assert config.tts_runtime.provider == "cuda"
