"""Per-run lifecycle helpers for conversion pipeline instances."""

from __future__ import annotations

from .models import ConversionResult


def begin_conversion_run(runtime) -> None:
    """Reset per-run state before a conversion starts."""
    selected_parallel_chapters = max(
        1,
        int(
            getattr(
                getattr(runtime.config, "worker_plan", None),
                "max_workers",
                runtime.config.max_parallel_chapters,
            )
            or 1
        ),
    )
    runtime.state.reset_for_conversion(
        selected_parallel_chapters=selected_parallel_chapters
    )


def finish_conversion_run(runtime, *, cleanup_models, cleanup_temp) -> None:
    """Release per-run resources after a conversion exits."""
    runtime.set_parallel_execution_active(False)
    executor = getattr(runtime.state, "chapter_aac_executor", None)
    if executor is not None:
        executor.shutdown(wait=True)
        runtime.state.chapter_aac_executor = None
        runtime.state.chapter_aac_pending_future = None
    wait_workers = getattr(runtime, "wait_for_background_workers", None)
    workers_stopped = bool(wait_workers(timeout=1.0)) if callable(wait_workers) else True
    try:
        if workers_stopped:
            cleanup_models()
        else:
            # A backend cleaner call may be non-interruptible. Do not unload its
            # model while that daemon producer is still executing; the bounded
            # one-second wait keeps cancellation responsive and intentionally
            # leaves model cleanup to process teardown in this rare case.
            log_verbose = getattr(runtime, "log_verbose", None)
            if callable(log_verbose):
                log_verbose(
                    "[CLEANUP] Background cleaner still running; deferred model unload",
                    "warning",
                )
        cleanup_temp()
    finally:
        runtime.state.cancelled = False


def cancelled_result(title: str = "", author: str | None = None) -> ConversionResult:
    """Build the standard cancelled conversion result."""
    return ConversionResult(
        success=False,
        title=title,
        author=author,
        error="Cancelled",
    )
