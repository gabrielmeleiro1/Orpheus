"""Public conversion stage facade over focused runtime collaborators."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .chapter_processor import ChapterProcessor
from .chapter_runners import ParallelChapterRunner, SequentialChapterRunner
from .model_session import ModelSession
from .models import ChapterResult
from .runtime import PipelineRuntimeProtocol


class PipelineStages:
    """Small public stage contract used by ``ConversionPipeline``."""

    def __init__(self, runtime: PipelineRuntimeProtocol) -> None:
        self.runtime = runtime
        self._chapter_processor = ChapterProcessor(self.runtime)
        self._sequential_runner = SequentialChapterRunner(
            self.runtime,
            self._chapter_processor,
        )
        self._parallel_runner = ParallelChapterRunner(
            self.runtime,
            self._chapter_processor,
            self._sequential_runner,
        )
        self._model_session = ModelSession(self.runtime)

    @property
    def chapter_processor(self) -> ChapterProcessor:
        """Public chapter-stage boundary for tests and focused orchestration."""
        return self._chapter_processor

    def begin_stage_timing_capture(
        self,
        stage_timings: dict[str, float],
    ) -> dict[str, float] | None:
        return self._chapter_processor.begin_stage_timing_capture(stage_timings)

    def restore_stage_timing_capture(
        self,
        previous_stage_timings: dict[str, float] | None,
    ) -> None:
        self._chapter_processor.restore_stage_timing_capture(previous_stage_timings)

    def process_chapters_sequential(
        self,
        chapters: list[Any],
        output_dir: Path,
    ) -> list[ChapterResult]:
        return self._sequential_runner.process_chapters(chapters, output_dir)

    def process_chapters_parallel(
        self,
        chapters: list[Any],
        output_dir: Path,
    ) -> list[ChapterResult]:
        return self._parallel_runner.process_chapters(chapters, output_dir)

    def process_chapter(
        self,
        chapter: Any,
        chapter_idx: int,
        total_chapters: int,
        output_dir: Path,
    ) -> ChapterResult:
        return self._chapter_processor.process_chapter(
            chapter=chapter,
            chapter_idx=chapter_idx,
            total_chapters=total_chapters,
            output_dir=output_dir,
        )

    @staticmethod
    def process_chapter_worker(
        chapter: Any,
        chapter_idx: int,
        output_dir: Path,
        total_chapters: Optional[int] = None,
        tts_engine: Any = None,
        text_cleaner: Any = None,
    ) -> ChapterResult:
        return ChapterProcessor.process_chapter_worker(
            chapter=chapter,
            chapter_idx=chapter_idx,
            output_dir=output_dir,
            total_chapters=total_chapters,
            tts_engine=tts_engine,
            text_cleaner=text_cleaner,
        )

    def ingest(self, source_path: Path):
        from .stages.ingestion import ingest_source

        return ingest_source(self.runtime, source_path)

    def save_intermediate_source(self, document: Any, output_dir: Path) -> None:
        from .stages.ingestion import save_intermediate_source

        save_intermediate_source(self.runtime, document, output_dir)

    def package(
        self,
        chapter_results: list[ChapterResult],
        title: str,
        author: Optional[str],
        output_dir: Path,
    ) -> Path:
        from .stages.packaging import package_chapters

        return package_chapters(
            runtime=self.runtime,
            chapter_results=chapter_results,
            title=title,
            author=author,
            output_dir=output_dir,
        )

    def warmup_models(self) -> None:
        self._model_session.warmup_models()

    def cleanup_models(self, *, force: bool = False) -> None:
        self._model_session.cleanup_models(force=force)

    def cancel_tts_idle_unload(self) -> None:
        self._model_session.cancel_tts_idle_unload()

    def schedule_tts_idle_unload(self) -> None:
        self._model_session.schedule_tts_idle_unload()
