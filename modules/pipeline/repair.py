"""Pipeline-owned repair orchestration."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .chapter_dispatch import dispatch_chapters
from .models import (
    ChapterResult,
    PipelineStage,
    RepairedChapterUpdate,
    RepairPlan,
    RepairResult,
)
from .result_validation import (
    summarize_repair_input_failures,
    validate_conversion_success_artifacts,
)
from .stages.ingestion import ingest_source
from .stages.packaging import package_chapters


@dataclass(frozen=True)
class RepairRuntime:
    """Explicit pipeline operations needed by repair orchestration."""

    begin_run: Callable[[], None]
    finish_run: Callable[[], None]
    check_cancelled: Callable[[], bool]
    set_stage: Callable[[PipelineStage], None]
    set_total_chars: Callable[[int], None]
    ingest_source: Callable[[Path], Any]
    process_missing_chapters: Callable[[list[Any], Path, int], list[ChapterResult]]
    package_results: Callable[[list[ChapterResult], str, Optional[str], Path], Path]
    cancel: Callable[[], None]


def _normalize_repair_chapter_title(title: Any) -> str:
    return " ".join(str(title or "").split()).casefold()


def _chapter_number(chapter: Any, fallback: int) -> int:
    return int(getattr(chapter, "chapter_number", getattr(chapter, "number", fallback)))


def _validate_repair_structure(document_chapters: list[Any], stored_chapters: list[Any]) -> list[str]:
    source_by_number: dict[int, Any] = {}
    duplicate_source_numbers: list[int] = []
    for index, chapter in enumerate(document_chapters):
        chapter_number = int(getattr(chapter, "number", index + 1))
        if chapter_number in source_by_number:
            duplicate_source_numbers.append(chapter_number)
        source_by_number[chapter_number] = chapter

    stored_numbers: set[int] = set()
    duplicate_stored_numbers: list[int] = []
    for chapter in stored_chapters:
        chapter_number = _chapter_number(chapter, 0)
        if chapter_number in stored_numbers:
            duplicate_stored_numbers.append(chapter_number)
        stored_numbers.add(chapter_number)

    source_numbers = set(source_by_number)
    errors: list[str] = []
    if duplicate_source_numbers:
        duplicate_numbers = ", ".join(str(number) for number in sorted(set(duplicate_source_numbers)))
        errors.append(f"duplicate source chapter rows: {duplicate_numbers}")
    if duplicate_stored_numbers:
        duplicate_numbers = ", ".join(str(number) for number in sorted(set(duplicate_stored_numbers)))
        errors.append(f"duplicate stored chapter rows: {duplicate_numbers}")

    missing_stored_numbers = sorted(source_numbers - stored_numbers)
    if missing_stored_numbers:
        missing_numbers = ", ".join(str(number) for number in missing_stored_numbers)
        errors.append(f"missing stored chapter rows for source chapters: {missing_numbers}")

    missing_source_numbers = sorted(stored_numbers - source_numbers)
    if missing_source_numbers:
        missing_numbers = ", ".join(str(number) for number in missing_source_numbers)
        errors.append(f"missing source chapter rows for stored chapters: {missing_numbers}")

    for chapter in stored_chapters:
        chapter_number = _chapter_number(chapter, 0)
        source_chapter = source_by_number.get(chapter_number)
        if source_chapter is None:
            continue
        stored_title = _normalize_repair_chapter_title(getattr(chapter, "title", ""))
        source_title = _normalize_repair_chapter_title(getattr(source_chapter, "title", ""))
        if not stored_title or not source_title or stored_title == source_title:
            continue
        errors.append(
            "title mismatch for chapter "
            f"{chapter_number}: expected {getattr(chapter, 'title', '')!r}, "
            f"got {getattr(source_chapter, 'title', '')!r}"
        )

    return errors


def _existing_repair_result(
    stored_chapter: Any,
    chapter_audio: Path,
) -> ChapterResult:
    return ChapterResult(
        chapter_number=_chapter_number(stored_chapter, 0),
        chapter_title=str(getattr(stored_chapter, "title", "")),
        mp3_path=chapter_audio,
        duration_ms=int(getattr(stored_chapter, "duration_ms", 0) or 0),
    )


def _failure_result(
    *,
    plan: RepairPlan,
    error: str,
    processing_started_at: float,
    chapters: Optional[list[ChapterResult]] = None,
) -> RepairResult:
    return RepairResult(
        success=False,
        title=plan.title,
        author=plan.author,
        chapters=chapters or [],
        total_duration_ms=sum(result.duration_ms for result in chapters or []),
        error=error,
        processing_time_seconds=time.time() - processing_started_at,
    )


def _cancelled_result(plan: RepairPlan) -> RepairResult:
    return RepairResult(success=False, title=plan.title, author=plan.author, error="Cancelled")


def _ingest_document(pipeline: Any, source_path: Path) -> Any:
    ingest = getattr(getattr(pipeline, "stages", pipeline), "ingest", None)
    if callable(ingest):
        return ingest(source_path)
    return ingest_source(pipeline, source_path)


def _process_missing_repair_chapters(
    pipeline: Any,
    missing_chapters: list[Any],
    chapters_dir: Path,
    total_chapters: int,
) -> list[ChapterResult]:
    if not missing_chapters:
        return []
    runner = getattr(pipeline, "stages", pipeline)
    can_use_dispatch = (
        hasattr(pipeline, "config")
        and hasattr(pipeline, "is_parallel")
        and callable(getattr(runner, "warmup_models", None))
        and (
            callable(getattr(runner, "process_chapters_parallel", None))
            or callable(getattr(runner, "process_chapters_sequential", None))
        )
    )
    if not can_use_dispatch:
        processor = runner.process_chapter
        results: list[ChapterResult] = []
        for source_chapter in missing_chapters:
            chapter_number = int(getattr(source_chapter, "number", len(results) + 1))
            results.append(
                processor(
                    chapter=source_chapter,
                    chapter_idx=max(0, chapter_number - 1),
                    total_chapters=total_chapters,
                    output_dir=chapters_dir,
                )
            )
        return results
    return dispatch_chapters(
        getattr(pipeline, "dispatch_context", pipeline),
        missing_chapters,
        chapters_dir,
    )


def _package_repair_results(
    pipeline: Any,
    *,
    chapter_results: list[ChapterResult],
    title: str,
    author: Optional[str],
    output_dir: Path,
) -> Path:
    package_results = getattr(pipeline, "package_results", None)
    if callable(package_results):
        return package_results(
            chapter_results=chapter_results,
            title=title,
            author=author,
            output_dir=output_dir,
        )
    package = getattr(getattr(pipeline, "stages", pipeline), "package", None)
    if callable(package):
        return package(
            chapter_results=chapter_results,
            title=title,
            author=author,
            output_dir=output_dir,
        )
    return package_chapters(
        pipeline=pipeline,
        chapter_results=chapter_results,
        title=title,
        author=author,
        output_dir=output_dir,
    )


def repair_runtime_from_pipeline(pipeline: Any) -> RepairRuntime:
    """Build a compatibility runtime from a pipeline-like object with public collaborators."""
    begin_run = getattr(pipeline, "begin_run", None)
    finish_run = getattr(pipeline, "finish_run", None)
    check_cancelled = getattr(pipeline, "check_cancelled", None)
    set_stage = getattr(pipeline, "set_stage", None)
    set_total_chars = getattr(pipeline, "set_total_chars", None)
    cancel = getattr(pipeline, "cancel", None)
    runner = getattr(pipeline, "stages", pipeline)
    cleanup_models = getattr(runner, "cleanup_models", None)
    fallback_finish_run = (
        (lambda: cleanup_models(force=False))
        if callable(cleanup_models)
        else (lambda: None)
    )
    return RepairRuntime(
        begin_run=begin_run if callable(begin_run) else lambda: None,
        finish_run=finish_run if callable(finish_run) else fallback_finish_run,
        check_cancelled=check_cancelled if callable(check_cancelled) else lambda: False,
        set_stage=set_stage if callable(set_stage) else lambda stage: None,
        set_total_chars=set_total_chars if callable(set_total_chars) else lambda total_chars: None,
        ingest_source=lambda source_path: _ingest_document(pipeline, source_path),
        process_missing_chapters=lambda chapters, output_dir, total_chapters: _process_missing_repair_chapters(
            pipeline,
            chapters,
            output_dir,
            total_chapters,
        ),
        package_results=lambda chapter_results, title, author, output_dir: _package_repair_results(
            pipeline,
            chapter_results=chapter_results,
            title=title,
            author=author,
            output_dir=output_dir,
        ),
        cancel=cancel if callable(cancel) else lambda: None,
    )


def _coerce_plan(
    plan: RepairPlan | None,
    *,
    source_path: Path | None = None,
    stored_chapters: list[Any] | None = None,
    title: str | None = None,
    author: Optional[str] = None,
    output_dir: Path | None = None,
    chapters_dir: Path | None = None,
    resolve_chapter_audio=None,
    cancel_requested=None,
) -> RepairPlan:
    if plan is not None:
        return plan
    if (
        source_path is None
        or stored_chapters is None
        or title is None
        or output_dir is None
        or chapters_dir is None
        or resolve_chapter_audio is None
    ):
        raise ValueError("repair_conversion requires a RepairPlan or complete repair inputs")
    return RepairPlan(
        source_path=Path(source_path),
        stored_chapters=stored_chapters,
        title=title,
        author=author,
        output_dir=output_dir,
        chapters_dir=chapters_dir,
        resolve_chapter_audio=resolve_chapter_audio,
        cancel_requested=cancel_requested,
    )


def execute_repair_plan(
    runtime: RepairRuntime,
    plan: RepairPlan,
) -> RepairResult:
    """Repair missing chapters and repackage using explicit runtime operations."""
    pipeline_start = time.time()
    runtime.begin_run()
    try:
        if runtime.check_cancelled():
            runtime.set_stage(PipelineStage.CANCELLED)
            return _cancelled_result(plan)

        runtime.set_stage(PipelineStage.INGESTING)
        document = runtime.ingest_source(plan.source_path)
        document_chapters = list(getattr(document, "chapters", []))
        stored_chapters = sorted(plan.stored_chapters, key=lambda item: _chapter_number(item, 0))
        structure_errors = _validate_repair_structure(document_chapters, stored_chapters)
        if structure_errors:
            runtime.set_stage(PipelineStage.ERROR)
            return _failure_result(
                plan=plan,
                error="Repair chapter structure mismatch. " + "; ".join(structure_errors),
                processing_started_at=pipeline_start,
            )

        plan.output_dir.mkdir(parents=True, exist_ok=True)
        plan.chapters_dir.mkdir(parents=True, exist_ok=True)
        source_by_number = {
            int(getattr(chapter, "number", index + 1)): chapter
            for index, chapter in enumerate(document_chapters)
        }
        runtime.set_total_chars(
            sum(len(str(getattr(chapter, "content", ""))) for chapter in document_chapters),
        )

        results_by_number: dict[int, ChapterResult] = {}
        repaired_updates: list[RepairedChapterUpdate] = []
        stored_by_number = {
            _chapter_number(stored_chapter, index + 1): stored_chapter
            for index, stored_chapter in enumerate(stored_chapters)
        }
        missing_chapters: list[Any] = []

        for stored_chapter in stored_chapters:
            chapter_number = _chapter_number(stored_chapter, 0)
            chapter_audio = plan.resolve_chapter_audio(stored_chapter)
            if chapter_audio is not None and chapter_audio.exists() and chapter_audio.is_file():
                results_by_number[chapter_number] = _existing_repair_result(stored_chapter, chapter_audio)
                continue

            if plan.cancel_requested is not None and plan.cancel_requested():
                runtime.cancel()
                runtime.set_stage(PipelineStage.CANCELLED)
                return _cancelled_result(plan)

            source_chapter = source_by_number.get(chapter_number)
            if source_chapter is None:
                results_by_number[chapter_number] = ChapterResult(
                    chapter_number=chapter_number,
                    chapter_title=str(getattr(stored_chapter, "title", "")),
                    error="Source chapter not found during repair",
                )
                continue
            missing_chapters.append(source_chapter)

        total_chapters = max(len(document_chapters), len(stored_chapters))
        for repaired in runtime.process_missing_chapters(
            missing_chapters,
            plan.chapters_dir,
            total_chapters,
        ):
            chapter_number = int(getattr(repaired, "chapter_number", 0) or 0)
            results_by_number[chapter_number] = repaired
            if str(repaired.error or "").strip().lower() == "cancelled":
                runtime.set_stage(PipelineStage.CANCELLED)
                return _cancelled_result(plan)
            stored_chapter = stored_by_number.get(chapter_number)
            if stored_chapter is not None and not repaired.error and repaired.mp3_path is not None:
                repaired_updates.append(
                    RepairedChapterUpdate(
                        chapter_id=int(getattr(stored_chapter, "id")),
                        duration_ms=repaired.duration_ms,
                        mp3_path=str(repaired.mp3_path),
                    )
                )

        for stored_chapter in stored_chapters:
            chapter_number = _chapter_number(stored_chapter, 0)
            if chapter_number not in results_by_number:
                results_by_number[chapter_number] = ChapterResult(
                    chapter_number=chapter_number,
                    chapter_title=str(getattr(stored_chapter, "title", "")),
                    error="Repair did not return chapter result",
                )

        results = [
            results_by_number[_chapter_number(stored_chapter, 0)]
            for stored_chapter in stored_chapters
        ]
        incomplete_notes = summarize_repair_input_failures(results)
        if incomplete_notes:
            runtime.set_stage(PipelineStage.ERROR)
            return _failure_result(
                plan=plan,
                chapters=results,
                error="Repair incomplete. " + "; ".join(incomplete_notes[:3]),
                processing_started_at=pipeline_start,
            )

        final_output_path = runtime.package_results(
            results,
            plan.title,
            plan.author,
            plan.output_dir,
        )
        result = RepairResult(
            success=True,
            title=plan.title,
            author=plan.author,
            output_path=Path(final_output_path),
            chapters=results,
            total_duration_ms=sum(chapter.duration_ms for chapter in results),
            processing_time_seconds=time.time() - pipeline_start,
            repaired_chapter_updates=repaired_updates,
        )
        artifact_validation = validate_conversion_success_artifacts(
            result,
            emit_chapter_mp3_artifacts=False,
            require_packaged_output=True,
        )
        if not artifact_validation.is_valid:
            notes = list(artifact_validation.chapter_notes)
            if artifact_validation.packaged_output_note is not None:
                notes.insert(0, artifact_validation.packaged_output_note)
            runtime.set_stage(PipelineStage.ERROR)
            return _failure_result(
                plan=plan,
                chapters=results,
                error="Repair reported invalid artifacts. " + "; ".join(notes[:3]),
                processing_started_at=pipeline_start,
            )
        runtime.set_stage(PipelineStage.COMPLETE)
        return result
    except Exception as exc:
        runtime.set_stage(PipelineStage.ERROR)
        return _failure_result(
            plan=plan,
            error=str(exc),
            processing_started_at=pipeline_start,
        )
    finally:
        runtime.finish_run()


def repair_conversion(
    pipeline: Any,
    plan: RepairPlan | None = None,
    **legacy_plan_kwargs: Any,
) -> RepairResult:
    """Compatibility wrapper for pipeline-like test doubles."""
    plan = _coerce_plan(plan, **legacy_plan_kwargs)
    try:
        from .orchestrator import ConversionPipeline
    except ImportError:
        ConversionPipeline = None
    if ConversionPipeline is not None and isinstance(pipeline, ConversionPipeline):
        return pipeline.repair(plan=plan)
    return execute_repair_plan(repair_runtime_from_pipeline(pipeline), plan)
