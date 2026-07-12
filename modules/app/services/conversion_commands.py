"""Command objects for conversion service pipeline jobs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from modules.app.services.conversion_paths import resolve_conversion_stored_path
from modules.app.services.conversion_runtime import ConversionRuntimeOptions
from modules.pipeline.models import ConversionResult as PipelineConversionResult
from modules.pipeline.models import RepairPlan, RepairResult
from modules.pipeline.orchestrator import PipelineConfig
from modules.storage.models import ConversionMode, ConversionResult, PlaybackStatus


@dataclass(frozen=True, kw_only=True)
class PipelineJobCommand:
    """Shared execution contract consumed by ConversionService."""

    job: Any
    callbacks: Any
    runtime_options: ConversionRuntimeOptions
    conversion_mode: ConversionMode | str
    failure_message: str
    invalid_artifacts_message: str
    cancel_message_text: str

    def pipeline_config_kwargs(self) -> dict[str, Any]:
        return {
            "conversion_mode": self.conversion_mode,
            "runtime_options": self.runtime_options,
        }

    def adjust_pipeline_config(self, pipeline_config: PipelineConfig) -> None:
        """Apply command-specific overrides before the pipeline is built."""
        _ = pipeline_config

    def run(
        self,
        pipeline: Any,
        pipeline_config: PipelineConfig,
    ) -> PipelineConversionResult:
        raise NotImplementedError

    def handle_success(
        self,
        service: Any,
        pipeline_result: PipelineConversionResult,
        controller_result: ConversionResult,
        pipeline_config: PipelineConfig,
    ) -> None:
        raise NotImplementedError

    def is_cancelled_result(self, pipeline_result: PipelineConversionResult) -> bool:
        return (
            not pipeline_result.success
            and (
                bool(getattr(self.job, "cancel_requested", False))
                or str(pipeline_result.error or "").strip().lower() == "cancelled"
            )
        )

    def cancel_message(self, pipeline_result: PipelineConversionResult) -> str:
        return pipeline_result.error or self.cancel_message_text

    def cleanup_first(self, pipeline: Any) -> bool:
        _ = pipeline
        return False


@dataclass(frozen=True, kw_only=True)
class ConversionRunCommand(PipelineJobCommand):
    """Command for a source-file conversion run."""

    source_path: Path
    title: str | None
    author: str | None

    def run(
        self,
        pipeline: Any,
        pipeline_config: PipelineConfig,
    ) -> PipelineConversionResult:
        _ = pipeline_config
        return pipeline.convert(
            source_path=self.source_path,
            title=self.title,
            author=self.author,
        )

    def handle_success(
        self,
        service: Any,
        pipeline_result: PipelineConversionResult,
        controller_result: ConversionResult,
        pipeline_config: PipelineConfig,
    ) -> None:
        _ = (controller_result, pipeline_config)
        service._handle_conversion_success(
            job=self.job,
            source_path=self.source_path,
            pipeline_result=pipeline_result,
            callbacks=self.callbacks,
            conversion_mode=self.conversion_mode,
            runtime_options=self.runtime_options,
        )


@dataclass(frozen=True)
class RepairContext:
    """Resolved storage state required to execute a repair command."""

    book: Any
    chapters: list[Any]
    source_path: Path
    book_output_dir: Path
    chapters_dir: Path
    resolve_chapter_audio: Callable[[Any], Path | None]
    # True when the chapters kept from the original conversion are MP3 files.
    # Repaired chapters must then also emit MP3 so packaging receives a
    # homogeneous chapter set instead of a mixed MP3/AAC one it rejects.
    requires_mp3_artifacts: bool = False


@dataclass(frozen=True, kw_only=True)
class RepairRunCommand(PipelineJobCommand):
    """Command for repairing missing chapter audio and repackaging a book."""

    book_id: int
    repair_context: RepairContext

    def adjust_pipeline_config(self, pipeline_config: PipelineConfig) -> None:
        if not self.repair_context.requires_mp3_artifacts:
            return
        pipeline_config.emit_chapter_mp3_artifacts = True
        # Chapter AAC artifacts would be ignored by MP3 packaging; skip them.
        pipeline_config.enable_chapter_aac_packaging = False

    def run(
        self,
        pipeline: Any,
        pipeline_config: PipelineConfig,
    ) -> RepairResult:
        _ = pipeline_config
        context = self.repair_context
        return pipeline.repair(
            plan=RepairPlan(
                source_path=context.source_path,
                stored_chapters=list(context.chapters),
                title=context.book.title,
                author=context.book.author,
                output_dir=context.book_output_dir,
                chapters_dir=context.chapters_dir,
                resolve_chapter_audio=context.resolve_chapter_audio,
                cancel_requested=lambda: bool(
                    getattr(self.job, "cancel_requested", False)
                ),
            ),
        )

    def handle_success(
        self,
        service: Any,
        pipeline_result: RepairResult,
        controller_result: ConversionResult,
        pipeline_config: PipelineConfig,
    ) -> None:
        _ = pipeline_config
        final_output_path = Path(pipeline_result.output_path)
        service._repository.apply_repair_result(
            self.book_id,
            repaired_chapter_updates=list(pipeline_result.repaired_chapter_updates),
            playback_path=str(final_output_path),
            playback_status=PlaybackStatus.PACKAGED,
            total_chapters=len(pipeline_result.chapters),
        )
        result = ConversionResult(
            success=True,
            book_id=self.book_id,
            output_path=str(final_output_path),
            total_duration_ms=pipeline_result.total_duration_ms,
            chapters_completed=controller_result.chapters_completed,
            chapters_total=len(pipeline_result.chapters),
        )
        # Use the synchronized job transitions (result before COMPLETED) so
        # concurrent readers never observe COMPLETED with a stale/None result.
        set_result = getattr(self.job, "set_result", None)
        if callable(set_result):
            set_result(result)
        else:
            self.job.result = result
        complete = getattr(self.job, "complete", None)
        if callable(complete):
            complete(self.book_id)
        else:
            self.job.status = service._status.COMPLETED
            self.job.book_id = self.book_id
        if self.callbacks and self.callbacks.on_event:
            from modules.app.events import JobState, make_state_event

            self.callbacks.on_event(
                make_state_event(JobState.COMPLETED, self.job.id, "repair completed")
            )


def resolve_repair_context(
    service: Any,
    *,
    book_id: int,
) -> RepairContext:
    detail = service._repository.get_book_with_chapters(book_id)
    if detail is None:
        raise ValueError("Book not found")
    book, chapters = detail

    def resolve_stored(raw_path: str | None) -> Path | None:
        return resolve_conversion_stored_path(
            raw_path,
            db_path=Path(service._config.db_path),
            output_dir=service._config.output_dir,
            temp_dir=service._config.temp_dir,
        )

    source_path = resolve_stored(getattr(book, "source_path", None))
    if source_path is None or not source_path.exists():
        raise ValueError("Original source file is missing; repair cannot rerun chapters")

    book_output_dir, chapters_dir = service._repair_output_dirs(book, chapters)

    def resolve_chapter_audio(chapter: Any) -> Path | None:
        return resolve_stored(getattr(chapter, "mp3_path", None))

    requires_mp3_artifacts = any(
        (resolved := resolve_chapter_audio(chapter)) is not None
        and resolved.suffix.lower() == ".mp3"
        and resolved.is_file()
        for chapter in chapters
    )
    return RepairContext(
        book=book,
        chapters=list(chapters),
        source_path=source_path,
        book_output_dir=book_output_dir,
        chapters_dir=chapters_dir,
        resolve_chapter_audio=resolve_chapter_audio,
        requires_mp3_artifacts=requires_mp3_artifacts,
    )
