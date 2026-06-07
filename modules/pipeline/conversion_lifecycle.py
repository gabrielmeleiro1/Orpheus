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
    cleanup_models()
    cleanup_temp()
    runtime.state.cancelled = False


def cancelled_result(title: str = "", author: str | None = None) -> ConversionResult:
    """Build the standard cancelled conversion result."""
    return ConversionResult(
        success=False,
        title=title,
        author=author,
        error="Cancelled",
    )
