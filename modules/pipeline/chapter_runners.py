"""Sequential and parallel chapter runners for the pipeline."""

from __future__ import annotations

import importlib.util
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, Optional

from .chapter_processor import ChapterProcessor
from .models import ChapterResult
from .result_validation import format_failure_notes
from .runtime import PipelineRuntimeProtocol
from .worker_errors import (
    WorkerRuntimeLoadError,
    WorkerRuntimeMemoryError,
    WorkerRuntimeUnavailableError,
    WorkerStartupError,
    WorkerStartupStatus,
    StartupFailureAction,
    resolve_startup_failure_action,
)

PARALLEL_AVAILABLE = importlib.util.find_spec(f"{__package__}.parallel") is not None

logger = logging.getLogger(__name__)


def _process_chapter_worker_entry(
    chapter: Any,
    chapter_idx: int,
    _file_manager: Any,
    output_dir: Path,
    total_chapters: Optional[int] = None,
) -> ChapterResult:
    _ = _file_manager
    return ChapterProcessor.process_chapter_worker(
        chapter=chapter,
        chapter_idx=chapter_idx,
        output_dir=output_dir,
        total_chapters=total_chapters,
    )


class SequentialChapterRunner:
    """Owns sequential chapter orchestration, including encode overlap."""

    def __init__(self, runtime: PipelineRuntimeProtocol, processor: ChapterProcessor) -> None:
        self.runtime = runtime
        self._processor = processor

    def process_chapters(self, chapters: list[Any], output_dir: Path) -> list[ChapterResult]:
        return self._process_chapters_sequential(chapters, output_dir)

    def process_chapter(
        self,
        chapter: Any,
        chapter_idx: int,
        total_chapters: int,
        output_dir: Path,
    ) -> ChapterResult:
        return self._processor.process_chapter(
            chapter,
            chapter_idx,
            total_chapters,
            output_dir,
        )

    def _prepare_chapter_audio(self, **kwargs: Any):
        return self._processor.prepare_chapter_audio(**kwargs)

    def _finalize_chapter_encoding(self, **kwargs: Any) -> ChapterResult:
        return self._processor.finalize_chapter_encoding(**kwargs)

    def _process_chapters_sequential(
        self,
        chapters: list,
        output_dir: Path,
    ) -> list[ChapterResult]:
        """
        Process chapters sequentially with optional encode overlap.

        When pipelining is enabled, chapter N encode runs while chapter N+1
        is preparing audio. Any orchestration failure falls back to strict
        chapter-by-chapter processing.
        """
        total_chapters = len(chapters)
        if not self.runtime.config.enable_pipelining or total_chapters <= 1:
            return self._process_chapters_sequential_strict(chapters, output_dir)

        chapter_results: list[ChapterResult] = []
        pending_encoding: Optional[tuple[Future[ChapterResult], ChapterResult]] = None

        try:
            with ThreadPoolExecutor(max_workers=1) as encode_executor:
                for idx, chapter in enumerate(chapters):
                    if self.runtime.check_cancelled():
                        break

                    prepared_result, chapter_wav, was_cancelled = (
                        self._prepare_chapter_audio(
                            chapter=chapter,
                            chapter_idx=idx,
                            total_chapters=total_chapters,
                            output_dir=output_dir,
                        )
                    )

                    if pending_encoding is not None:
                        encode_future, fallback_result = pending_encoding
                        try:
                            chapter_results.append(encode_future.result())
                        except Exception as exc:
                            fallback_result.error = str(exc)
                            chapter_results.append(fallback_result)
                        pending_encoding = None

                    if was_cancelled:
                        prepared_result.error = prepared_result.error or "Cancelled"
                        chapter_results.append(prepared_result)
                        break

                    if prepared_result.error or chapter_wav is None:
                        chapter_results.append(prepared_result)
                        continue

                    future = encode_executor.submit(
                        self._finalize_chapter_encoding,
                        result=prepared_result,
                        chapter_wav=chapter_wav,
                        chapter=chapter,
                        chapter_idx=idx,
                        total_chapters=total_chapters,
                        output_dir=output_dir,
                    )
                    pending_encoding = (future, prepared_result)

                if pending_encoding is not None:
                    encode_future, fallback_result = pending_encoding
                    try:
                        chapter_results.append(encode_future.result())
                    except Exception as exc:
                        fallback_result.error = str(exc)
                        chapter_results.append(fallback_result)

            return chapter_results
        except Exception as exc:
            self.runtime.log_verbose(
                f"[SEQUENTIAL] Pipelined encode overlap failed ({exc}); falling back to strict mode",
                "warning",
            )
            return self._process_chapters_sequential_strict(chapters, output_dir)

    def _process_chapters_sequential_strict(
        self,
        chapters: list,
        output_dir: Path,
    ) -> list[ChapterResult]:
        """Strict chapter-by-chapter sequential processing without overlap."""
        chapter_results: list[ChapterResult] = []
        total_chapters = len(chapters)

        for idx, chapter in enumerate(chapters):
            if self.runtime.check_cancelled():
                break

            result = self.process_chapter(
                chapter=chapter,
                chapter_idx=idx,
                total_chapters=total_chapters,
                output_dir=output_dir,
            )
            chapter_results.append(result)

        return chapter_results



