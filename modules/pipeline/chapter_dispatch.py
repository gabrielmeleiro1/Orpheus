"""Helpers for chapter worker selection and dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .models import WorkerPlan
from .pool_shutdown import shutdown_shared_worker_pool
from .runtime import PipelineRuntimeProtocol


@dataclass(frozen=True)
class WorkerSelection:
    """Resolved worker count for this dispatch pass."""

    count: int
    requested: int
    reason: str | None = None


@dataclass(frozen=True)
class ChapterDispatchContext:
    """Public dispatch boundary for worker selection and chapter execution."""

    runtime: PipelineRuntimeProtocol
    runner: Any

    @property
    def config(self) -> Any:
        return self.runtime.config

    @property
    def total_chars(self) -> int:
        return max(0, int(getattr(self.runtime.state, "total_chars", 0) or 0))

    @property
    def is_parallel(self) -> bool:
        return self.runtime.is_parallel

    def check_cancelled(self) -> bool:
        return self.runtime.check_cancelled()

    def log_verbose(self, message: str, log_type: str = "info") -> None:
        self.runtime.log_verbose(message, log_type)

    def parallel_capacity(self, fallback: int) -> int:
        return self.runtime.parallel_config_max_workers(fallback)

    def set_parallel_execution_active(self, active: bool) -> None:
        self.runtime.set_parallel_execution_active(active)

    def set_selected_parallel_chapters(self, count: int) -> None:
        self.runtime.set_selected_parallel_chapters(count)

    def set_selected_parallel_worker_count_reason(self, reason: str | None) -> None:
        self.runtime.set_selected_parallel_worker_count_reason(reason)


def _coerce_dispatch_context(candidate: Any) -> ChapterDispatchContext:
    context = getattr(candidate, "dispatch_context", candidate)
    if isinstance(context, ChapterDispatchContext):
        return context
    raise TypeError("dispatch_chapters requires a ChapterDispatchContext")


def adaptive_parallel_workers(
    *,
    total_chapters: int,
    total_chars: int,
    max_parallel_chapters: int,
) -> int:
    """Select a worker cap using the current total-book and average-chapter size."""
    if total_chapters <= 1:
        return 1

    total_chars = max(0, int(total_chars))
    avg_chapter_chars = total_chars / max(total_chapters, 1)

    if total_chars < 40_000:
        return 1
    if avg_chapter_chars < 20_000 and total_chars < 200_000:
        return 1
    if avg_chapter_chars < 90_000:
        return min(2, max_parallel_chapters)
    if avg_chapter_chars < 180_000:
        return min(3, max_parallel_chapters)
    return max_parallel_chapters


def _runtime_backpressure_worker_limit(
    memory_policy: Any,
    requested_workers: int,
) -> int:
    """Estimate the maximum immediate submits allowed by runtime backpressure."""
    requested_workers = max(1, int(requested_workers or 1))
    if memory_policy is None or requested_workers <= 1:
        return requested_workers

    should_submit_more = getattr(memory_policy, "should_submit_more", None)
    if not callable(should_submit_more):
        return requested_workers

    permitted_workers = 0
    while permitted_workers < requested_workers:
        if not should_submit_more(permitted_workers):
            break
        permitted_workers += 1

    return max(1, permitted_workers)


def _configured_worker_plan(config: Any) -> WorkerPlan:
    plan = getattr(config, "worker_plan", None)
    if isinstance(plan, WorkerPlan):
        return plan
    return WorkerPlan()


def _worker_selection_reason(
    worker_plan: WorkerPlan,
    selected_workers: int,
    requested_workers: int,
) -> str | None:
    worker_mode = worker_plan.worker_mode
    worker_auto_mode = worker_plan.worker_auto_mode

    if (
        worker_plan.initial_reason is not None
        and (selected_workers < requested_workers or worker_mode == "manual")
    ):
        return worker_plan.initial_reason
    if worker_mode == "manual":
        return "manual worker count"
    if worker_auto_mode == "cpu_cores" and worker_plan.process_safe:
        return "process-safe CPU-core auto worker policy"
    if worker_auto_mode == "sequential" and selected_workers < requested_workers:
        return "backend sequential auto worker policy"
    if selected_workers < requested_workers:
        return "adaptive worker guard"
    return None


def effective_workers(
    *,
    worker_plan: WorkerPlan,
    total_chapters: int,
    total_chars: int,
    parallel_capacity: int | None = None,
    adaptive_selector: Callable[..., int] = adaptive_parallel_workers,
    memory_policy: Any = None,
) -> WorkerSelection:
    """Resolve the dispatch worker count from the canonical WorkerPlan."""
    requested_workers = max(1, int(worker_plan.requested_workers or 1))
    planned_workers = max(1, int(worker_plan.max_workers or 1))
    capacity = max(1, int(parallel_capacity or planned_workers))
    worker_mode = worker_plan.worker_mode
    worker_auto_mode = worker_plan.worker_auto_mode

    if not worker_plan.enable_parallel:
        selected_workers = 1
    elif worker_mode == "manual":
        selected_workers = planned_workers
    elif worker_auto_mode == "cpu_cores" and worker_plan.process_safe:
        selected_workers = planned_workers
    elif worker_auto_mode == "sequential":
        selected_workers = 1
    elif planned_workers < requested_workers and worker_plan.initial_reason is not None:
        selected_workers = planned_workers
    else:
        selected_workers = adaptive_selector(
            total_chapters=total_chapters,
            total_chars=total_chars,
            max_parallel_chapters=planned_workers,
        )

    selected_workers = min(max(1, int(selected_workers or 1)), planned_workers, capacity)
    if (
        worker_mode == "manual"
        and worker_plan.enable_parallel
        and selected_workers > 1
        and not (worker_auto_mode == "cpu_cores" and worker_plan.process_safe)
        and callable(getattr(memory_policy, "allowed_workers", None))
    ):
        # A manual count is a ceiling: each worker loads its own models, so it is
        # limited to what free device memory allows at the moment of dispatch.
        allowed = max(1, int(memory_policy.allowed_workers(selected_workers)))
        if allowed < selected_workers:
            return WorkerSelection(
                count=allowed,
                requested=requested_workers,
                reason=(
                    getattr(memory_policy, "selected_worker_count_reason", None)
                    or "device memory allows fewer workers"
                ),
            )
    return WorkerSelection(
        count=selected_workers,
        requested=requested_workers,
        reason=_worker_selection_reason(
            worker_plan,
            selected_workers,
            requested_workers,
        ),
    )


def dispatch_chapters(
    dispatch_context: Any,
    chapters: list[Any],
    output_dir: Path,
) -> list[Any]:
    """Apply the adaptive guard and route chapter processing to the active mode."""
    context = _coerce_dispatch_context(dispatch_context)
    runner = context.runner
    total_chapters = len(chapters)
    worker_plan = _configured_worker_plan(context.config)
    total_chars = context.total_chars
    parallel_capacity = context.parallel_capacity(worker_plan.max_workers)
    selection = effective_workers(
        worker_plan=worker_plan,
        total_chapters=total_chapters,
        total_chars=total_chars,
        parallel_capacity=parallel_capacity,
        memory_policy=getattr(context.config, "memory_policy", None),
    )
    selected_workers = selection.count
    context.set_selected_parallel_chapters(selected_workers)
    context.set_selected_parallel_worker_count_reason(selection.reason)
    if worker_plan.enable_parallel and selected_workers < selection.requested:
        avg_chapter_chars = int(total_chars / max(total_chapters, 1))
        reason = selection.reason or f"avg chapter size {avg_chapter_chars} chars"
        noun = "worker" if selected_workers == 1 else "workers"
        context.log_verbose(
            f"[PARALLEL] Using {selected_workers} {noun} of {selection.requested} "
            f"requested: {reason}",
            "warning" if worker_plan.worker_mode == "manual" else "info",
        )

    use_parallel = (
        context.is_parallel
        and total_chapters > 1
        and selected_workers > 1
    )
    context.set_parallel_execution_active(use_parallel)

    if context.check_cancelled():
        return []

    if not use_parallel and total_chapters > 0:
        shutdown_shared_worker_pool(context.config)
        runner.warmup_models()
        if context.check_cancelled():
            return []

    if use_parallel:
        memory_policy = getattr(context.config, "memory_policy", None)
        if (
            memory_policy is not None
            and getattr(memory_policy, "enable_warmup_calibration", True)
            and getattr(memory_policy, "measured_warmup_delta_bytes", None) is None
            and callable(getattr(memory_policy, "record_warmup", None))
        ):
            context.log_verbose(
                "[PARALLEL] Calibrating per-worker memory from model warmup",
                "process",
            )
            context.set_parallel_execution_active(False)
            runner.warmup_models()
            runner.cleanup_models(force=True)
            if context.check_cancelled():
                return []
            selected_workers = min(
                selected_workers,
                memory_policy.allowed_workers(selected_workers),
            )
            context.set_selected_parallel_chapters(selected_workers)
            use_parallel = selected_workers > 1
            context.set_parallel_execution_active(use_parallel)
            if not use_parallel:
                context.set_selected_parallel_worker_count_reason(
                    "warmup memory estimate selected sequential processing",
                )
                context.log_verbose(
                    "[PARALLEL] Warmup memory estimate selected sequential processing",
                    "warning",
                )
                return runner.process_chapters_sequential(chapters, output_dir)

        if worker_plan.worker_mode != "manual":
            backpressure_workers = _runtime_backpressure_worker_limit(
                getattr(context.config, "memory_policy", None),
                selected_workers,
            )
            if backpressure_workers < selected_workers:
                previous_workers = selected_workers
                selected_workers = backpressure_workers
                context.set_selected_parallel_chapters(selected_workers)
                context.set_selected_parallel_worker_count_reason(
                    getattr(memory_policy, "safe_mode_reason", None)
                    or "runtime memory backpressure guard",
                )
                use_parallel = selected_workers > 1
                context.set_parallel_execution_active(use_parallel)
                context.log_verbose(
                    f"[PARALLEL] Runtime backpressure guard: {previous_workers} -> "
                    f"{selected_workers}",
                    "warning",
                )
                if not use_parallel:
                    shutdown_shared_worker_pool(context.config)
                    runner.warmup_models()
                    if context.check_cancelled():
                        return []
                    return runner.process_chapters_sequential(chapters, output_dir)

        if context.check_cancelled():
            return []
        context.log_verbose(
            f"[PARALLEL] Using {selected_workers} concurrent workers "
            f"for {total_chapters} chapters",
            "process",
        )
        return runner.process_chapters_parallel(chapters, output_dir)

    context.log_verbose(
        f"[SEQUENTIAL] Processing {total_chapters} chapters sequentially",
        "process",
    )
    return runner.process_chapters_sequential(chapters, output_dir)
