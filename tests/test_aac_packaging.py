"""Tests for AAC packaging input selection."""

from __future__ import annotations

import gc
import threading
import weakref
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from modules.audio.packager import audio_files_support_stream_copy
from modules.pipeline.orchestrator import ChapterResult, ConversionPipeline, PipelineConfig
from modules.pipeline.stages import encoding as encoding_stage
from modules.pipeline.stages.packaging import resolve_background_audio_futures
from modules.pipeline.result_validation import (
    select_packaging_audio_input,
    select_packaging_audio_inputs,
)


class TestPackagingInputSelection:
    def test_select_packaging_audio_input_prefers_aac_over_wav(self, tmp_path):
        wav = tmp_path / "chapter.wav"
        aac = tmp_path / "chapter.m4a"
        wav.write_bytes(b"wav")
        aac.write_bytes(b"aac")

        result = type(
            "ChapterResult",
            (),
            {"wav_path": wav, "mp3_path": None, "aac_path": aac},
        )()

        selected = select_packaging_audio_input(result)
        assert selected == aac

    def test_select_packaging_audio_inputs_uses_aac_only_when_all_chapters_have_aac(
        self,
        tmp_path,
    ):
        first_aac = tmp_path / "one.m4a"
        first_wav = tmp_path / "one.wav"
        second_wav = tmp_path / "two.wav"
        for path in (first_aac, first_wav, second_wav):
            path.write_bytes(path.name.encode("utf-8"))

        first = type(
            "ChapterResult",
            (),
            {"wav_path": first_wav, "mp3_path": None, "aac_path": first_aac},
        )()
        second = type(
            "ChapterResult",
            (),
            {"wav_path": second_wav, "mp3_path": None, "aac_path": None},
        )()

        selected = select_packaging_audio_inputs([first, second])

        assert selected == [first_wav, second_wav]

    def test_select_packaging_audio_inputs_rejects_incompatible_mixed_inputs(
        self,
        tmp_path,
    ):
        first_aac = tmp_path / "one.m4a"
        second_wav = tmp_path / "two.wav"
        first_aac.write_bytes(b"aac")
        second_wav.write_bytes(b"wav")

        first = type(
            "ChapterResult",
            (),
            {"wav_path": None, "mp3_path": None, "aac_path": first_aac},
        )()
        second = type(
            "ChapterResult",
            (),
            {"wav_path": second_wav, "mp3_path": None, "aac_path": None},
        )()

        assert select_packaging_audio_inputs([first, second]) == []

    def test_resolve_background_audio_futures_sets_aac_path(self, tmp_path):
        aac = tmp_path / "one.m4a"
        aac.write_bytes(b"aac")
        result = type(
            "ChapterResult",
            (),
            {"wav_path": None, "mp3_path": None, "aac_path": None},
        )()
        future: Future = Future()
        future.set_result(aac)
        result.aac_future = future

        resolve_background_audio_futures([result])

        assert result.aac_path == aac
        assert select_packaging_audio_inputs([result]) == [aac]


class TestStreamCopyDetection:
    def test_audio_files_support_stream_copy_requires_m4a_inputs(self, tmp_path):
        first = tmp_path / "one.m4a"
        second = tmp_path / "two.wav"
        first.write_bytes(b"one")
        second.write_bytes(b"two")

        assert audio_files_support_stream_copy([first]) is True
        assert audio_files_support_stream_copy([first, second]) is False


