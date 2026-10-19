"""
Conversion Pipeline Orchestrator
=================================
Coordinates all modules for audiobook conversion:
Ingest → Chunker → TTS → Audio Processor → M4B Packager

Supports both sequential and parallel processing modes:
- Sequential: Better for single-chapter books, debugging
- Parallel: Process multiple chapters concurrently with VRAM management
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Optional, Callable, Union, Any

from .book_workspace import initialize_book_workspace
from .chapter_dispatch import ChapterDispatchContext, dispatch_chapters
from .conversion_lifecycle import (
    begin_conversion_run,
    cancelled_result,
    finish_conversion_run,
)
from .models import (
    ChapterProgressCallback as ChapterProgressCallback,
    ChapterResult as ChapterResult,
    ConversionResult,
    PipelineConfig,
    PipelineStage,
    ProgressCallback,
    RepairedChapterUpdate as RepairedChapterUpdate,
    RepairPlan,
    RepairResult,
    VerboseCallback,
)
from .progress import PipelineProgressTracker
from .pipeline_stages import PipelineStages
from .result_validation import (
    format_failure_notes,
    summarize_incomplete_chapters,
    validate_conversion_success_artifacts,
    validate_chapter_result_structure,
)
from .worker_config import (
    build_shared_pool_fingerprint,
    build_worker_init_config,
)
from .runtime import PipelineRunState, PipelineRuntime

# Import parallel processing modules
try:
    from .parallel import (
        ParallelConfig,
        create_parallel_config,
        _detect_available_ram_gb,
        get_worker_cached_config,
        get_worker_cached_models,
    )

    PARALLEL_AVAILABLE = True
except ImportError:
    PARALLEL_AVAILABLE = False

    def _detect_available_ram_gb() -> float:
        return 8.0

    def get_worker_cached_models() -> tuple[Any, Any]:
        return None, None

    def get_worker_cached_config() -> Any:
        return None


logger = logging.getLogger(__name__)


# Uploaded and pasted sources are stored as "<timestamp>_<hash>_<name>.<ext>".
_UPLOAD_HANDLE_PREFIX = re.compile(r"^\d{6,}_[0-9a-f]{6,}(?:_|$)")


def _display_source_name(source_path: Path) -> str:
    """The user's file name, without the upload handle the app stores it under."""
    name = _UPLOAD_HANDLE_PREFIX.sub("", source_path.stem).strip()
    return f"{name}{source_path.suffix}" if name else f"the {source_path.suffix.lstrip('.').upper()} source"


