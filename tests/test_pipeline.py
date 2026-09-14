
import pytest
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from pathlib import Path
import numpy as np
import wave
import modules.audio as audio_module
from modules.errors import OCRRequiredPDFError
from modules.ingestion.models import Chapter, Document
from modules.pipeline.book_workspace import initialize_book_workspace, prepare_book_workspace
from modules.pipeline.stages.ingestion import ingest_source, save_intermediate_source
from modules.pipeline.orchestrator import (
    ChapterResult,
    ConversionPipeline,
    PipelineConfig,
)


def test_conversion_cleanup_defers_model_unload_for_stuck_background_cleaner():
    from modules.pipeline.conversion_lifecycle import finish_conversion_run

    cleanup_models = MagicMock()
    cleanup_temp = MagicMock()
    runtime = SimpleNamespace(
        state=SimpleNamespace(
            chapter_aac_executor=None,
            chapter_aac_pending_future=None,
            cancelled=True,
        ),
        set_parallel_execution_active=MagicMock(),
        wait_for_background_workers=MagicMock(return_value=False),
        log_verbose=MagicMock(),
    )

    finish_conversion_run(
        runtime,
        cleanup_models=cleanup_models,
        cleanup_temp=cleanup_temp,
    )

    cleanup_models.assert_not_called()
    cleanup_temp.assert_called_once()
    assert runtime.state.cancelled is False


def test_benchmark_backed_kokoro_defaults():
    from modules.app.config import AppConfig
    from modules.tts.chunker import ChunkConfig
    from modules.tts.engine_config import TTSConfig as LegacyTTSConfig
    from modules.tts.strategies.base import TTSConfig as StrategyTTSConfig

    pipeline_config = PipelineConfig()

    assert pipeline_config.chunk_size == 700
    assert pipeline_config.enable_tts_file_batch_results is False
    assert pipeline_config.enable_chapter_aac_packaging is False
    assert pipeline_config.tts_file_batch_results_auto_threshold_chunks == 64
    assert pipeline_config.keep_tts_engine_warm is True
    assert pipeline_config.tts_idle_timeout_seconds == 300.0
    assert AppConfig().chunk_size == 700
    assert AppConfig().enable_tts_file_batch_results is False
    assert AppConfig().emit_chapter_mp3_artifacts is False
    assert ChunkConfig().max_tokens == 700
    assert StrategyTTSConfig().max_chunk_tokens == 700
    assert LegacyTTSConfig().max_chunk_tokens == 700


def test_app_config_emit_chapter_mp3_artifacts_env_and_serialization(monkeypatch, tmp_path):
    from modules.app.config import AppConfig

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUDIOBOOK_EMIT_CHAPTER_MP3_ARTIFACTS", "yes")

    config = AppConfig()

    assert config.emit_chapter_mp3_artifacts is True
    assert config.to_dict()["emit_chapter_mp3_artifacts"] is True
    assert AppConfig.from_dict(config.to_dict()).emit_chapter_mp3_artifacts is True


def test_conversion_cleanup_keeps_kokoro_hot_until_forced(tmp_path):
    config = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        enable_parallel=False,
        tts_engine="kokoro",
        keep_tts_engine_warm=True,
        tts_idle_timeout_seconds=300.0,
    )
    pipeline = ConversionPipeline(config)
    tts_engine = MagicMock()
    text_cleaner = MagicMock()
    pipeline._tts_engine = tts_engine  # noqa: SLF001
    pipeline._text_cleaner = text_cleaner  # noqa: SLF001

    pipeline.stages.cleanup_models()  # noqa: SLF001

    tts_engine.unload.assert_not_called()
    assert pipeline._tts_engine is tts_engine  # noqa: SLF001
    text_cleaner.unload.assert_called_once_with(keep_in_cache=True)
    assert pipeline._text_cleaner is None  # noqa: SLF001

    pipeline.stages.cleanup_models(force=True)  # noqa: SLF001

    tts_engine.unload.assert_called_once_with(keep_in_cache=False)
    assert pipeline._tts_engine is None  # noqa: SLF001


def test_zero_idle_timeout_unloads_retained_kokoro_immediately(tmp_path):
    config = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        enable_parallel=False,
        tts_engine="kokoro",
        keep_tts_engine_warm=True,
        tts_idle_timeout_seconds=0.0,
    )
    pipeline = ConversionPipeline(config)
    tts_engine = MagicMock()
    pipeline._tts_engine = tts_engine  # noqa: SLF001

    pipeline.stages.cleanup_models()  # noqa: SLF001

    tts_engine.unload.assert_called_once_with(keep_in_cache=False)
    assert pipeline._tts_engine is None  # noqa: SLF001


def test_finish_conversion_run_shuts_down_chapter_aac_executor_on_cleanup_error():
    from modules.pipeline.conversion_lifecycle import finish_conversion_run

    executor = MagicMock()
    runtime = SimpleNamespace(
        state=SimpleNamespace(chapter_aac_executor=executor, cancelled=True),
        set_parallel_execution_active=MagicMock(),
    )

    def cleanup_temp():
        raise RuntimeError("cleanup failed")

    with pytest.raises(RuntimeError, match="cleanup failed"):
        finish_conversion_run(
            runtime,
            cleanup_models=MagicMock(),
            cleanup_temp=cleanup_temp,
        )

    executor.shutdown.assert_called_once_with(wait=True)
    assert runtime.state.chapter_aac_executor is None
    assert runtime.state.cancelled is False


def test_warmup_reuses_retained_loaded_kokoro_without_probe(tmp_path):
    config = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        enable_parallel=False,
        tts_engine="kokoro",
    )
    pipeline = ConversionPipeline(config)
    tts_engine = MagicMock()
    tts_engine.is_loaded = True
    pipeline._tts_engine = tts_engine  # noqa: SLF001

    with patch("modules.tts.cleaner.TextCleaner") as mock_cleaner:
        mock_cleaner.return_value = MagicMock()
        pipeline.stages.warmup_models()  # noqa: SLF001

    tts_engine.load.assert_not_called()
    tts_engine.warmup.assert_not_called()
    pipeline.stages.cleanup_models(force=True)  # noqa: SLF001


def test_convert_pre_run_cancellation_finishes_lifecycle(tmp_path):
    config = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        enable_parallel=False,
    )
    pipeline = ConversionPipeline(config)
    cleanup_calls = []
    pipeline.stages.cleanup_models = lambda force=False: cleanup_calls.append(force)
    pipeline.cancel()

    result = pipeline.convert(tmp_path / "book.md")

    assert result.success is False
    assert result.error == "Cancelled"
    assert pipeline._cancelled is False  # noqa: SLF001
    assert cleanup_calls == [False]