class TestBackgroundChapterAacEncoding:
    @pytest.fixture(autouse=True)
    def _close_chapter_aac_executors(self):
        self._pipelines = []
        yield
        for pipeline in self._pipelines:
            executor = pipeline.stages.runtime.state.chapter_aac_executor
            if executor is not None:
                executor.shutdown(wait=True)
                pipeline.stages.runtime.state.chapter_aac_executor = None

    def _pipeline(self, tmp_path: Path) -> ConversionPipeline:
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
            emit_chapter_mp3_artifacts=False,
            enable_chapter_aac_packaging=True,
            enable_background_chapter_aac_encoding=True,
        )
        pipeline = ConversionPipeline(cfg)
        pipeline._tts_engine = SimpleNamespace(sample_rate=24_000)
        self._pipelines.append(pipeline)
        return pipeline

    def test_background_aac_future_does_not_retain_chapter_numpy_array(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pipeline = self._pipeline(tmp_path)
        chapter = SimpleNamespace(number=1, title="Chapter 1")
        chapter_audio = np.zeros(24_000, dtype=np.float32)
        audio_ref = weakref.ref(chapter_audio)

        encode_started = threading.Event()
        encode_release = threading.Event()
        captured_sources: list[object] = []

        def _blocked_encode_chapter_aac(*, context, chapter, chapter_wav, precomputed_rms=None, output_path=None):
            captured_sources.append(chapter_wav)
            encode_started.set()
            assert encode_release.wait(timeout=2.0)
            target = output_path or (context.config.temp_dir / f"chapter_{chapter.number:02d}.m4a")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"aac")
            return target

        monkeypatch.setattr(encoding_stage, "encode_chapter_aac", _blocked_encode_chapter_aac)

        result = ChapterResult(chapter_number=1, chapter_title="Chapter 1")
        finalized = pipeline.stages.chapter_processor.finalize_chapter_encoding(  # noqa: SLF001
            result=result,
            chapter_wav=chapter_audio,
            chapter=chapter,
            chapter_idx=0,
            total_chapters=2,
            output_dir=pipeline.config.output_dir,
        )
        del chapter_audio
        gc.collect()

        assert finalized.wav_path is not None
        assert finalized.aac_future is not None
        assert encode_started.wait(timeout=2.0)
        assert audio_ref() is None, (
            "queued AAC future must not retain the in-memory chapter array after WAV write"
        )
        assert captured_sources
        assert all(isinstance(source, type(finalized.wav_path)) for source in captured_sources)

        encode_release.set()
        assert finalized.aac_future.result(timeout=2.0).exists()

    def test_background_aac_encoding_bounds_pending_work_to_one_inflight_encode(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pipeline = self._pipeline(tmp_path)
        first_encode_started = threading.Event()
        release_first_encode = threading.Event()
        second_finalize_started = threading.Event()
        second_finalize_finished = threading.Event()

        def _blocked_encode_chapter_aac(*, context, chapter, chapter_wav, precomputed_rms=None, output_path=None):
            if chapter.number == 1:
                first_encode_started.set()
                assert release_first_encode.wait(timeout=2.0)
            target = output_path or (context.config.temp_dir / f"chapter_{chapter.number:02d}.m4a")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"aac")
            return target

        monkeypatch.setattr(encoding_stage, "encode_chapter_aac", _blocked_encode_chapter_aac)

        first = ChapterResult(chapter_number=1, chapter_title="Chapter 1")
        pipeline.stages.chapter_processor.finalize_chapter_encoding(  # noqa: SLF001
            result=first,
            chapter_wav=np.zeros(24_000, dtype=np.float32),
            chapter=SimpleNamespace(number=1, title="Chapter 1"),
            chapter_idx=0,
            total_chapters=2,
            output_dir=pipeline.config.output_dir,
        )
        assert first_encode_started.wait(timeout=2.0)

        def _finalize_second_chapter() -> None:
            second_finalize_started.set()
            pipeline.stages.chapter_processor.finalize_chapter_encoding(  # noqa: SLF001
                result=ChapterResult(chapter_number=2, chapter_title="Chapter 2"),
                chapter_wav=np.ones(24_000, dtype=np.float32),
                chapter=SimpleNamespace(number=2, title="Chapter 2"),
                chapter_idx=1,
                total_chapters=2,
                output_dir=pipeline.config.output_dir,
            )
            second_finalize_finished.set()

        second_thread = threading.Thread(target=_finalize_second_chapter)
        second_thread.start()
        assert second_finalize_started.wait(timeout=2.0)
        assert not second_finalize_finished.wait(timeout=0.05)

        release_first_encode.set()
        second_thread.join(timeout=2.0)
        assert second_finalize_finished.is_set()

    def test_background_aac_encoder_failure_does_not_block_subsequent_chapters(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pipeline = self._pipeline(tmp_path)
        calls = 0

        def _failing_then_success_encode(*, context, chapter, chapter_wav, precomputed_rms=None, output_path=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("aac encode failed")
            target = output_path or (context.config.temp_dir / f"chapter_{chapter.number:02d}.m4a")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"aac")
            return target

        monkeypatch.setattr(encoding_stage, "encode_chapter_aac", _failing_then_success_encode)

        first = ChapterResult(chapter_number=1, chapter_title="Chapter 1")
        second = ChapterResult(chapter_number=2, chapter_title="Chapter 2")
        pipeline.stages.chapter_processor.finalize_chapter_encoding(  # noqa: SLF001
            result=first,
            chapter_wav=np.zeros(24_000, dtype=np.float32),
            chapter=SimpleNamespace(number=1, title="Chapter 1"),
            chapter_idx=0,
            total_chapters=2,
            output_dir=pipeline.config.output_dir,
        )
        pipeline.stages.chapter_processor.finalize_chapter_encoding(  # noqa: SLF001
            result=second,
            chapter_wav=np.ones(24_000, dtype=np.float32),
            chapter=SimpleNamespace(number=2, title="Chapter 2"),
            chapter_idx=1,
            total_chapters=2,
            output_dir=pipeline.config.output_dir,
        )

        with pytest.raises(RuntimeError, match="aac encode failed"):
            first.aac_future.result(timeout=2.0)
        assert second.aac_future.result(timeout=2.0).exists()

    def test_finish_conversion_run_shuts_down_blocked_aac_executor(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.pipeline.conversion_lifecycle import finish_conversion_run

        pipeline = self._pipeline(tmp_path)
        encode_release = threading.Event()

        def _blocked_encode_chapter_aac(*, context, chapter, chapter_wav, precomputed_rms=None, output_path=None):
            assert encode_release.wait(timeout=2.0)
            target = output_path or (context.config.temp_dir / f"chapter_{chapter.number:02d}.m4a")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"aac")
            return target

        monkeypatch.setattr(encoding_stage, "encode_chapter_aac", _blocked_encode_chapter_aac)

        result = ChapterResult(chapter_number=1, chapter_title="Chapter 1")
        pipeline.stages.chapter_processor.finalize_chapter_encoding(  # noqa: SLF001
            result=result,
            chapter_wav=np.zeros(24_000, dtype=np.float32),
            chapter=SimpleNamespace(number=1, title="Chapter 1"),
            chapter_idx=0,
            total_chapters=1,
            output_dir=pipeline.config.output_dir,
        )
        assert pipeline.stages.runtime.state.chapter_aac_executor is not None

        finish_conversion_run(
            pipeline.stages.runtime,
            cleanup_models=lambda: None,
            cleanup_temp=lambda: None,
        )

        assert pipeline.stages.runtime.state.chapter_aac_executor is None
        encode_release.set()


class TestDurableChapterAacForRepair:
    def test_aac_only_finalize_persists_durable_chapter_path_outside_temp(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """BUG1: AAC-only mode must leave a durable chapter path for repair."""
        chapters_dir = tmp_path / "output" / "Book" / "chapters"
        chapters_dir.mkdir(parents=True)
        temp_dir = tmp_path / "temp"
        temp_dir.mkdir()

        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=temp_dir,
            enable_parallel=False,
            emit_chapter_mp3_artifacts=False,
            enable_chapter_aac_packaging=True,
            enable_background_chapter_aac_encoding=False,
        )
        pipeline = ConversionPipeline(cfg)
        pipeline._tts_engine = SimpleNamespace(sample_rate=24_000)

        def _encode_chapter_aac(*, context, chapter, chapter_wav, precomputed_rms=None, output_path=None):
            _ = (context, chapter_wav, precomputed_rms)
            target = output_path or (temp_dir / f"chapter_{chapter.number:02d}.m4a")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"aac")
            return target

        monkeypatch.setattr(encoding_stage, "encode_chapter_aac", _encode_chapter_aac)

        result = ChapterResult(chapter_number=1, chapter_title="Chapter 1")
        finalized = pipeline.stages.chapter_processor.finalize_chapter_encoding(  # noqa: SLF001
            result=result,
            chapter_wav=np.zeros(24_000, dtype=np.float32),
            chapter=SimpleNamespace(number=1, title="Chapter 1"),
            chapter_idx=0,
            total_chapters=1,
            output_dir=chapters_dir,
        )

        assert finalized.mp3_path is not None
        assert finalized.aac_path == finalized.mp3_path
        assert finalized.mp3_path.exists()
        assert finalized.mp3_path.suffix.lower() == ".m4a"
        assert chapters_dir in finalized.mp3_path.parents
        assert temp_dir not in finalized.mp3_path.parents

        # Simulate post-packaging temp cleanup: durable chapter audio must survive.
        for path in temp_dir.rglob("*"):
            if path.is_file():
                path.unlink()
        assert finalized.mp3_path.exists()

    def test_background_aac_resolve_sets_mp3_path_for_durable_outputs(
        self,
        tmp_path,
    ) -> None:
        chapters_dir = tmp_path / "output" / "chapters"
        chapters_dir.mkdir(parents=True)
        durable = chapters_dir / "01_Chapter_1.m4a"
        durable.write_bytes(b"aac")

        result = ChapterResult(chapter_number=1, chapter_title="Chapter 1")
        future: Future = Future()
        future.set_result(durable)
        result.aac_future = future

        resolve_background_audio_futures(
            [result],
            runtime=SimpleNamespace(
                config=SimpleNamespace(temp_dir=tmp_path / "temp"),
                track_temp_cleanup_path=lambda _path: None,
            ),
        )

        assert result.aac_path == durable
        assert result.mp3_path == durable