class ParallelChapterRunner:
    """Owns parallel dispatch, fallback, and failed-worker retry handling."""

    def __init__(
        self,
        runtime: PipelineRuntimeProtocol,
        processor: ChapterProcessor,
        sequential_runner: SequentialChapterRunner,
    ) -> None:
        self.runtime = runtime
        self._processor = processor
        self._sequential_runner = sequential_runner
    def process_chapters(self, chapters: list[Any], output_dir: Path) -> list[ChapterResult]:
        return self._process_chapters_parallel(chapters, output_dir)

    def process_chapter(
        self,
        chapter: Any,
        chapter_idx: int,
        total_chapters: int,
        output_dir: Path,
    ) -> ChapterResult:
        return self._processor.process_chapter(
            chapter,
            chapter_idx,
            total_chapters,
            output_dir,
        )

    def process_chapters_sequential(
        self,
        chapters: list[Any],
        output_dir: Path,
    ) -> list[ChapterResult]:
        return self._sequential_runner.process_chapters(chapters, output_dir)

    def _record_parallel_stage_throughput(
        self,
        results: list[ChapterResult],
        chapters: list[Any],
    ) -> None:
        self._processor.record_parallel_stage_throughput(results, chapters)

    def _notify_worker_startup_failure(
        self,
        *,
        total_chapters: int,
        message: str,
        status: WorkerStartupStatus | None = None,
        reason: str = "worker_startup_failure",
    ) -> None:
        performance: dict[str, Any] = {
            "pipeline_mode": "parallel",
            "startup_failure": True,
            "fallback_reason": reason,
        }
        if status is not None:
            performance["startup_provider"] = status.provider
            performance["startup_device"] = status.device
            performance["startup_error_type"] = status.error_type
        self.runtime.notify_progress(
            chapter_idx=0,
            total_chapters=total_chapters,
            message=message,
            performance=performance,
        )

    @staticmethod
    def _summarize_parallel_worker_errors(results: list[ChapterResult]) -> str:
        errors = [str(result.error) for result in results if result.error]
        if not errors:
            return "no worker error details"
        return format_failure_notes(errors)

    def _parallel_fallback(
        self,
        chapters: list,
        output_dir: Path,
        *,
        total_chapters: int,
        reason: str,
        message: str,
        log_message: str,
        failed_count: int,
        safe_mode_reason: Optional[str] = None,
        wrap_reason: Optional[str] = None,
        worker_results: Optional[list[ChapterResult]] = None,
    ) -> list[ChapterResult]:
        if self.runtime.check_cancelled():
            return []

        self.runtime.set_parallel_execution_active(False)
        self.runtime.log_verbose(log_message, "warning")
        performance: dict[str, Any] = {
            "pipeline_mode": "sequential",
            "fallback_reason": reason,
            "failed_chapters": failed_count,
        }
        if safe_mode_reason is not None:
            performance["safe_mode_reason"] = safe_mode_reason
        self.runtime.notify_progress(
            chapter_idx=0,
            total_chapters=total_chapters,
            message=message,
            performance=performance,
        )

        try:
            return self.process_chapters_sequential(chapters, output_dir)
        except Exception as fallback_exc:
            if wrap_reason is None:
                raise
            worker_error_summary = self._summarize_parallel_worker_errors(
                worker_results or []
            )
            raise RuntimeError(
                "Sequential fallback failed after "
                f"{wrap_reason}: {fallback_exc}. Worker errors: "
                f"{worker_error_summary}"
            ) from fallback_exc

    def _process_chapters_parallel(
        self,
        chapters: list,
        output_dir: Path,
    ) -> list[ChapterResult]:
        """
        Process chapters in parallel using ProcessPoolExecutor.

        Each chapter is processed independently in a separate process,
        with proper VRAM budget management.
        """
        total_chapters = len(chapters)

        if not PARALLEL_AVAILABLE or self.runtime.parallel_config is None:
            # Fallback to sequential
            self.runtime.set_parallel_execution_active(False)
            self.runtime.log_verbose(
                "[PARALLEL] Backend unavailable, using sequential chapter processing",
                "warning",
            )
            return self.process_chapters_sequential(chapters, output_dir)

        def chapter_progress(
            current: int,
            total: int,
            message: str,
            performance: Optional[dict[str, Any]] = None,
        ):
            self.runtime.log_verbose(f"[PARALLEL] {message}", "info")
            self.runtime.notify_progress(
                chapter_idx=current,
                total_chapters=total,
                message=message,
                performance=performance,
            )

        from .parallel import ParallelPipelineOrchestrator

        if self.runtime.config.shared_worker_pool_manager is not None:
            self.runtime.log_verbose("[PARALLEL] Using shared worker pool manager", "info")

        parallel_config = self.runtime.parallel_config
        selected_parallel_workers = max(
            1,
            min(
                int(getattr(parallel_config, "max_workers", 1) or 1),
                self.runtime.selected_parallel_chapters(
                    getattr(parallel_config, "max_workers", 1)
                ),
            ),
        )
        run_parallel_config = replace(
            parallel_config,
            max_workers=selected_parallel_workers,
        )

        orchestrator = ParallelPipelineOrchestrator(
            run_parallel_config,
            pool_manager=self.runtime.config.shared_worker_pool_manager,
            config_fingerprint=self.runtime.shared_pool_fingerprint(),
            worker_init_config=self.runtime.build_worker_init_config(),
            memory_policy=(
                None
                if self.runtime.config.worker_plan.worker_mode == "manual"
                else self.runtime.config.memory_policy
            ),
        )

        try:
            self.runtime.set_parallel_execution_active(True)
            parallel_results = orchestrator.process_chapters_parallel(
                chapters=list(chapters),
                process_func=partial(
                    _process_chapter_worker_entry,
                    output_dir=output_dir,
                    total_chapters=total_chapters,
                ),
                progress_callback=chapter_progress,
                cancel_requested=self.runtime.check_cancelled,
            )
            if self.runtime.check_cancelled():
                return []

            processed_results = list(parallel_results)

            if not processed_results:
                return self._parallel_fallback(
                    chapters,
                    output_dir,
                    total_chapters=total_chapters,
                    reason="no_parallel_results",
                    message="Parallel fallback: no chapter results returned",
                    log_message="[PARALLEL] No chapter results returned; retrying sequential mode",
                    failed_count=total_chapters,
                )

            if self._should_degrade_sequential_from_memory_errors(processed_results):
                if self.runtime.check_cancelled():
                    return []
                self._degrade_to_sequential_memory_mode()
                return self._parallel_fallback(
                    chapters,
                    output_dir,
                    total_chapters=total_chapters,
                    reason="memory_pressure",
                    message="Parallel fallback: memory pressure detected",
                    log_message="[PARALLEL] Memory pressure detected; retrying sequential mode",
                    failed_count=len(
                        [result for result in processed_results if result.error]
                    ),
                    safe_mode_reason=self._memory_policy_reason(),
                )

            if all(result.error for result in processed_results):
                return self._parallel_fallback(
                    chapters,
                    output_dir,
                    total_chapters=total_chapters,
                    reason="all_chapter_workers_failed",
                    message="Parallel fallback: all chapter workers failed",
                    log_message="[PARALLEL] All chapter workers failed; retrying sequential mode",
                    failed_count=len(processed_results),
                    wrap_reason="all chapter workers failed",
                    worker_results=processed_results,
                )

            if self._should_retry_sequential_from_parallel_errors(processed_results):
                return self._parallel_fallback(
                    chapters,
                    output_dir,
                    total_chapters=total_chapters,
                    reason="worker_infrastructure_error",
                    message="Parallel fallback: worker infrastructure errors detected",
                    log_message="[PARALLEL] Worker infrastructure errors detected; retrying sequential mode",
                    failed_count=len(
                        [result for result in processed_results if result.error]
                    ),
                    wrap_reason="worker infrastructure errors",
                    worker_results=processed_results,
                )

            self._record_parallel_stage_throughput(processed_results, chapters)

            # Partial failure: retry only the failed chapters sequentially
            failed_indices = [i for i, result in enumerate(processed_results) if result.error]
            if failed_indices:
                if self.runtime.check_cancelled():
                    return [result for result in processed_results if not result.error]
                self.runtime.log_verbose(
                    f"[PARALLEL] Retrying {len(failed_indices)} failed chapter(s) sequentially",
                    "warning",
                )
                previous_parallel_active = self.runtime.parallel_execution_active()
                self.runtime.set_parallel_execution_active(False)
                try:
                    for idx in failed_indices:
                        retry_result = self.process_chapter(
                            chapter=chapters[idx],
                            chapter_idx=idx,
                            total_chapters=total_chapters,
                            output_dir=output_dir,
                        )
                        processed_results[idx] = retry_result
                finally:
                    self.runtime.set_parallel_execution_active(previous_parallel_active)

            return processed_results
        except WorkerStartupError as startup_exc:
            if self.runtime.check_cancelled():
                return []
            return self._handle_worker_startup_failure(
                startup_exc,
                chapters,
                output_dir,
                total_chapters=total_chapters,
            )
        except Exception as e:
            if self.runtime.check_cancelled():
                return []
            if str(e).startswith("Sequential fallback failed after "):
                raise
            logger.error(f"Parallel processing failed: {e}, falling back to sequential")
            return self._parallel_fallback(
                chapters,
                output_dir,
                total_chapters=total_chapters,
                reason="parallel_exception",
                message=f"Parallel fallback: {e}",
                log_message=f"[PARALLEL] Failed ({e}); falling back to sequential mode",
                failed_count=total_chapters,
            )

    def _handle_worker_startup_failure(
        self,
        startup_exc: WorkerStartupError,
        chapters: list,
        output_dir: Path,
        *,
        total_chapters: int,
    ) -> list[ChapterResult]:
        statuses = list(startup_exc.statuses or [])
        primary = statuses[0] if statuses else None
        action = resolve_startup_failure_action(
            primary,
            memory_degrade_allowed=self._memory_policy_allows_startup_degrade(),
        )

        if action == StartupFailureAction.FALLBACK_MEMORY:
            self._degrade_to_sequential_memory_mode()
            return self._parallel_fallback(
                chapters,
                output_dir,
                total_chapters=total_chapters,
                reason="memory_pressure",
                message=primary.format_failure_detail(
                    "Parallel fallback: worker startup memory pressure detected"
                ),
                log_message=(
                    "[PARALLEL] Worker startup memory pressure detected; "
                    "retrying sequential mode"
                ),
                failed_count=total_chapters,
                safe_mode_reason=self._memory_policy_reason()
                or "memory pressure error",
            )

        if action == StartupFailureAction.FAIL_FAST_MEMORY:
            self._notify_worker_startup_failure(
                total_chapters=total_chapters,
                message=primary.format_failure_detail("Worker startup failed"),
                status=primary,
                reason="worker_startup_memory_error",
            )
            raise WorkerRuntimeMemoryError(
                primary.format_failure(
                    fallback_message=str(startup_exc),
                    include_meta=False,
                ),
                status=primary,
            ) from startup_exc

        if action == StartupFailureAction.FAIL_FAST_UNAVAILABLE:
            self._notify_worker_startup_failure(
                total_chapters=total_chapters,
                message=primary.format_failure_detail("Worker startup failed"),
                status=primary,
                reason="worker_startup_unavailable",
            )
            raise WorkerRuntimeUnavailableError(
                primary.format_failure(
                    fallback_message=str(startup_exc),
                    include_meta=False,
                ),
                status=primary,
            ) from startup_exc

        if action == StartupFailureAction.FALLBACK_INFRASTRUCTURE:
            return self._parallel_fallback(
                chapters,
                output_dir,
                total_chapters=total_chapters,
                reason="worker_infrastructure_error",
                message=primary.format_failure_detail(
                    "Parallel fallback: worker infrastructure startup errors detected"
                ),
                log_message=(
                    "[PARALLEL] Worker infrastructure startup errors detected; "
                    "retrying sequential mode"
                ),
                failed_count=total_chapters,
                wrap_reason="worker infrastructure startup errors",
            )

        if action == StartupFailureAction.FAIL_FAST_LOAD:
            self._notify_worker_startup_failure(
                total_chapters=total_chapters,
                message=primary.format_failure_detail("Worker startup failed"),
                status=primary,
                reason="worker_startup_load_error",
            )
            raise WorkerRuntimeLoadError(
                primary.format_failure(
                    fallback_message=str(startup_exc),
                    include_meta=False,
                ),
                status=primary,
            ) from startup_exc

        self._notify_worker_startup_failure(
            total_chapters=total_chapters,
            message=(
                primary.format_failure_detail("Worker startup failed")
                if primary is not None
                else "Worker startup failed"
            ),
            status=primary,
        )
        raise startup_exc

    def _memory_policy_allows_startup_degrade(self) -> bool:
        memory_policy = self.runtime.config.memory_policy
        if memory_policy is None:
            return False
        manual_worker_mode = self.runtime.config.worker_plan.worker_mode == "manual"
        if manual_worker_mode:
            return False
        return True

    def _memory_policy_reason(self) -> Optional[str]:
        memory_policy = self.runtime.config.memory_policy
        if memory_policy is None:
            return None
        reason = getattr(memory_policy, "safe_mode_reason", None)
        manual_worker_mode = self.runtime.config.worker_plan.worker_mode == "manual"
        if manual_worker_mode and reason != "memory pressure error":
            return None
        return str(reason) if reason else None

    def _degrade_to_sequential_memory_mode(self) -> None:
        memory_policy = self.runtime.config.memory_policy
        record_memory_error = getattr(memory_policy, "record_memory_error", None)
        if callable(record_memory_error):
            record_memory_error()
        self.runtime.set_selected_parallel_chapters(1)
        self.runtime.set_parallel_execution_active(False)

    @staticmethod
    def _should_degrade_sequential_from_memory_errors(
        results: list[ChapterResult],
    ) -> bool:
        """Return True when parallel worker failures indicate memory pressure."""
        from .retry import should_degrade_sequential_from_memory_errors

        return should_degrade_sequential_from_memory_errors(results)

    @staticmethod
    def _should_retry_sequential_from_parallel_errors(
        results: list[ChapterResult],
    ) -> bool:
        """
        Return True when parallel worker failures are likely infrastructure-level.
        """
        from .retry import should_retry_sequential_from_parallel_errors

        return should_retry_sequential_from_parallel_errors(results)
