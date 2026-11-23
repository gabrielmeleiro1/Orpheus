"""Pipeline-owned repair orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .models import (
    ChapterResult,
    PipelineStage,
    RepairedChapterUpdate,
    RepairPlan,
    RepairResult,
)
from .result_validation import (
    format_failure_notes,
    summarize_repair_input_failures,
    validate_chapter_sets,
    validate_conversion_success_artifacts,
)
from .stages.packaging import resolve_background_audio_futures


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
    # Returns False when workers may still be writing chapter outputs.
    shutdown_workers: Optional[Callable[[], bool]] = None


def _chapter_number(chapter: Any, fallback: int) -> int:
    return int(getattr(chapter, "chapter_number", getattr(chapter, "number", fallback)))


def _existing_repair_result(
    stored_chapter: Any,
    chapter_audio: Path,
    *,
    cancel_requested: Optional[Callable[[], bool]] = None,
) -> ChapterResult:
    # Classify the stored audio by container so packaging homogeneity checks
    # compare kept chapters against repaired ones on the right field.
    suffix = chapter_audio.suffix.lower()
    audio_field = "mp3_path"
    if suffix == ".m4a":
        audio_field = "aac_path"
    elif suffix == ".wav":
        audio_field = "wav_path"
    from modules.audio.encoder import AudioEncoder

    # A DB duration records the original encode, not the current file's health.
    # Probe retained files even after a checkpoint persisted their duration.
    probed_duration_ms = round(
        AudioEncoder().get_duration(chapter_audio, cancel_requested=cancel_requested) * 1000
    )
    if probed_duration_ms <= 0:
        raise ValueError(f"Chapter audio has no playable duration: {chapter_audio.name}")
    duration_ms = int(getattr(stored_chapter, "duration_ms", 0) or 0)
    if duration_ms <= 0:
        duration_ms = probed_duration_ms
    return ChapterResult(
        chapter_number=_chapter_number(stored_chapter, 0),
        chapter_title=str(getattr(stored_chapter, "title", "")),
        duration_ms=duration_ms,
        **{audio_field: chapter_audio},
    )


def _failure_result(
    *,
    plan: RepairPlan,
    error: str,
    chapters: Optional[list[ChapterResult]] = None,
) -> RepairResult:
    return RepairResult(
        success=False,
        title=plan.title,
        author=plan.author,
        chapters=chapters or [],
        total_duration_ms=sum(result.duration_ms for result in chapters or []),
        error=error,
    )


def _cancelled_result(plan: RepairPlan) -> RepairResult:
    return RepairResult(success=False, title=plan.title, author=plan.author, error="Cancelled")


def execute_repair_plan(
    runtime: RepairRuntime,
    plan: RepairPlan,
) -> RepairResult:
    """Repair missing chapters and repackage using explicit runtime operations."""

    def cancel_requested() -> bool:
        if plan.cancel_requested is not None and plan.cancel_requested():
            return True
        return bool(runtime.check_cancelled())

    runtime.begin_run()
    try:
        if runtime.check_cancelled():
            runtime.set_stage(PipelineStage.CANCELLED)
            return _cancelled_result(plan)

        runtime.set_stage(PipelineStage.INGESTING)
        document = runtime.ingest_source(plan.source_path)
        document_chapters = list(getattr(document, "chapters", []))
        stored_chapters = sorted(plan.stored_chapters, key=lambda item: _chapter_number(item, 0))
        structure_errors = validate_chapter_sets(
            document_chapters,
            stored_chapters,
            context="repair",
        )
        if structure_errors:
            runtime.set_stage(PipelineStage.ERROR)
            return _failure_result(
                plan=plan,
                error="Repair chapter structure mismatch. " + "; ".join(structure_errors),
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
        repaired_records: list[tuple[Any, ChapterResult]] = []
        stored_by_number = {
            _chapter_number(stored_chapter, index + 1): stored_chapter
            for index, stored_chapter in enumerate(stored_chapters)
        }
        missing_chapters: list[Any] = []

        for stored_chapter in stored_chapters:
            if plan.cancel_requested is not None and plan.cancel_requested():
                runtime.cancel()
                runtime.set_stage(PipelineStage.CANCELLED)
                return _cancelled_result(plan)

            chapter_number = _chapter_number(stored_chapter, 0)
            chapter_audio = plan.resolve_chapter_audio(stored_chapter)
            if chapter_audio is not None and chapter_audio.exists() and chapter_audio.is_file():
                try:
                    kept = _existing_repair_result(
                        stored_chapter, chapter_audio, cancel_requested=cancel_requested,
                    )
                except (OSError, RuntimeError, ValueError, KeyError):
                    if cancel_requested():
                        # A cancelled probe says nothing about the file's health.
                        runtime.cancel()
                        runtime.set_stage(PipelineStage.CANCELLED)
                        return _cancelled_result(plan)
                    # Unreadable/empty audio is a missing chapter for repair.
                    # Keep it on disk until the encoder atomically replaces it.
                else:
                    results_by_number[chapter_number] = kept
                    if not getattr(stored_chapter, "duration_ms", None) or not getattr(stored_chapter, "mp3_path", None):
                        # Recovered atomic artifacts may predate their DB checkpoint.
                        kept.mp3_path = chapter_audio
                        repaired_records.append((stored_chapter, kept))
                    continue

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
            if stored_chapter is not None and not repaired.error:
                # Defer building the update until futures are resolved: in the
                # background-AAC default the chapter returns with mp3_path=None
                # and only aac_future set, and resolving it backfills the
                # durable .m4a onto mp3_path. Capturing mp3_path here would
                # persist None, dropping the durable reference and re-triggering
                # repair forever.
                repaired_records.append((stored_chapter, repaired))

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
        resolve_background_audio_futures(results)
        incomplete_notes = summarize_repair_input_failures(results)
        if incomplete_notes:
            runtime.set_stage(PipelineStage.ERROR)
            return _failure_result(
                plan=plan,
                chapters=results,
                error="Repair incomplete. " + format_failure_notes(incomplete_notes),
            )

        final_output_path = runtime.package_results(
            results,
            plan.title,
            plan.author,
            plan.output_dir,
        )
        # Background AAC futures have been resolved, so mp3_path
        # reflects the durable per-chapter audio (.m4a or .mp3).
        repaired_updates = [
            RepairedChapterUpdate(
                chapter_id=int(getattr(stored_chapter, "id")),
                duration_ms=repaired.duration_ms,
                mp3_path=(
                    str(repaired.mp3_path)
                    if repaired.mp3_path is not None
                    else None
                ),
            )
            for stored_chapter, repaired in repaired_records
        ]
        result = RepairResult(
            success=True,
            title=plan.title,
            author=plan.author,
            output_path=Path(final_output_path),
            chapters=results,
            total_duration_ms=sum(chapter.duration_ms for chapter in results),
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
                error="Repair reported invalid artifacts. " + format_failure_notes(notes),
            )
        runtime.set_stage(PipelineStage.COMPLETE)
        return result
    except Exception as exc:
        if cancel_requested():
            # Stages abort a cancelled subprocess by raising; that is the
            # requested cancellation, not a repair failure.
            runtime.set_stage(PipelineStage.CANCELLED)
            return _cancelled_result(plan)
        runtime.set_stage(PipelineStage.ERROR)
        return _failure_result(
            plan=plan,
            error=str(exc),
        )
    finally:
        was_cancelled = cancel_requested()
        workers_stopped = True
        if was_cancelled and runtime.shutdown_workers is not None:
            workers_stopped = runtime.shutdown_workers() is not False
        runtime.finish_run()
        if was_cancelled and workers_stopped:
            from modules.audio.ffmpeg import cleanup_abandoned_atomic_outputs

            for directory in (plan.output_dir, plan.chapters_dir):
                cleanup_abandoned_atomic_outputs(directory)