class TestTempChapterAacCleanupTracking:
    @contextmanager
    def _pipeline(self, tmp_path: Path, *, emit_mp3: bool):
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
            emit_chapter_mp3_artifacts=emit_mp3,
            enable_chapter_aac_packaging=True,
            enable_background_chapter_aac_encoding=True,
        )
        pipeline = ConversionPipeline(cfg)
        pipeline._tts_engine = SimpleNamespace(sample_rate=24_000)
        try:
            yield pipeline
        finally:
            executor = pipeline.stages.runtime.state.chapter_aac_executor
            if executor is not None:
                executor.shutdown(wait=True)
                pipeline.stages.runtime.state.chapter_aac_executor = None

    @staticmethod
    def _encode_writing_output(*, context, chapter, chapter_wav, precomputed_rms=None, output_path=None):
        _ = (chapter_wav, precomputed_rms)
        target = output_path or (context.config.temp_dir / f"chapter_{chapter.number:02d}.m4a")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"aac")
        return target

    def test_failed_run_does_not_orphan_background_temp_chapter_aac(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Background AAC temp outputs must be tracked at submit time.

        A conversion that fails or is cancelled after the encode never reaches
        packaging (the only other place that tracks them), and the tracked-paths
        cleanup branch does not sweep the temp root.
        """
        with self._pipeline(tmp_path, emit_mp3=True) as pipeline:
            monkeypatch.setattr(encoding_stage, "encode_chapter_aac", self._encode_writing_output)
            temp_dir = pipeline.config.temp_dir
            temp_dir.mkdir(parents=True, exist_ok=True)

            wav = temp_dir / "chapter_01.wav"
            wav.write_bytes(b"wav")
            pipeline.stages.runtime.track_temp_cleanup_path(wav)

            result = ChapterResult(chapter_number=1, chapter_title="Chapter 1")
            pipeline.stages.chapter_processor._ensure_chapter_aac(  # noqa: SLF001
                result=result,
                chapter=SimpleNamespace(number=1, title="Chapter 1"),
                encoding_source=wav,
                output_dir=pipeline.config.output_dir,
            )
            assert result.aac_future is not None
            assert result.aac_future.result(timeout=2.0).exists()

            # The run fails before packaging resolves the future; cleanup must not
            # leave the encoded chapter .m4a behind in the temp root.
            pipeline._cleanup_temp()  # noqa: SLF001

            assert not list(temp_dir.glob("*.m4a"))

    def test_durable_chapter_aac_survives_temp_cleanup(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Submit-time tracking must not sweep durable repair chapter audio."""
        with self._pipeline(tmp_path, emit_mp3=False) as pipeline:
            monkeypatch.setattr(encoding_stage, "encode_chapter_aac", self._encode_writing_output)
            temp_dir = pipeline.config.temp_dir
            temp_dir.mkdir(parents=True, exist_ok=True)
            chapters_dir = tmp_path / "output" / "Book" / "chapters"
            chapters_dir.mkdir(parents=True)

            wav = temp_dir / "chapter_01.wav"
            wav.write_bytes(b"wav")
            pipeline.stages.runtime.track_temp_cleanup_path(wav)

            result = ChapterResult(chapter_number=1, chapter_title="Chapter 1")
            pipeline.stages.chapter_processor._ensure_chapter_aac(  # noqa: SLF001
                result=result,
                chapter=SimpleNamespace(number=1, title="Chapter 1"),
                encoding_source=wav,
                output_dir=chapters_dir,
            )
            assert result.aac_future is not None
            aac_path = result.aac_future.result(timeout=2.0)
            assert temp_dir not in aac_path.parents

            pipeline._cleanup_temp()  # noqa: SLF001

            assert aac_path.exists()


class TestPackagingSafeTitle:
    @staticmethod
    def _runtime(tmp_path: Path) -> SimpleNamespace:
        return SimpleNamespace(
            config=SimpleNamespace(
                voice="af_heart",
                conversion_mode="audiobook",
                temp_dir=tmp_path / "temp",
            ),
            state=SimpleNamespace(
                chapter_aac_executor=None,
                chapter_aac_pending_future=None,
            ),
            track_temp_cleanup_path=lambda _path: None,
        )

    def _chapter_result(self, tmp_path: Path) -> ChapterResult:
        wav = tmp_path / "chapter_01.wav"
        wav.write_bytes(b"wav")
        return ChapterResult(
            chapter_number=1,
            chapter_title="Chapter 1",
            wav_path=wav,
            duration_ms=1000,
        )

    def test_package_chapters_truncates_very_long_titles(self, tmp_path):
        from modules.pipeline.stages.packaging import package_chapters

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        long_title = "A Very Long Title " * 30

        with patch("modules.audio.packager.M4BPackager") as mock_packager:
            m4b_path = package_chapters(
                self._runtime(tmp_path),
                [self._chapter_result(tmp_path)],
                long_title,
                "Author",
                output_dir,
            )

        assert mock_packager.return_value.create_m4b.called
        assert m4b_path.suffix == ".m4b"
        assert len(m4b_path.stem) == 50

    def test_package_chapters_falls_back_when_title_sanitizes_to_empty(self, tmp_path):
        from modules.pipeline.stages.packaging import package_chapters

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        with patch("modules.audio.packager.M4BPackager"):
            m4b_path = package_chapters(
                self._runtime(tmp_path),
                [self._chapter_result(tmp_path)],
                "",
                "Author",
                output_dir,
            )

        assert m4b_path.name == "audiobook.m4b"

    def test_package_chapters_sanitizes_windows_reserved_title_basename(self, tmp_path):
        from modules.pipeline.stages.packaging import package_chapters

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        with patch("modules.audio.packager.M4BPackager"):
            m4b_path = package_chapters(
                self._runtime(tmp_path),
                [self._chapter_result(tmp_path)],
                "CON",
                "Author",
                output_dir,
            )

        assert m4b_path.name.casefold() != "con.m4b"
        assert m4b_path.stem.casefold() != "con"