def _remove_temp_artifact(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file():
            path.unlink()
    except OSError:
        logger.debug("Could not remove temporary artifact %s", path, exc_info=True)


class _RunStateField:
    """Compatibility descriptor for legacy private-field tests and callers."""

    def __init__(
        self,
        name: str,
        *,
        coerce: Callable[[Any], Any] | None = None,
    ) -> None:
        self.name = name
        self.coerce = coerce

    def __get__(self, instance: Any, _owner: Any) -> Any:
        if instance is None:
            return self
        return getattr(instance._run_state, self.name)

    def __set__(self, instance: Any, value: Any) -> None:
        if self.coerce is not None:
            value = self.coerce(value)
        setattr(instance._run_state, self.name, value)


def _coerce_worker_count(value: Any) -> int:
    return max(1, int(value or 1))



class ConversionPipeline:
    """
    Orchestrates the full audiobook conversion pipeline.

    Supports both sequential and parallel processing:
    - Sequential: Process chapters one by one (good for single-chapter books)
    - Parallel: Process multiple chapters concurrently with VRAM management

    Stages:
        1. INGESTING - Parse PDF/EPUB, extract chapters
        2. CHUNKING - Split text into TTS-sized chunks
        3. CLEANING - LLM-based text normalization
        4. SYNTHESIZING - Generate audio per chunk
        5. ENCODING - Normalize and encode to MP3
        6. PACKAGING - Create M4B with chapters
    """

    _tts_engine = _RunStateField("tts_engine")
    _selected_parallel_chapters = _RunStateField(
        "selected_parallel_chapters",
        coerce=_coerce_worker_count,
    )

    def __init__(
        self,
        config: Optional[PipelineConfig] = None,
        progress_callback: Optional[ProgressCallback] = None,
        verbose_callback: Optional[VerboseCallback] = None,
    ):
        """
        Initialize the pipeline.

        Args:
            config: Pipeline configuration
            progress_callback: Called on progress updates
            verbose_callback: Called on detailed log updates
        """
        self.config = config or PipelineConfig()
        self.progress_callback = progress_callback
        self.verbose_callback = verbose_callback

        # Initialize parallel processing if available
        self._parallel_config: Optional[ParallelConfig] = None
        worker_plan = self.config.worker_plan

        if PARALLEL_AVAILABLE and worker_plan.enable_parallel:
            self._parallel_config = create_parallel_config(
                max_workers=worker_plan.max_workers,
                total_vram_gb=self.config.total_vram_gb,
                enforce_vram_budget=worker_plan.enforce_vram_budget,
            )
            self._parallel_config.worker_init_config = self._build_worker_init_config()

        # Ensure directories exist
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self.config.temp_dir.mkdir(parents=True, exist_ok=True)

        initial_parallel_chapters = max(
            1,
            int(
                self._parallel_config.max_workers
                if self._parallel_config is not None
                else worker_plan.max_workers
                or 1
            ),
        )
        self._run_state = PipelineRunState(
            selected_parallel_chapters=initial_parallel_chapters
        )
        self._progress = PipelineProgressTracker(self._run_state)
        self._runtime = PipelineRuntime(
            config=self.config,
            state=self._run_state,
            progress=self._progress,
            get_progress_callback=lambda: self.progress_callback,
            get_verbose_callback=lambda: self.verbose_callback,
            is_parallel_enabled=lambda: PARALLEL_AVAILABLE
            and self.config.worker_plan.enable_parallel,
            parallel_config_provider=lambda: self._parallel_config,
            shared_pool_fingerprint_builder=self._shared_pool_fingerprint,
            worker_init_config_builder=self._build_worker_init_config,
        )
        self._stages = PipelineStages(self._runtime)
        self.dispatch_context = ChapterDispatchContext(
            runtime=self._runtime,
            runner=self._stages,
        )

    @property
    def stage(self) -> PipelineStage:
        """Current pipeline stage."""
        return self._run_state.stage

    @stage.setter
    def stage(self, value: PipelineStage) -> None:
        self._run_state.stage = value

    @property
    def is_running(self) -> bool:
        """Check if pipeline is currently running."""
        return self._runtime.is_running

    @property
    def is_parallel(self) -> bool:
        """Check if parallel processing is enabled and available."""
        return self._runtime.is_parallel

    def set_recovery_callback(self, callback: Callable[[Path, ConversionResult], None]) -> None:
        """Register durable library checkpoints for application-owned conversions."""
        self._recovery_callback = callback

    def cancel(self):
        """Request pipeline cancellation."""
        self._run_state.cancelled = True

    def _check_cancelled(self) -> bool:
        """Check if cancellation was requested."""
        return self._runtime.check_cancelled()

    def _begin_conversion_run(self) -> None:
        """Reset per-run lifecycle state before conversion starts."""
        begin_conversion_run(self._runtime)

    def _finish_conversion_run(self) -> None:
        """Release per-run resources after conversion exits."""
        finish_conversion_run(
            self._runtime,
            cleanup_models=self.stages.cleanup_models,
            cleanup_temp=self._cleanup_temp,
        )

    @staticmethod
    def _cancelled_result(
        title: str = "",
        author: Optional[str] = None,
    ) -> ConversionResult:
        """Build the standard cancelled conversion result."""
        return cancelled_result(title=title, author=author)

    def _notify_progress(
        self,
        chapter_idx: int = 0,
        total_chapters: int = 0,
        chunk_idx: int = 0,
        total_chunks: int = 0,
        message: str = "",
        performance: Optional[dict[str, Any]] = None,
    ):
        """Send progress update via callback."""
        self._runtime.notify_progress(
            chapter_idx=chapter_idx,
            total_chapters=total_chapters,
            chunk_idx=chunk_idx,
            total_chunks=total_chunks,
            message=message,
            performance=performance,
        )

    def estimate_eta(self) -> Optional[float]:
        """
        Estimate remaining time in seconds.

        Returns:
            Estimated seconds remaining, or None if not enough data
        """
        return self._progress.estimate_eta()

    def _build_worker_init_config(self) -> dict[str, Any]:
        """Serialize worker model settings used for per-process load and warm-up."""
        return build_worker_init_config(self.config)

    def _shared_pool_fingerprint(self) -> tuple[Any, ...]:
        """Stable fingerprint for shared worker-pool reuse decisions."""
        max_workers = (
            self._run_state.selected_parallel_chapters
            if self._parallel_config is not None
            else self.config.worker_plan.max_workers
        )
        return build_shared_pool_fingerprint(self.config, max_workers=max_workers)

    def convert(
        self,
        source_path: Union[Path, str],
        title: Optional[str] = None,
        author: Optional[str] = None,
    ) -> ConversionResult:
        """
        Run the full conversion pipeline.

        Automatically selects parallel or sequential processing based on:
        - Configuration settings
        - Number of chapters
        - Available VRAM

        Args:
            source_path: Path to PDF or EPUB file
            title: Override book title
            author: Override author name

        Returns:
            ConversionResult with output paths and status
        """
        source_path = Path(source_path)
        self._begin_conversion_run()
        self._run_state.source_path = source_path
        recovery_source = None
        recovery_registered = False
        workspace = None
        chapter_results = []
        book_title, book_author = title or "", author
        recovery_callback = getattr(self, "_recovery_callback", None)
        try:
            if self._check_cancelled():
                return self._cancelled_result()

            # Stage 1: Ingest
            self._runtime.set_stage(PipelineStage.INGESTING)
            self._notify_progress(
                message=f"Reading {title or _display_source_name(source_path)}...",
            )

            document = self.stages.ingest(source_path)
            if self._check_cancelled():
                return self._cancelled_result()

            # Use provided or parsed title/author
            book_title = title or document.title
            book_author = author or getattr(document, "author", None)

            # Calculate total characters for ETA
            self._progress.set_total_chars(
                sum(len(ch.content) for ch in document.chapters)
            )

            workspace = initialize_book_workspace(self, document, book_title)
            book_output_dir = workspace.output_dir
            chapters_dir = workspace.chapters_dir

            if recovery_callback is not None:
                from .recovery import prepare_recovery_source, recovery_snapshot

                recovery_source = prepare_recovery_source(
                    source_path, workspace, document.chapters,
                    emit_mp3=self.config.emit_chapter_mp3_artifacts,
                )
                recovery_callback(recovery_source, recovery_snapshot(
                    recovery_source, title=book_title, author=book_author, results=[],
                ))
                recovery_registered = True

            total_chapters = len(document.chapters)
            chapter_results = dispatch_chapters(
                self.dispatch_context,
                document.chapters,
                chapters_dir,
            )

            if self._check_cancelled():
                return ConversionResult(
                    success=False, title=book_title, author=book_author,
                    chapters=chapter_results, error="Cancelled",
                    total_duration_ms=sum(result.duration_ms for result in chapter_results),
                )

            chapter_results, structural_mismatch_notes = (
                validate_chapter_result_structure(
                    document.chapters,
                    chapter_results,
                )
            )
            if structural_mismatch_notes:
                failure_summary = format_failure_notes(structural_mismatch_notes)
                self._log_verbose(
                    "[STATUS] Chapter result structural mismatch before packaging: "
                    f"{failure_summary}",
                    "error",
                )
                self._runtime.set_stage(PipelineStage.ERROR)
                return ConversionResult(
                    success=False,
                    title=book_title,
                    author=book_author,
                    chapters=chapter_results,
                    total_duration_ms=sum(r.duration_ms for r in chapter_results),
                    error=f"Structural mismatch; packaging skipped. {failure_summary}",
                )

            incomplete_chapter_notes = summarize_incomplete_chapters(chapter_results)
            if incomplete_chapter_notes:
                failure_summary = format_failure_notes(incomplete_chapter_notes)
                self._log_verbose(
                    f"[STATUS] {len(incomplete_chapter_notes)}/{len(chapter_results)} chapters incomplete before packaging: "
                    f"{failure_summary}",
                    "error",
                )
                self._runtime.set_stage(PipelineStage.ERROR)
                return ConversionResult(
                    success=False,
                    title=book_title,
                    author=book_author,
                    chapters=chapter_results,
                    total_duration_ms=sum(r.duration_ms for r in chapter_results),
                    error=f"Incomplete conversion; packaging skipped. {failure_summary}",
                )

            # Stage 5: Package final audio artifact
            self._runtime.set_stage(PipelineStage.PACKAGING)
            final_format = (
                "M4A document audio"
                if self.config.conversion_mode == "document_audio"
                else "M4B audiobook"
            )
            self._notify_progress(
                chapter_idx=total_chapters,
                total_chapters=total_chapters,
                message=f"Creating {final_format}...",
            )

            final_output_path = self.stages.package(
                chapter_results=chapter_results,
                title=book_title,
                author=book_author,
                output_dir=book_output_dir,
            )
            total_duration = sum(r.duration_ms for r in chapter_results)
            success_result = ConversionResult(
                success=True,
                title=book_title,
                author=book_author,
                output_path=final_output_path,
                chapters=chapter_results,
                total_duration_ms=total_duration,
            )
            artifact_validation = validate_conversion_success_artifacts(
                success_result,
                emit_chapter_mp3_artifacts=self.config.emit_chapter_mp3_artifacts,
                require_packaged_output=True,
            )
            if not artifact_validation.is_valid:
                failure_notes = list(artifact_validation.chapter_notes)
                if artifact_validation.packaged_output_note is not None:
                    failure_notes.insert(0, artifact_validation.packaged_output_note)
                failure_summary = format_failure_notes(failure_notes)
                self._runtime.set_stage(PipelineStage.ERROR)
                return ConversionResult(
                    success=False,
                    title=book_title,
                    author=book_author,
                    chapters=chapter_results,
                    total_duration_ms=total_duration,
                    error=f"Invalid conversion artifacts. {failure_summary}",
                )

            # Complete
            self._runtime.set_stage(PipelineStage.COMPLETE)
            self._notify_progress(
                chapter_idx=total_chapters,
                total_chapters=total_chapters,
                message="Conversion complete!",
            )

            return success_result

        except Exception as e:
            if self._check_cancelled():
                # Stages abort a cancelled subprocess (FFmpeg, probes) by raising;
                # that is the requested cancellation, not a conversion failure.
                logger.info("Pipeline conversion cancelled: %s", e)
                return ConversionResult(
                    success=False,
                    title=book_title,
                    author=book_author,
                    chapters=chapter_results,
                    error="Cancelled",
                )
            self._runtime.set_stage(PipelineStage.ERROR)
            logger.exception("Pipeline conversion failed")
            return ConversionResult(
                success=False,
                title=book_title,
                author=book_author,
                chapters=chapter_results,
                error=str(e),
            )
        finally:
            was_cancelled = self._check_cancelled()
            pool_stopped = True
            if was_cancelled:
                manager = self.config.shared_worker_pool_manager
                shutdown = getattr(manager, "shutdown", None)
                if callable(shutdown):
                    pool_stopped = shutdown() is not False
            self._finish_conversion_run()
            if was_cancelled and pool_stopped and workspace is not None:
                from modules.audio.ffmpeg import cleanup_abandoned_atomic_outputs

                for directory in (workspace.output_dir, workspace.chapters_dir):
                    cleanup_abandoned_atomic_outputs(directory)
            if recovery_registered:
                try:
                    recovery_callback(recovery_source, recovery_snapshot(
                        recovery_source, title=book_title, author=book_author,
                        results=chapter_results,
                    ))
                except Exception:
                    # The committed inventory still permits repair after restart.
                    # Keep the original failure/cancellation; successful runs must
                    # still pass the service's atomic final publication below.
                    logger.warning("Could not refresh recovery checkpoint", exc_info=True)

    def repair(
        self,
        plan: RepairPlan | None = None,
        *,
        source_path: Union[Path, str] | None = None,
        stored_chapters: list[Any] | None = None,
        title: str | None = None,
        author: Optional[str] = None,
        output_dir: Path | None = None,
        chapters_dir: Path | None = None,
        resolve_chapter_audio: Callable[[Any], Optional[Path]] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> RepairResult:
        """Repair missing chapter audio and repackage through the pipeline layer."""
        from .repair import RepairRuntime, execute_repair_plan

        if plan is None:
            if (
                source_path is None
                or stored_chapters is None
                or title is None
                or output_dir is None
                or chapters_dir is None
                or resolve_chapter_audio is None
            ):
                raise ValueError("Repair requires a RepairPlan or complete repair inputs")
            plan = RepairPlan(
                source_path=Path(source_path),
                stored_chapters=stored_chapters,
                title=title,
                author=author,
                output_dir=output_dir,
                chapters_dir=chapters_dir,
                resolve_chapter_audio=resolve_chapter_audio,
                cancel_requested=cancel_requested,
            )

        self._run_state.source_path = Path(plan.source_path)

        def set_stage(stage: PipelineStage) -> None:
            self._runtime.set_stage(stage)

        def set_total_chars(total_chars: int) -> None:
            self._progress.set_total_chars(max(0, int(total_chars or 0)))

        runtime = RepairRuntime(
            begin_run=self._begin_conversion_run,
            finish_run=self._finish_conversion_run,
            check_cancelled=self._check_cancelled,
            set_stage=set_stage,
            set_total_chars=set_total_chars,
            ingest_source=self.stages.ingest,
            process_missing_chapters=lambda chapters, output_dir, total_chapters: dispatch_chapters(
                self.dispatch_context,
                chapters,
                output_dir,
            ),
            package_results=lambda chapter_results, package_title, package_author, package_output_dir: self.stages.package(
                chapter_results=chapter_results,
                title=package_title,
                author=package_author,
                output_dir=package_output_dir,
            ),
            cancel=self.cancel,
        )
        return execute_repair_plan(runtime, plan)

    @property
    def stages(self):
        """Pipeline stage execution collaborator."""
        return self._stages

    def _cleanup_temp(self) -> None:
        """Best-effort removal of this run's temporary chapter artifacts."""
        tracked = self._run_state.temp_cleanup_paths
        try:
            if tracked:
                # Deepest paths first, so files go before their directories.
                candidates = sorted(tracked, key=lambda path: len(path.parts), reverse=True)
            else:
                candidates = self._untracked_chapter_temp_artifacts()
            for item in candidates:
                _remove_temp_artifact(item)
        finally:
            tracked.clear()

    def _untracked_chapter_temp_artifacts(self) -> list[Path]:
        """Chapter scratch under temp_dir, for runs that ended before tracking any."""
        try:
            return [
                item
                for item in self.config.temp_dir.iterdir()
                if (
                    item.is_dir()
                    and (item.name.startswith("ch_") or item.name == "chapter_masters")
                )
                or (
                    item.is_file()
                    and item.name.startswith("chapter_")
                    and item.suffix.lower() == ".wav"
                )
            ]
        except OSError:
            return []

    def _log_verbose(self, message: str, log_type: str = "info"):
        """Emit verbose log if callback present."""
        self._runtime.log_verbose(message, log_type)