class TestConversionPipeline:
    @pytest.fixture
    def mock_components(self):
        """Mock all external components used by pipeline."""
        with patch("modules.ingestion.pdf_parser.PDFParser") as mock_pdf, \
             patch("modules.ingestion.epub_parser.EPUBParser"), \
             patch("modules.tts.cleaner.TextCleaner") as mock_cleaner, \
             patch("modules.tts.chunker.TextChunker") as mock_chunker, \
             patch("modules.tts.factory.TTSEngineFactory.create") as mock_tts_factory, \
             patch("modules.tts.engine.TTSEngine") as mock_tts, \
             patch("modules.audio.encoder.AudioEncoder") as mock_enc, \
             patch("modules.audio.packager.M4BPackager") as mock_packager:
             
            # Setup returns
            # Mock Document object
            mock_doc = MagicMock()
            mock_doc.title = "Test Book"
            mock_doc.author = "Test Author"
            mock_doc.chapters = [
                MagicMock(title="Chapter 1", content="Text 1", number=1)
            ]
            mock_pdf.return_value.parse.return_value = mock_doc
            
            # TextCleaner now uses persistent instance with load/clean/unload
            mock_cleaner_instance = MagicMock()
            mock_cleaner_instance.clean.side_effect = lambda x: x
            mock_cleaner.return_value = mock_cleaner_instance
            
            # TextChunker is instantiated, so we mock the instance method
            mock_chunker.return_value.chunk.return_value = ["chunk1"]
            mock_chunker.chunk_text.return_value = ["chunk1"] # Fallback
            
            # Mock batch synthesis - returns BatchResult-like objects
            from modules.tts.engine import BatchResult
            import numpy as np
            
            def mock_synthesize_batch(items, progress_callback=None):
                """Mock batch synthesis that returns audio arrays."""
                results = []
                for i, item in enumerate(items):
                    result = BatchResult(
                        audio=np.array([0.1, 0.2, 0.3], dtype=np.float32),
                        index=i,
                        error=None,
                        duration_ms=100
                    )
                    results.append(result)
                    if progress_callback:
                        progress_callback(i + 1, len(items))
                return results
            
            mock_tts_instance = MagicMock()
            mock_tts_instance.synthesize_batch.side_effect = mock_synthesize_batch
            mock_tts_instance.synthesize.return_value = np.array([0.1, 0.2, 0.3], dtype=np.float32)
            mock_tts_instance.sample_rate = 24000
            mock_tts.return_value = mock_tts_instance
            mock_tts_factory.return_value = mock_tts_instance
            
            mock_enc.return_value.get_duration.return_value = 10.0 # 10 seconds
            
            # Make encoder.wav_to_mp3 actually create the file
            def wav_to_mp3_side_effect(wav_path, output_path, bitrate="128k"):
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"fake mp3 content")
            mock_enc.return_value.wav_to_mp3.side_effect = wav_to_mp3_side_effect

            def array_to_mp3_side_effect(audio, output_path, sample_rate=24000, bitrate="128k"):
                _ = audio, sample_rate, bitrate
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"fake mp3 content")
            mock_enc.return_value.array_to_mp3.side_effect = array_to_mp3_side_effect

            def create_m4b_side_effect(*args, **kwargs):
                _ = args
                output_path = Path(kwargs["output_path"])
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"fake m4b content")
                return output_path
            mock_packager.return_value.create_m4b.side_effect = create_m4b_side_effect
            
            yield {
                "pdf": mock_pdf,
                "cleaner": mock_cleaner,
                "chunker": mock_chunker,
                "tts": mock_tts,
                "tts_factory": mock_tts_factory,
                "encoder": mock_enc,
                "packager": mock_packager,
            }

    def test_pipeline_execution_success(self, mock_components, tmp_path):
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp"
        )
        pipeline = ConversionPipeline(config)
        
        # Create a dummy test file
        test_file = tmp_path / "test.pdf"
        test_file.write_text("dummy pdf content")
        
        result = pipeline.convert(test_file)
        
        if not result.success:
            print(f"Pipeline failed with error: {result.error}")
            
        assert result.success
        assert result.title == "Test Book"
        assert len(result.chapters) == 1

    def test_convert_filters_non_prose_sections_before_cleaning_tts_and_output(
        self, mock_components, tmp_path
    ):
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
        )
        pipeline = ConversionPipeline(config)
        test_file = tmp_path / "test.pdf"
        test_file.write_text("dummy pdf content")
        mock_components["pdf"].return_value.parse.return_value = Document(
            title="Mixed Book",
            author="Test Author",
            chapters=[
                Chapter(
                    number=1,
                    title="Contents",
                    content="\n".join(
                        [
                            "Foreword ................................ ix",
                            "Chapter 1 The Signal .................... 1",
                            "Chapter 2 The Reply ..................... 23",
                            "Epilogue ................................ 97",
                        ]
                    ),
                ),
                Chapter(
                    number=2,
                    title="Chapter 1",
                    content="The first narrated paragraph was calm and clear. " * 10,
                ),
                Chapter(
                    number=3,
                    title="Bibliography",
                    content="\n".join(
                        [
                            "Adams, J. (2018). The long memory of cities. Oxford University Press.",
                            "Baker, L. (2020). Archive fever revisited. Journal of History, 14(2), 33-49.",
                            "Davis, P. (2016). Reading by lantern light. North River Press.",
                            "Evans, R. (2021). Listening rooms and public speech. https://example.org/listening",
                        ]
                    ),
                ),
                Chapter(
                    number=4,
                    title="Chapter 2",
                    content="The second narrated paragraph belonged in the audiobook. " * 10,
                ),
                Chapter(
                    number=5,
                    title="Index",
                    content="\n".join(
                        [
                            "Anderson, Mary 12, 44, 109",
                            "Barnes, Thomas 15, 61",
                            "Carter, Evelyn 18, 72, 119",
                            "Dawson, River 31, 88",
                            "Ellis Island 41, 42",
                            "Franklin, Anna 53, 100",
                        ]
                    ),
                ),
            ],
        )

        result = pipeline.convert(test_file)

        assert result.success
        assert [chapter.chapter_title for chapter in result.chapters] == [
            "Chapter 1",
            "Chapter 2",
        ]
        assert [chapter.chapter_number for chapter in result.chapters] == [1, 2]
        chunk_inputs = [
            call.args[0] for call in mock_components["chunker"].return_value.chunk.call_args_list
        ]
        assert [text.strip() for text in chunk_inputs] == [
            ("The first narrated paragraph was calm and clear. " * 10).strip(),
            ("The second narrated paragraph belonged in the audiobook. " * 10).strip(),
        ]
        _, create_m4b_kwargs = mock_components["packager"].return_value.create_m4b.call_args
        assert create_m4b_kwargs["chapter_names"] == ["Chapter 1", "Chapter 2"]

    def test_pipeline_reports_ocr_required_pdf_before_cleaning(self, mock_components, tmp_path):
        config = PipelineConfig(output_dir=tmp_path / "output", temp_dir=tmp_path / "temp")
        pipeline = ConversionPipeline(config)
        source = tmp_path / "scanned.pdf"
        source.write_bytes(b"%PDF-1.4\n" + b"x" * 128 + b"\n%%EOF")
        mock_components["pdf"].return_value.parse.side_effect = OCRRequiredPDFError(
            file_path=source,
            confidence=0.96,
            sampled_pages=2,
            text_chars=0,
        )

        result = pipeline.convert(source)

        assert result.success is False
        assert "OCR required" in result.error
        assert "not text-extractable" in result.error
        mock_components["cleaner"].assert_not_called()

    def test_pipeline_execution_success_document_audio_outputs_m4a(self, mock_components, tmp_path):
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            conversion_mode="document_audio",
        )
        pipeline = ConversionPipeline(config)

        test_file = tmp_path / "test.pdf"
        test_file.write_text("dummy pdf content")

        assert hasattr(audio_module, "DocumentAudioPackager")

        expected_output = tmp_path / "output" / "Test Book" / "Test Book.m4a"

        with patch("modules.audio.document_packager.DocumentAudioPackager") as packager_cls:
            def create_m4a_side_effect(*args, **kwargs):
                _ = args
                output_path = Path(kwargs["output_path"])
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"fake m4a content")
                return output_path

            packager_cls.return_value.create_m4a.side_effect = create_m4a_side_effect

            result = pipeline.convert(test_file)

        assert result.success
        assert result.output_path == expected_output
        assert result.output_path.suffix == ".m4a"
        
    def test_cancellation(self, mock_components, tmp_path):
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp"
        )
        pipeline = ConversionPipeline(config)
        
        # Create a dummy test file
        test_file = tmp_path / "test.pdf"
        test_file.write_text("dummy pdf content")
        
        # We need to trigger cancellation DURING processing
        # Use chunker side effect to cancel
        def cancel_side_effect(*args, **kwargs):
            pipeline.cancel()
            return ["chunk1"]
            
        mock_components["chunker"].return_value.chunk.side_effect = cancel_side_effect
        mock_components["chunker"].chunk_text.side_effect = cancel_side_effect
        
        result = pipeline.convert(test_file)
            
        assert not result.success
        assert "Cancelled" in str(result.error)

    def test_cancellation_after_ingestion_returns_cancelled_result(self, tmp_path):
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
        )
        pipeline = ConversionPipeline(config)
        test_file = tmp_path / "test.txt"
        test_file.write_text("dummy text")

        def ingest_and_cancel(_source_path):
            pipeline.cancel()
            return Document(
                title="Cancelled Book",
                author="Author",
                chapters=[Chapter(number=1, title="One", content="Text")],
            )

        with patch.object(pipeline.stages, "ingest", side_effect=ingest_and_cancel):
            result = pipeline.convert(test_file)

        assert not result.success
        assert result.title == ""
        assert result.author is None
        assert result.error == "Cancelled"

    def test_persistence_files_created(self, mock_components, tmp_path):
        """Verify that source.md and chapter_XX_cleaned.md files are created."""
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp"
        )
        pipeline = ConversionPipeline(config)
        
        # Create a dummy test file
        test_file = tmp_path / "test.pdf"
        test_file.write_text("dummy pdf content")
        
        result = pipeline.convert(test_file)
        
        assert result.success
        
        # Check that source.md was created
        book_output_dir = tmp_path / "output" / "Test Book"
        source_md = book_output_dir / "source.md"
        assert source_md.exists(), f"source.md should be created at {source_md}"
        
        # Check that chapter cleaning artifacts were created
        chapters_dir = book_output_dir / "chapters"
        chapter_cleaned = chapters_dir / "chapter_01_cleaned.md"
        chapter_before = chapters_dir / "chapter_01_before_cleaning.md"
        assert chapter_before.exists(), f"chapter_01_before_cleaning.md should be created at {chapter_before}"
        assert chapter_cleaned.exists() or chapter_before.exists(), (
            f"Expected at least one chapter artifact at {chapters_dir}"
        )
        
        # Verify content of source.md
        source_content = source_md.read_text(encoding="utf-8")
        assert "# Test Book" in source_content
        
        # Verify content of chapter artifact file
        chapter_content_path = chapter_cleaned if chapter_cleaned.exists() else chapter_before
        chapter_content = chapter_content_path.read_text(encoding="utf-8")
        assert "# Chapter 1" in chapter_content
        assert "chunk1" in chapter_content or "Text 1" in chapter_content

    def test_save_intermediate_source_preserves_raw_markdown_without_duplicate_title(self, tmp_path):
        pipeline = MagicMock()
        pipeline.config.save_cleaning_debug = True
        pipeline._log_verbose = MagicMock()
        document = Document(
            title="Guide",
            chapters=[Chapter(number=1, title="Guide", content="Intro paragraph.")],
            raw_markdown="# Guide\n\nIntro paragraph.\n",
        )
        output_dir = tmp_path / "Guide"
        output_dir.mkdir(parents=True, exist_ok=True)

        save_intermediate_source(pipeline, document, output_dir)

        assert (output_dir / "source.md").read_text(encoding="utf-8") == "# Guide\n\nIntro paragraph.\n"

    def test_book_workspace_initialization_creates_output_tree_and_uses_pipeline_persistence_hook(
        self, tmp_path
    ):
        output_root = tmp_path / "output"
        persisted = {}
        pipeline = MagicMock()
        pipeline.config.output_dir = output_root
        pipeline.stages.save_intermediate_source.side_effect = (  # noqa: SLF001 - seam verification
            lambda document, output_dir: persisted.update(
                {
                    "document": document,
                    "output_dir": output_dir,
                }
            )
        )
        document = MagicMock()

        workspace = initialize_book_workspace(
            pipeline,
            document,
            "Bad:/Title?",
        )

        assert workspace.output_dir == output_root / "Bad__Title_"
        assert workspace.chapters_dir == workspace.output_dir / "chapters"
        assert workspace.output_dir.exists()
        assert workspace.chapters_dir.exists()
        assert persisted == {
            "document": document,
            "output_dir": workspace.output_dir,
        }

    def test_book_workspace_repeated_same_title_creates_isolated_output_tree(
        self, tmp_path
    ):
        output_root = tmp_path / "output"
        pipeline = MagicMock()
        pipeline.config.output_dir = output_root
        pipeline.stages.save_intermediate_source.side_effect = (  # noqa: SLF001
            lambda document, output_dir: (output_dir / "source.md").write_text(
                document.raw_markdown,
                encoding="utf-8",
            )
        )
        first_document = MagicMock(raw_markdown="# Same Title\n\nFirst source.\n")
        second_document = MagicMock(raw_markdown="# Same Title\n\nSecond source.\n")

        first_workspace = initialize_book_workspace(
            pipeline,
            first_document,
            "Same Title",
        )
        first_source = first_workspace.output_dir / "source.md"
        first_chapter = first_workspace.chapters_dir / "chapter_01.mp3"
        first_package = first_workspace.output_dir / "Same Title.m4b"
        first_chapter.write_bytes(b"first chapter audio")
        first_package.write_bytes(b"first package")
        first_source_before = first_source.read_text(encoding="utf-8")
        first_chapter_before = first_chapter.read_bytes()
        first_package_before = first_package.read_bytes()

        second_workspace = initialize_book_workspace(
            pipeline,
            second_document,
            "Same Title",
        )

        assert first_workspace.output_dir == output_root / "Same Title"
        assert second_workspace.output_dir.parent == output_root
        assert second_workspace.output_dir != first_workspace.output_dir
        assert second_workspace.chapters_dir != first_workspace.chapters_dir
        assert first_source.exists()
        assert first_chapter.exists()
        assert first_package.exists()
        assert first_source.read_text(encoding="utf-8") == first_source_before
        assert first_chapter.read_bytes() == first_chapter_before
        assert first_package.read_bytes() == first_package_before

    def test_book_workspace_truncates_long_title_to_safe_path_segment(self, tmp_path):
        output_root = tmp_path / "output"
        long_title = "A" * 300

        workspace = prepare_book_workspace(output_root, long_title)
        repeated_workspace = prepare_book_workspace(output_root, long_title)

        assert workspace.book_title == long_title
        assert workspace.output_dir.parent == output_root
        assert workspace.output_dir.exists()
        assert workspace.chapters_dir.exists()
        assert len(workspace.output_dir.name.encode("utf-8")) <= 120
        assert repeated_workspace.output_dir.parent == output_root
        assert repeated_workspace.output_dir != workspace.output_dir
        assert len(repeated_workspace.output_dir.name.encode("utf-8")) <= 120

    def test_book_workspace_truncates_multibyte_title_by_encoded_path_length(self, tmp_path):
        output_root = tmp_path / "output"
        long_title = "章节" * 100

        workspace = prepare_book_workspace(output_root, long_title)

        assert workspace.output_dir.parent == output_root
        assert workspace.output_dir.exists()
        assert len(workspace.output_dir.name) < len(long_title)
        assert len(workspace.output_dir.name.encode("utf-8")) <= 120

    def test_book_workspace_uses_fallback_for_blank_title(self, tmp_path):
        output_root = tmp_path / "output"

        workspace = prepare_book_workspace(output_root, "   ")
        repeated_workspace = prepare_book_workspace(output_root, "")

        assert workspace.output_dir == output_root / "Untitled"
        assert repeated_workspace.output_dir == output_root / "Untitled (2)"
        assert workspace.output_dir.exists()
        assert repeated_workspace.output_dir.exists()

    def test_persistence_files_skipped_when_debug_disabled(self, mock_components, tmp_path):
        """Verify large debug artifacts are skipped when save_cleaning_debug is disabled."""
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            save_cleaning_debug=False,
        )
        pipeline = ConversionPipeline(config)

        test_file = tmp_path / "test.pdf"
        test_file.write_text("dummy pdf content")

        result = pipeline.convert(test_file)

        assert result.success

        book_output_dir = tmp_path / "output" / "Test Book"
        chapters_dir = book_output_dir / "chapters"
        assert not (book_output_dir / "source.md").exists()
        assert not (chapters_dir / "chapter_01_before_cleaning.md").exists()
        assert not (chapters_dir / "chapter_01_cleaned.md").exists()

    def test_chunk_overlap_is_not_tied_to_global_pipelining(self, mock_components, tmp_path):
        """Global pipelining should not force overlap mode for multi-chunk chapters."""
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_pipelining=True,
            enable_chunk_overlap=False,
        )
        pipeline = ConversionPipeline(config)

        chapter = MagicMock(number=1, title="Chapter 1", content="Text")
        wav_path = tmp_path / "chapter.wav"
        with wave.open(str(wav_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes(np.zeros(24000, dtype=np.int16).tobytes())
        mp3_path = tmp_path / "chapter.mp3"

        processor = pipeline.stages.chapter_processor  # noqa: SLF001 - stage collaborator contract
        with patch.object(type(processor.runtime), "synthesize_speech", return_value=np.ones(3, dtype=np.float32)), \
             patch.object(processor, "ensure_chapter_tts_engine"), \
             patch.object(processor, "chunk_chapter_content", return_value=["chunk1", "chunk2"]), \
             patch.object(processor, "clean_chapter_content", return_value=(["clean1", "clean2"], False)), \
             patch.object(processor, "synthesize_chapter_chunks", return_value=wav_path), \
             patch.object(processor, "clean_and_synthesize_with_overlap", return_value=(wav_path, False)), \
             patch.object(processor, "encode_chapter_audio", return_value=(mp3_path, 1000)):
            result = pipeline.stages.process_chapter(
                chapter=chapter,
                chapter_idx=0,
                total_chapters=1,
                output_dir=tmp_path,
            )

        assert result.error is None

    def test_ensure_chapter_tts_engine_uses_factory_selected_strategy(self, tmp_path):
        config = PipelineConfig(
            tts_engine="kokoro",
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
        )
        pipeline = ConversionPipeline(config)
        fake_strategy = MagicMock()
        fake_strategy.is_loaded = True

        with patch("modules.tts.factory.TTSEngineFactory.create", return_value=fake_strategy) as mock_create, \
             patch("modules.tts.engine.TTSEngine", side_effect=AssertionError("TTSEngine should not be constructed")):
            returned = pipeline.stages.chapter_processor.ensure_chapter_tts_engine()  # noqa: SLF001

        assert returned is fake_strategy
        mock_create.assert_called_once()

    def test_process_chapter_skips_mp3_encode_when_wav_master_is_packaged_by_default(
        self, mock_components, tmp_path
    ):
        """In-memory chapter audio should be packaged from WAV without redundant MP3 encode."""
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_chunk_overlap=False,
        )
        pipeline = ConversionPipeline(config)

        chapter = MagicMock(number=1, title="Chapter 1", content="Text")
        in_memory_audio = np.array([0.1, 0.2, 0.3], dtype=np.float32)

        processor = pipeline.stages.chapter_processor  # noqa: SLF001 - stage collaborator contract
        with patch.object(type(processor.runtime), "synthesize_speech", return_value=np.ones(3, dtype=np.float32)), \
             patch.object(processor, "ensure_chapter_tts_engine"), \
             patch.object(processor, "chunk_chapter_content", return_value=["chunk1"]), \
             patch.object(processor, "clean_chapter_content", return_value=(["clean1"], False)), \
             patch.object(processor, "synthesize_chapter_chunks", return_value=in_memory_audio), \
             patch.object(
                 processor,
                 "encode_chapter_audio",
                 side_effect=AssertionError("MP3 encode should be skipped"),
             ):
            result = pipeline.stages.process_chapter(
                chapter=chapter,
                chapter_idx=0,
                total_chapters=1,
                output_dir=tmp_path,
            )

        assert result.error is None
        assert result.wav_path == config.temp_dir / "chapter_01_narrated.wav"
        assert result.wav_path.exists()
        assert result.duration_ms == 6000
        assert result.mp3_path is None

    def test_process_chapter_can_still_emit_mp3_artifacts_when_enabled(
        self, mock_components, tmp_path
    ):
        """Chapter MP3 artifacts remain available when explicitly requested."""
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_chunk_overlap=False,
            emit_chapter_mp3_artifacts=True,
        )
        pipeline = ConversionPipeline(config)

        chapter = MagicMock(number=1, title="Chapter 1", content="Text")
        in_memory_audio = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        mp3_path = tmp_path / "chapter.mp3"

        processor = pipeline.stages.chapter_processor  # noqa: SLF001 - stage collaborator contract
        with patch.object(type(processor.runtime), "synthesize_speech", return_value=np.ones(3, dtype=np.float32)), \
             patch.object(processor, "ensure_chapter_tts_engine"), \
             patch.object(processor, "chunk_chapter_content", return_value=["chunk1"]), \
             patch.object(processor, "clean_chapter_content", return_value=(["clean1"], False)), \
             patch.object(processor, "synthesize_chapter_chunks", return_value=in_memory_audio), \
             patch.object(processor, "encode_chapter_audio", return_value=(mp3_path, 1000)):
            result = pipeline.stages.process_chapter(
                chapter=chapter,
                chapter_idx=0,
                total_chapters=1,
                output_dir=tmp_path,
            )

        assert result.error is None
        assert result.wav_path == config.temp_dir / "chapter_01_narrated.wav"
        assert result.mp3_path == mp3_path
        assert result.duration_ms == 1000

    def test_cleanup_temp_removes_known_current_run_artifacts_without_scanning_temp_root(
        self, tmp_path, monkeypatch
    ):
        """Normal cleanup should avoid scanning stale temp-root entries."""
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
        )
        pipeline = ConversionPipeline(config)

        current_master = config.temp_dir / "chapter_masters" / "chapter_01.wav"
        current_master.parent.mkdir(parents=True, exist_ok=True)
        current_master.write_bytes(b"wav")
        stale_dir = config.temp_dir / "ch_999"
        stale_dir.mkdir()
        (stale_dir / "chunk.wav").write_bytes(b"old")
        stale_wav = config.temp_dir / "chapter_99.wav"
        stale_wav.write_bytes(b"old")
        keep_file = config.temp_dir / "keep.txt"
        keep_file.write_text("keep")

        pipeline._track_temp_cleanup_path(current_master)  # noqa: SLF001

        original_iterdir = Path.iterdir

        def fail_on_temp_root_scan(path):
            if path == config.temp_dir:
                raise AssertionError("temp root should not be scanned")
            return original_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", fail_on_temp_root_scan)

        pipeline._cleanup_temp()  # noqa: SLF001

        assert not current_master.parent.exists()
        assert stale_dir.exists()
        assert stale_wav.exists()
        assert keep_file.exists()

    def test_cleanup_temp_legacy_scan_fallback_preserves_existing_behavior(
        self, tmp_path
    ):
        """Direct cleanup calls with no tracked paths keep the legacy temp scan."""
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
        )
        pipeline = ConversionPipeline(config)

        old_chunk_dir = config.temp_dir / "ch_001"
        old_chunk_dir.mkdir(parents=True)
        (old_chunk_dir / "chunk.wav").write_bytes(b"old")
        old_master_dir = config.temp_dir / "chapter_masters"
        old_master_dir.mkdir()
        (old_master_dir / "chapter_01.wav").write_bytes(b"old")
        old_wav = config.temp_dir / "chapter_01.wav"
        old_wav.write_bytes(b"old")
        keep_file = config.temp_dir / "keep.txt"
        keep_file.write_text("keep")

        pipeline._cleanup_temp()  # noqa: SLF001

        assert not old_chunk_dir.exists()
        assert not old_master_dir.exists()
        assert not old_wav.exists()
        assert keep_file.exists()

    def test_encode_chapter_audio_prefers_array_encoder(self, mock_components, tmp_path):
        """Encoding stage should use array_to_mp3 when chapter audio is in-memory."""
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            normalize_volume=False,
        )
        pipeline = ConversionPipeline(config)
        chapter = MagicMock(number=1, title="Chapter 1")
        audio = np.zeros(24000, dtype=np.float32)

        fake_encoder = MagicMock()
        fake_encoder.get_duration.return_value = 1.0

        def _array_to_mp3(arr, output_path, sample_rate, bitrate):
            _ = arr, sample_rate, bitrate
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"mp3")

        fake_encoder.array_to_mp3.side_effect = _array_to_mp3

        with patch("modules.audio.encoder.AudioEncoder", return_value=fake_encoder):
            mp3_path, duration_ms = pipeline.stages.chapter_processor.encode_chapter_audio(  # noqa: SLF001
                chapter=chapter,
                chapter_idx=0,
                total_chapters=1,
                output_dir=tmp_path,
                chapter_wav=audio,
            )

        assert mp3_path.exists()
        assert duration_ms == 1000
        assert fake_encoder.array_to_mp3.call_count == 1
        assert fake_encoder.wav_to_mp3.call_count == 0

    def test_encode_chapter_audio_falls_back_to_wav_file_on_array_failure(self, mock_components, tmp_path):
        """Encoding stage should gracefully fall back to wav_to_mp3 when array path fails."""
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            normalize_volume=False,
        )
        pipeline = ConversionPipeline(config)
        chapter = MagicMock(number=1, title="Chapter 1")
        audio = np.zeros(24000, dtype=np.float32)

        fake_encoder = MagicMock()
        fake_encoder.get_duration.return_value = 1.0
        fake_encoder.array_to_mp3.side_effect = RuntimeError("simulated array encode failure")

        def _wav_to_mp3(input_path, output_path, bitrate="128k"):
            _ = bitrate
            assert Path(input_path).exists()
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"mp3")

        fake_encoder.wav_to_mp3.side_effect = _wav_to_mp3

        with patch("modules.audio.encoder.AudioEncoder", return_value=fake_encoder):
            mp3_path, duration_ms = pipeline.stages.chapter_processor.encode_chapter_audio(  # noqa: SLF001
                chapter=chapter,
                chapter_idx=0,
                total_chapters=1,
                output_dir=tmp_path,
                chapter_wav=audio,
            )

        assert mp3_path.exists()
        assert duration_ms == 1000
        assert fake_encoder.array_to_mp3.call_count == 1
        assert fake_encoder.wav_to_mp3.call_count == 1

    def test_synthesize_chapter_chunks_streams_large_audio_without_concat(self, mock_components, tmp_path):
        """Large chapters should stream directly to chapter WAV without temp concatenation."""
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_tts_file_batch_results=True,
        )
        pipeline = ConversionPipeline(config)
        pipeline.config.max_in_memory_chapter_samples = 1
        pipeline._tts_engine = mock_components["tts"].return_value

        chapter = MagicMock(number=1, title="Chapter 1")

        chapter_wav = pipeline.stages.chapter_processor.synthesize_chapter_chunks(  # noqa: SLF001
            chapter=chapter,
            chapter_idx=0,
            total_chapters=1,
            chunks=["chunk one", "chunk two"],
        )

        assert isinstance(chapter_wav, Path)
        with wave.open(str(chapter_wav), "rb") as wav_file:
            assert wav_file.getnframes() == 6
            assert wav_file.getframerate() == 24000

    def test_synthesize_chapter_chunks_consumes_incremental_tts_results(self, mock_components, tmp_path):
        """Iterator-capable engines should stream and clear each result before requesting the next."""
        from modules.tts.engine import BatchResult

        class IncrementalTTSEngine:
            supports_incremental_batch_results = True
            is_loaded = True
            sample_rate = 24000

            def __init__(self):
                self.generated_results = []
                self.synthesize_batch_called = False

            def synthesize_batch(self, items, progress_callback=None):  # noqa: ANN001
                _ = items, progress_callback
                self.synthesize_batch_called = True
                raise AssertionError("synthesize_batch should not be used when an iterator exists")

            def iter_synthesize_batch(self, items, progress_callback=None):  # noqa: ANN001
                total = len(items)
                for completed, item in enumerate(items, start=1):
                    if self.generated_results:
                        assert self.generated_results[-1].audio is None

                    result = BatchResult(
                        audio=np.full(4, item.index + 1, dtype=np.float32),
                        index=item.index,
                        error=None,
                        duration_ms=100,
                    )
                    self.generated_results.append(result)
                    if progress_callback is not None:
                        progress_callback(completed, total)
                    yield result

                assert self.generated_results[-1].audio is None

        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
        )
        pipeline = ConversionPipeline(config)
        pipeline.config.max_in_memory_chapter_samples = 1
        fake_tts = IncrementalTTSEngine()
        pipeline._tts_engine = fake_tts
        chapter = MagicMock(number=1, title="Chapter 1")

        chapter_wav = pipeline.stages.chapter_processor.synthesize_chapter_chunks(  # noqa: SLF001
            chapter=chapter,
            chapter_idx=0,
            total_chapters=1,
            chunks=["chunk one", "chunk two", "chunk three"],
        )

        assert isinstance(chapter_wav, Path)
        assert fake_tts.synthesize_batch_called is False
        assert [result.audio for result in fake_tts.generated_results] == [None, None, None]
        with wave.open(str(chapter_wav), "rb") as wav_file:
            assert wav_file.getnframes() == 12
            assert wav_file.getframerate() == 24000

    def test_consume_batch_result_audio_clears_file_backed_audio_payload(
        self,
        tmp_path,
    ):
        import wave

        from modules.pipeline.stages.synthesis import (
            ChapterAudioAccumulator,
            _consume_batch_result_audio,
        )
        from modules.tts.engine import BatchResult

        chunk_path = tmp_path / "chunk.wav"
        with wave.open(str(chunk_path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(24000)
            writer.writeframes(b"\0\0" * 4)

        accumulator = ChapterAudioAccumulator(
            output_path=tmp_path / "chapter.wav",
            sample_rate=24000,
            max_in_memory_samples=1,
        )
        result = BatchResult(
            audio=np.array([0.1, 0.2], dtype=np.float32),
            audio_path=chunk_path,
            index=0,
        )

        _consume_batch_result_audio(accumulator, result)
        accumulator.close()

        assert result.audio_path is None
        assert result.audio is None
        assert not chunk_path.exists()

    def test_synthesize_chapter_chunks_orders_incremental_audio_by_chunk_index(
        self,
        mock_components,
        tmp_path,
    ):
        """Incremental TTS results may arrive out of order; final audio follows source chunks."""
        from modules.tts.engine import BatchResult

        class OutOfOrderIncrementalTTSEngine:
            supports_incremental_batch_results = True
            is_loaded = True
            sample_rate = 24000

            def synthesize_batch(self, items, progress_callback=None):  # noqa: ANN001
                _ = items, progress_callback
                raise AssertionError("synthesize_batch should not be used when an iterator exists")

            def iter_synthesize_batch(self, items, progress_callback=None):  # noqa: ANN001
                _ = progress_callback
                assert [item.index for item in items] == [0, 1]
                yield BatchResult(
                    audio=np.array([2.0, 2.0], dtype=np.float32),
                    index=1,
                    error=None,
                    duration_ms=100,
                )
                yield BatchResult(
                    audio=np.array([1.0, 1.0], dtype=np.float32),
                    index=0,
                    error=None,
                    duration_ms=100,
                )

        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
        )
        pipeline = ConversionPipeline(config)
        pipeline._tts_engine = OutOfOrderIncrementalTTSEngine()
        chapter = MagicMock(number=1, title="Chapter 1")

        chapter_audio = pipeline.stages.chapter_processor.synthesize_chapter_chunks(  # noqa: SLF001
            chapter=chapter,
            chapter_idx=0,
            total_chapters=1,
            chunks=["chunk one", "chunk two"],
        )

        assert isinstance(chapter_audio, np.ndarray)
        assert chapter_audio.tolist() == [1.0, 1.0, 2.0, 2.0]

    def test_synthesize_chapter_chunks_requires_incremental_opt_in(
        self,
        mock_components,
        tmp_path,
    ):
        """Engines with custom list batching must opt in before iterator consumption is used."""
        from modules.tts.engine import BatchResult

        class CustomBatchEngine:
            is_loaded = True
            sample_rate = 24000

            def __init__(self):
                self.synthesize_batch_called = False

            def synthesize_batch(self, items, progress_callback=None):  # noqa: ANN001
                self.synthesize_batch_called = True
                results = []
                for completed, item in enumerate(items, start=1):
                    if progress_callback is not None:
                        progress_callback(completed, len(items))
                    results.append(
                        BatchResult(
                            audio=np.full(2, item.index + 1, dtype=np.float32),
                            index=item.index,
                            error=None,
                            duration_ms=100,
                        )
                    )
                return results

            def iter_synthesize_batch(self, items, progress_callback=None):  # noqa: ANN001
                _ = items, progress_callback
                raise AssertionError("iterator path requires explicit opt-in")

        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
        )
        pipeline = ConversionPipeline(config)
        fake_tts = CustomBatchEngine()
        pipeline._tts_engine = fake_tts
        chapter = MagicMock(number=1, title="Chapter 1")

        chapter_audio = pipeline.stages.chapter_processor.synthesize_chapter_chunks(  # noqa: SLF001
            chapter=chapter,
            chapter_idx=0,
            total_chapters=1,
            chunks=["chunk one", "chunk two"],
        )

        assert fake_tts.synthesize_batch_called is True
        assert isinstance(chapter_audio, np.ndarray)
        assert chapter_audio.tolist() == [1.0, 1.0, 2.0, 2.0]

    def test_synthesize_chapter_chunks_incremental_all_errors_retries_serially(
        self,
        mock_components,
        tmp_path,
    ):
        """Iterator-capable engines should keep the existing all-error serial retry fallback."""
        from modules.tts.engine import BatchResult

        class FailingIteratorTTSEngine:
            supports_incremental_batch_results = True
            is_loaded = True
            sample_rate = 24000

            def __init__(self):
                self.synthesize_calls = []

            def iter_synthesize_batch(self, items, progress_callback=None):  # noqa: ANN001
                total = len(items)
                for completed, item in enumerate(items, start=1):
                    if progress_callback is not None:
                        progress_callback(completed, total)
                    yield BatchResult(
                        audio=None,
                        index=item.index,
                        error="grouped failure",
                        duration_ms=0,
                    )

            def synthesize(self, text, voice, speed):  # noqa: ANN001
                _ = voice, speed
                self.synthesize_calls.append(text)
                return np.full(2, len(self.synthesize_calls), dtype=np.float32)

        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
        )
        pipeline = ConversionPipeline(config)
        fake_tts = FailingIteratorTTSEngine()
        pipeline._tts_engine = fake_tts
        chapter = MagicMock(number=1, title="Chapter 1")

        chapter_audio = pipeline.stages.chapter_processor.synthesize_chapter_chunks(  # noqa: SLF001
            chapter=chapter,
            chapter_idx=0,
            total_chapters=1,
            chunks=["chunk one", "chunk two"],
        )

        assert isinstance(chapter_audio, np.ndarray)
        assert chapter_audio.tolist() == [1.0, 1.0, 2.0, 2.0]
        assert fake_tts.synthesize_calls == ["chunk one", "chunk two"]

    def test_synthesize_chapter_chunks_consumes_incremental_file_results(
        self,
        mock_components,
        tmp_path,
    ):
        """Iterator-capable engines can stream chunk WAV paths without retaining arrays."""
        from modules.tts.engine import BatchResult

        class FileResultTTSEngine:
            supports_incremental_batch_results = True
            supports_file_batch_results = True
            is_loaded = True
            sample_rate = 24000

            def __init__(self):
                self.output_paths = []

            def iter_synthesize_batch(self, items, progress_callback=None):  # noqa: ANN001
                total = len(items)
                for completed, item in enumerate(items, start=1):
                    assert item.output_path is not None
                    self.output_paths.append(item.output_path)
                    with wave.open(str(item.output_path), "wb") as wav_file:
                        wav_file.setnchannels(1)
                        wav_file.setsampwidth(2)
                        wav_file.setframerate(24000)
                        wav_file.writeframes((np.array([item.index + 1, item.index + 1], dtype=np.int16)).tobytes())
                    if progress_callback is not None:
                        progress_callback(completed, total)
                    yield BatchResult(
                        audio=None,
                        audio_path=item.output_path,
                        index=item.index,
                        error=None,
                        duration_ms=1,
                    )

        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_tts_file_batch_results=True,
        )
        pipeline = ConversionPipeline(config)
        fake_tts = FileResultTTSEngine()
        pipeline._tts_engine = fake_tts
        chapter = MagicMock(number=1, title="Chapter 1")

        chapter_wav = pipeline.stages.chapter_processor.synthesize_chapter_chunks(  # noqa: SLF001
            chapter=chapter,
            chapter_idx=0,
            total_chapters=1,
            chunks=["chunk one", "chunk two"],
        )

        assert isinstance(chapter_wav, Path)
        assert len(fake_tts.output_paths) == 2
        with wave.open(str(chapter_wav), "rb") as wav_file:
            assert wav_file.getnframes() == 4
            assert wav_file.getframerate() == 24000

    def test_synthesize_chapter_chunks_auto_uses_file_results_for_long_chapters(
        self,
        mock_components,
        tmp_path,
    ):
        """Long chapters should get file-backed chunk paths without explicit opt-in."""
        from modules.tts.engine import BatchResult

        class FileResultTTSEngine:
            supports_incremental_batch_results = True
            supports_file_batch_results = True
            is_loaded = True
            sample_rate = 24000

            def __init__(self):
                self.output_paths = []

            def iter_synthesize_batch(self, items, progress_callback=None):  # noqa: ANN001
                total = len(items)
                for completed, item in enumerate(items, start=1):
                    assert item.output_path is not None
                    self.output_paths.append(item.output_path)
                    with wave.open(str(item.output_path), "wb") as wav_file:
                        wav_file.setnchannels(1)
                        wav_file.setsampwidth(2)
                        wav_file.setframerate(24000)
                        wav_file.writeframes(
                            np.array([item.index + 1, item.index + 1], dtype=np.int16).tobytes()
                        )
                    if progress_callback is not None:
                        progress_callback(completed, total)
                    yield BatchResult(
                        audio=None,
                        audio_path=item.output_path,
                        index=item.index,
                        error=None,
                        duration_ms=1,
                    )

        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_tts_file_batch_results=False,
            tts_file_batch_results_auto_threshold_chunks=2,
        )
        pipeline = ConversionPipeline(config)
        fake_tts = FileResultTTSEngine()
        pipeline._tts_engine = fake_tts
        chapter = MagicMock(number=1, title="Chapter 1")

        chapter_wav = pipeline.stages.chapter_processor.synthesize_chapter_chunks(  # noqa: SLF001
            chapter=chapter,
            chapter_idx=0,
            total_chapters=1,
            chunks=["chunk one", "chunk two"],
        )

        assert isinstance(chapter_wav, Path)
        assert len(fake_tts.output_paths) == 2
        with wave.open(str(chapter_wav), "rb") as wav_file:
            assert wav_file.getnframes() == 4

    def test_synthesize_chapter_chunks_keeps_small_chapters_in_memory_without_opt_in(
        self,
        mock_components,
        tmp_path,
    ):
        """Automatic file-backed synthesis should not alter small chapter behavior."""
        from modules.tts.engine import BatchResult

        class FileCapableTTSEngine:
            supports_incremental_batch_results = True
            supports_file_batch_results = True
            is_loaded = True
            sample_rate = 24000

            def __init__(self):
                self.output_paths = []

            def iter_synthesize_batch(self, items, progress_callback=None):  # noqa: ANN001
                total = len(items)
                for completed, item in enumerate(items, start=1):
                    self.output_paths.append(item.output_path)
                    if progress_callback is not None:
                        progress_callback(completed, total)
                    yield BatchResult(
                        audio=np.full(2, item.index + 1, dtype=np.float32),
                        index=item.index,
                        error=None,
                        duration_ms=1,
                    )

        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_tts_file_batch_results=False,
            tts_file_batch_results_auto_threshold_chunks=3,
        )
        pipeline = ConversionPipeline(config)
        fake_tts = FileCapableTTSEngine()
        pipeline._tts_engine = fake_tts
        chapter = MagicMock(number=1, title="Chapter 1")

        chapter_audio = pipeline.stages.chapter_processor.synthesize_chapter_chunks(  # noqa: SLF001
            chapter=chapter,
            chapter_idx=0,
            total_chapters=1,
            chunks=["chunk one", "chunk two"],
        )

        assert isinstance(chapter_audio, np.ndarray)
        assert fake_tts.output_paths == [None, None]

    def test_synthesize_chapter_chunks_incremental_early_all_error_retries_serially(
        self,
        mock_components,
        tmp_path,
    ):
        """Iterator all-error retry should still run when the iterator exits early."""
        from modules.tts.engine import BatchResult

        class EarlyFailingIteratorTTSEngine:
            supports_incremental_batch_results = True
            is_loaded = True
            sample_rate = 24000

            def __init__(self):
                self.synthesize_calls = []

            def iter_synthesize_batch(self, items, progress_callback=None):  # noqa: ANN001
                item = items[0]
                if progress_callback is not None:
                    progress_callback(1, len(items))
                yield BatchResult(
                    audio=None,
                    index=item.index,
                    error="grouped failure",
                    duration_ms=0,
                )

            def synthesize(self, text, voice, speed):  # noqa: ANN001
                _ = voice, speed
                self.synthesize_calls.append(text)
                return np.full(2, len(self.synthesize_calls), dtype=np.float32)

        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
        )
        pipeline = ConversionPipeline(config)
        fake_tts = EarlyFailingIteratorTTSEngine()
        pipeline._tts_engine = fake_tts
        chapter = MagicMock(number=1, title="Chapter 1")

        chapter_audio = pipeline.stages.chapter_processor.synthesize_chapter_chunks(  # noqa: SLF001
            chapter=chapter,
            chapter_idx=0,
            total_chapters=1,
            chunks=["chunk one", "chunk two"],
        )

        assert isinstance(chapter_audio, np.ndarray)
        assert chapter_audio.tolist() == [1.0, 1.0, 2.0, 2.0]
        assert fake_tts.synthesize_calls == ["chunk one", "chunk two"]

    def test_synthesize_chapter_chunks_rejects_partial_final_batch(self, mock_components, tmp_path):
        """A chapter must not be assembled when final batch results omit chunk audio."""
        from modules.tts.engine import BatchResult

        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
        )
        pipeline = ConversionPipeline(config)
        pipeline._tts_engine = mock_components["tts"].return_value

        def _partial_batch(items, progress_callback=None):  # noqa: ANN001
            _ = progress_callback
            assert len(items) == 2
            return [
                BatchResult(
                    audio=np.array([0.1, 0.2, 0.3], dtype=np.float32),
                    index=0,
                    error=None,
                    duration_ms=100,
                ),
                BatchResult(
                    audio=None,
                    index=1,
                    error="still failed",
                    duration_ms=0,
                ),
            ]

        pipeline._tts_engine.synthesize_batch.side_effect = _partial_batch
        chapter = MagicMock(number=1, title="Chapter 1")

        chapter_audio = pipeline.stages.chapter_processor.synthesize_chapter_chunks(  # noqa: SLF001
            chapter=chapter,
            chapter_idx=0,
            total_chapters=1,
            chunks=["chunk one", "chunk two"],
        )

        assert chapter_audio is None

    def test_synthesize_chapter_chunks_orders_audio_by_chunk_index(self, mock_components, tmp_path):
        """Batch backends may return results out of order; audio must follow source chunks."""
        from modules.tts.engine import BatchResult

        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
        )
        pipeline = ConversionPipeline(config)
        pipeline._tts_engine = mock_components["tts"].return_value

        def _out_of_order_batch(items, progress_callback=None):  # noqa: ANN001
            _ = progress_callback
            assert len(items) == 2
            return [
                BatchResult(
                    audio=np.array([2.0, 2.0], dtype=np.float32),
                    index=1,
                    error=None,
                    duration_ms=100,
                ),
                BatchResult(
                    audio=np.array([1.0, 1.0], dtype=np.float32),
                    index=0,
                    error=None,
                    duration_ms=100,
                ),
            ]

        pipeline._tts_engine.synthesize_batch.side_effect = _out_of_order_batch
        chapter = MagicMock(number=1, title="Chapter 1")

        chapter_audio = pipeline.stages.chapter_processor.synthesize_chapter_chunks(  # noqa: SLF001
            chapter=chapter,
            chapter_idx=0,
            total_chapters=1,
            chunks=["chunk one", "chunk two"],
        )

        assert isinstance(chapter_audio, np.ndarray)
        assert chapter_audio.tolist() == [1.0, 1.0, 2.0, 2.0]

    def test_clean_and_synthesize_with_overlap_streams_large_audio_without_concat(self, mock_components, tmp_path):
        """Overlap mode should stream directly to chapter WAV without temp concatenation."""
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_chunk_overlap=True,
        )
        pipeline = ConversionPipeline(config)
        pipeline.config.max_in_memory_chapter_samples = 1
        pipeline._tts_engine = mock_components["tts"].return_value

        chapter = MagicMock(number=1, title="Chapter 1")

        def _clean_chunk_safely(*, cleaner, chunk, llm_cleaning_enabled):  # noqa: ANN001
            _ = cleaner
            return f"cleaned {chunk}", llm_cleaning_enabled, None

        processor = pipeline.stages.chapter_processor  # noqa: SLF001 - stage collaborator contract
        with patch.object(processor, "get_text_cleaner_for_chapter", return_value=MagicMock()), \
             patch.object(processor, "clean_chunk_safely", side_effect=_clean_chunk_safely), \
             patch.object(processor, "save_pre_clean_markdown"), \
             patch.object(processor, "maybe_capture_cleaner_stats", return_value=None), \
             patch.object(processor, "log_cleaner_stats_delta"), \
             patch.object(processor, "save_cleaning_artifacts"):
            chapter_wav, was_cancelled = processor.clean_and_synthesize_with_overlap(
                chapter=chapter,
                chapter_idx=0,
                total_chapters=1,
                chunks=["chunk one", "chunk two"],
                output_dir=tmp_path,
            )

        assert was_cancelled is False
        assert isinstance(chapter_wav, Path)
        with wave.open(str(chapter_wav), "rb") as wav_file:
            assert wav_file.getnframes() == 6
            assert wav_file.getframerate() == 24000

    def test_clean_and_synthesize_with_overlap_fails_when_tts_chunk_fails(
        self,
        mock_components,
        tmp_path,
    ):
        """Overlap mode must not finalize partial audio when a chunk synthesis fails."""
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_chunk_overlap=True,
        )
        pipeline = ConversionPipeline(config)
        pipeline._tts_engine = mock_components["tts"].return_value
        pipeline._tts_engine.synthesize.side_effect = [
            np.array([0.1, 0.2], dtype=np.float32),
            RuntimeError("tts boom"),
        ]

        chapter = MagicMock(number=1, title="Chapter 1")

        def _clean_chunk_safely(*, cleaner, chunk, llm_cleaning_enabled):  # noqa: ANN001
            _ = cleaner
            return f"cleaned {chunk}", llm_cleaning_enabled, None

        processor = pipeline.stages.chapter_processor  # noqa: SLF001 - stage collaborator contract
        with patch.object(processor, "get_text_cleaner_for_chapter", return_value=MagicMock()), \
             patch.object(processor, "clean_chunk_safely", side_effect=_clean_chunk_safely), \
             patch.object(processor, "save_pre_clean_markdown"), \
             patch.object(processor, "maybe_capture_cleaner_stats", return_value=None), \
             patch.object(processor, "log_cleaner_stats_delta"), \
             patch.object(processor, "save_cleaning_artifacts"):
            chapter_wav, was_cancelled = processor.clean_and_synthesize_with_overlap(
                chapter=chapter,
                chapter_idx=0,
                total_chapters=1,
                chunks=["chunk one", "chunk two"],
                output_dir=tmp_path,
            )

        assert was_cancelled is False
        assert chapter_wav is None

    def test_clean_and_synthesize_with_overlap_returns_when_producer_fails(self, mock_components, tmp_path):
        """Producer exceptions should be delivered to the consumer instead of hanging queue.get()."""
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_chunk_overlap=True,
        )
        pipeline = ConversionPipeline(config)
        pipeline._tts_engine = mock_components["tts"].return_value
        chapter = MagicMock(number=1, title="Chapter 1")

        def _run_overlap(result_holder):  # noqa: ANN001
            try:
                result_holder["result"] = pipeline.stages.chapter_processor.clean_and_synthesize_with_overlap(  # noqa: SLF001
                    chapter=chapter,
                    chapter_idx=0,
                    total_chapters=1,
                    chunks=["chunk one", "chunk two"],
                    output_dir=tmp_path,
                )
            except Exception as exc:  # pragma: no cover - acceptable propagated failure
                result_holder["error"] = exc

        result_holder: dict[str, object] = {}
        processor = pipeline.stages.chapter_processor  # noqa: SLF001 - stage collaborator contract
        with patch.object(processor, "get_text_cleaner_for_chapter", return_value=MagicMock()), \
             patch.object(processor, "clean_chunk_safely", side_effect=RuntimeError("clean boom")), \
             patch.object(processor, "save_pre_clean_markdown"), \
             patch.object(processor, "maybe_capture_cleaner_stats", return_value=None), \
             patch.object(processor, "log_cleaner_stats_delta"), \
             patch.object(processor, "save_cleaning_artifacts"):
            worker = threading.Thread(target=_run_overlap, args=(result_holder,), daemon=True)
            worker.start()
            worker.join(timeout=0.5)

        assert not worker.is_alive()
        assert "clean boom" in str(result_holder.get("error") or result_holder.get("result"))

    def test_clean_and_synthesize_with_overlap_observes_cancellation_while_cleaner_is_blocked(
        self,
        mock_components,
        tmp_path,
    ):
        """Overlap mode should poll cancellation while waiting for cleaner output."""
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_chunk_overlap=True,
        )
        pipeline = ConversionPipeline(config)
        pipeline._tts_engine = mock_components["tts"].return_value
        chapter = MagicMock(number=1, title="Chapter 1")
        cleaner_entered = threading.Event()
        release_cleaner = threading.Event()
        result_holder: dict[str, object] = {}

        def _clean_chunk_safely(*, cleaner, chunk, llm_cleaning_enabled):  # noqa: ANN001
            _ = (cleaner, chunk, llm_cleaning_enabled)
            cleaner_entered.set()
            release_cleaner.wait(timeout=2.0)
            return "cleaned chunk", True, None

        def _run_overlap() -> None:
            result_holder["result"] = pipeline.stages.chapter_processor.clean_and_synthesize_with_overlap(  # noqa: SLF001
                chapter=chapter,
                chapter_idx=0,
                total_chapters=1,
                chunks=["chunk one", "chunk two"],
                output_dir=tmp_path,
            )

        processor = pipeline.stages.chapter_processor  # noqa: SLF001 - stage collaborator contract
        with patch.object(processor, "get_text_cleaner_for_chapter", return_value=MagicMock()), \
             patch.object(processor, "clean_chunk_safely", side_effect=_clean_chunk_safely), \
             patch.object(processor, "save_pre_clean_markdown"), \
             patch.object(processor, "maybe_capture_cleaner_stats", return_value=None), \
             patch.object(processor, "log_cleaner_stats_delta"), \
             patch.object(processor, "save_cleaning_artifacts"):
            worker = threading.Thread(target=_run_overlap, daemon=True)
            worker.start()
            assert cleaner_entered.wait(timeout=0.5)
            pipeline.cancel()
            worker.join(timeout=0.5)
            was_still_blocked_after_cancel = worker.is_alive()
            release_cleaner.set()
            worker.join(timeout=1.0)

        assert not was_still_blocked_after_cancel
        assert result_holder["result"] == (None, True)

    def test_encode_chapter_audio_uses_source_duration_without_mp3_probe(self, mock_components, tmp_path):
        """Encoding should derive duration from source audio instead of probing MP3 output."""
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            normalize_volume=False,
        )
        pipeline = ConversionPipeline(config)
        chapter = MagicMock(number=1, title="Chapter 1")
        wav_path = tmp_path / "chapter.wav"
        with wave.open(str(wav_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes(b"\x00\x00" * 24000)

        fake_encoder = MagicMock()

        def _wav_to_mp3(input_path, output_path, bitrate="128k"):
            _ = bitrate
            assert Path(input_path).exists()
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"mp3")

        fake_encoder.wav_to_mp3.side_effect = _wav_to_mp3
        fake_encoder.get_duration.side_effect = AssertionError("unexpected MP3 duration probe")

        with patch("modules.audio.encoder.AudioEncoder", return_value=fake_encoder):
            mp3_path, duration_ms = pipeline.stages.chapter_processor.encode_chapter_audio(  # noqa: SLF001
                chapter=chapter,
                chapter_idx=0,
                total_chapters=1,
                output_dir=tmp_path,
                chapter_wav=wav_path,
            )

        assert mp3_path.exists()
        assert duration_ms == 1000
        assert fake_encoder.get_duration.call_count == 0

    def test_convert_fails_when_any_chapter_fails_before_packaging(self, tmp_path):
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
        )
        pipeline = ConversionPipeline(config)
        test_file = tmp_path / "book.pdf"
        test_file.write_text("dummy pdf content")

        chapter_one_mp3 = tmp_path / "output" / "Test Book" / "chapters" / "chapter_01.mp3"
        chapter_one_mp3.parent.mkdir(parents=True, exist_ok=True)
        chapter_one_mp3.write_bytes(b"mp3")

        document = MagicMock()
        document.title = "Test Book"
        document.author = "Test Author"
        document.chapters = [
            MagicMock(number=1, title="One", content="alpha"),
            MagicMock(number=2, title="Two", content="beta"),
        ]

        chapter_results = [
            ChapterResult(
                chapter_number=1,
                chapter_title="One",
                mp3_path=chapter_one_mp3,
                duration_ms=1000,
            ),
            ChapterResult(
                chapter_number=2,
                chapter_title="Two",
                error="ffmpeg exploded",
            ),
        ]

        with patch.object(pipeline.stages, "ingest", return_value=document), \
             patch.object(pipeline.stages, "save_intermediate_source"), \
             patch.object(pipeline.stages, "warmup_models"), \
             patch.object(pipeline.stages, "process_chapters_sequential", return_value=chapter_results), \
             patch.object(pipeline.stages, "package") as mock_package:
            result = pipeline.convert(test_file)

        assert result.success is False
        assert "chapter 2" in str(result.error).lower()
        assert "Two" in str(result.error)
        assert "ffmpeg exploded" in str(result.error)
        assert result.chapters == chapter_results
        assert result.output_path is None
        assert mock_package.call_count == 0

    def test_convert_fails_when_dispatch_omits_ingested_chapter_before_packaging(self, tmp_path):
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
        )
        pipeline = ConversionPipeline(config)
        test_file = tmp_path / "book.pdf"
        test_file.write_text("dummy pdf content")

        chapter_one_mp3 = tmp_path / "output" / "Test Book" / "chapters" / "chapter_01.mp3"
        chapter_one_mp3.parent.mkdir(parents=True, exist_ok=True)
        chapter_one_mp3.write_bytes(b"mp3")

        document = Document(
            title="Test Book",
            author="Test Author",
            chapters=[
                Chapter(number=1, title="One", content="alpha"),
                Chapter(number=2, title="Two", content="beta"),
            ],
        )
        chapter_results = [
            ChapterResult(
                chapter_number=1,
                chapter_title="One",
                mp3_path=chapter_one_mp3,
                duration_ms=1000,
            ),
        ]

        with patch.object(pipeline.stages, "ingest", return_value=document), \
             patch.object(pipeline.stages, "save_intermediate_source"), \
             patch.object(pipeline.stages, "warmup_models"), \
             patch.object(pipeline.stages, "process_chapters_sequential", return_value=chapter_results), \
             patch.object(pipeline.stages, "package") as mock_package:
            result = pipeline.convert(test_file)

        assert result.success is False
        assert "structural mismatch" in str(result.error).lower()
        assert "missing chapter 2" in str(result.error).lower()
        assert "Two" in str(result.error)
        assert result.chapters == chapter_results
        assert result.output_path is None
        assert mock_package.call_count == 0

    def test_convert_fails_when_dispatch_duplicates_chapter_before_packaging(self, tmp_path):
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
        )
        pipeline = ConversionPipeline(config)
        test_file = tmp_path / "book.pdf"
        test_file.write_text("dummy pdf content")

        chapters_dir = tmp_path / "output" / "Test Book" / "chapters"
        chapter_one_mp3 = chapters_dir / "chapter_01.mp3"
        duplicate_mp3 = chapters_dir / "chapter_01_duplicate.mp3"
        chapters_dir.mkdir(parents=True, exist_ok=True)
        chapter_one_mp3.write_bytes(b"mp3")
        duplicate_mp3.write_bytes(b"mp3")

        document = Document(
            title="Test Book",
            author="Test Author",
            chapters=[
                Chapter(number=1, title="One", content="alpha"),
                Chapter(number=2, title="Two", content="beta"),
            ],
        )
        chapter_results = [
            ChapterResult(
                chapter_number=1,
                chapter_title="One",
                mp3_path=chapter_one_mp3,
                duration_ms=1000,
            ),
            ChapterResult(
                chapter_number=1,
                chapter_title="One again",
                mp3_path=duplicate_mp3,
                duration_ms=1000,
            ),
        ]

        with patch.object(pipeline.stages, "ingest", return_value=document), \
             patch.object(pipeline.stages, "save_intermediate_source"), \
             patch.object(pipeline.stages, "warmup_models"), \
             patch.object(pipeline.stages, "process_chapters_sequential", return_value=chapter_results), \
             patch.object(pipeline.stages, "package") as mock_package:
            result = pipeline.convert(test_file)

        assert result.success is False
        assert "structural mismatch" in str(result.error).lower()
        assert "duplicate chapter 1" in str(result.error).lower()
        assert "missing chapter 2" in str(result.error).lower()
        assert result.chapters == chapter_results
        assert result.output_path is None
        assert mock_package.call_count == 0

    def test_convert_sorts_complete_chapter_results_before_packaging(self, tmp_path):
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
        )
        pipeline = ConversionPipeline(config)
        test_file = tmp_path / "book.pdf"
        test_file.write_text("dummy pdf content")

        chapters_dir = tmp_path / "output" / "Test Book" / "chapters"
        chapter_one_mp3 = chapters_dir / "chapter_01.mp3"
        chapter_two_mp3 = chapters_dir / "chapter_02.mp3"
        chapters_dir.mkdir(parents=True, exist_ok=True)
        chapter_one_mp3.write_bytes(b"mp3")
        chapter_two_mp3.write_bytes(b"mp3")

        document = Document(
            title="Test Book",
            author="Test Author",
            chapters=[
                Chapter(number=1, title="One", content="alpha"),
                Chapter(number=2, title="Two", content="beta"),
            ],
        )
        chapter_results = [
            ChapterResult(
                chapter_number=2,
                chapter_title="Two",
                mp3_path=chapter_two_mp3,
                duration_ms=2000,
            ),
            ChapterResult(
                chapter_number=1,
                chapter_title="One",
                mp3_path=chapter_one_mp3,
                duration_ms=1000,
            ),
        ]
        final_output = tmp_path / "output" / "Test Book" / "Test Book.m4b"

        def package_side_effect(**kwargs):
            _ = kwargs
            final_output.parent.mkdir(parents=True, exist_ok=True)
            final_output.write_bytes(b"m4b")
            return final_output

        with patch.object(pipeline.stages, "ingest", return_value=document), \
             patch.object(pipeline.stages, "save_intermediate_source"), \
             patch.object(pipeline.stages, "warmup_models"), \
             patch.object(pipeline.stages, "process_chapters_sequential", return_value=chapter_results), \
             patch.object(pipeline.stages, "package", side_effect=package_side_effect) as mock_package:
            result = pipeline.convert(test_file)

        assert result.success is True
        packaged_results = mock_package.call_args.kwargs["chapter_results"]
        assert [chapter.chapter_number for chapter in packaged_results] == [1, 2]
        assert result.chapters == packaged_results


def test_ingest_source_dispatches_text_parser_for_txt(tmp_path):
    source_path = tmp_path / "notes.txt"
    source_path.write_text("hello", encoding="utf-8")
    document = MagicMock()
    document.chapters = [MagicMock(title="notes", content="hello")]
    pipeline = MagicMock()

    with patch("modules.ingestion.text_parser.TextParser") as mock_text_parser:
        mock_text_parser.return_value.parse.return_value = document

        result = ingest_source(pipeline, source_path)

    assert result is document
    mock_text_parser.assert_called_once_with(source_path)


def test_ingest_source_dispatches_markdown_parser_for_md(tmp_path):
    source_path = tmp_path / "guide.md"
    source_path.write_text("# Guide", encoding="utf-8")
    document = MagicMock()
    document.chapters = [MagicMock(title="Guide", content="overview")]
    pipeline = MagicMock()

    with patch("modules.ingestion.markdown_parser.MarkdownParser") as mock_markdown_parser:
        mock_markdown_parser.return_value.parse.return_value = document

        result = ingest_source(pipeline, source_path)

    assert result is document
    mock_markdown_parser.assert_called_once_with(source_path)


def test_ingest_source_describes_extensionless_file_clearly(tmp_path):
    source_path = tmp_path / "README"
    source_path.write_text(
        "plain text without a supported extension", encoding="utf-8"
    )
    pipeline = MagicMock()

    with pytest.raises(
        ValueError,
        match=r"Unsupported file type: \(no extension\).*\.pdf.*\.epub.*\.txt.*\.md",
    ):
        ingest_source(pipeline, source_path)


def test_batch_output_dir_is_registered_for_temp_cleanup(tmp_path):
    """The per-chunk WAV directory must be tracked so cleanup removes it.

    _cleanup_temp skips its temp-root sweep whenever the tracked set is
    non-empty, so an untracked ch_NNN_tts directory is never reclaimed -- and
    on a failed batch validation its chunk WAVs are never unlinked either.
    """
    from dataclasses import dataclass
    from modules.pipeline.stages import synthesis

    temp_dir = tmp_path / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    tracked: list[Path] = []

    @dataclass
    class _Item:
        index: int
        output_path: Path | None = None

    class _Runtime:
        config = SimpleNamespace(
            temp_dir=temp_dir,
            enable_tts_file_batch_results=True,
        )

        @staticmethod
        def supports_file_batch_results() -> bool:
            return True

        @staticmethod
        def track_temp_cleanup_path(path: Path) -> None:
            tracked.append(Path(path))

    items = synthesis._with_batch_output_paths(
        _Runtime(),
        chapter=SimpleNamespace(number=7),
        batch_items=[_Item(index=0), _Item(index=1)],
    )

    chapter_temp_dir = temp_dir / "ch_007_tts"
    assert chapter_temp_dir.is_dir()
    assert tracked == [chapter_temp_dir]
    assert [item.output_path for item in items] == [
        chapter_temp_dir / "chunk_0000.wav",
        chapter_temp_dir / "chunk_0001.wav",
    ]
