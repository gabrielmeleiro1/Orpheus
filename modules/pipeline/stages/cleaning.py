"""Pipeline cleaning-stage helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

from modules.tts.cleaner_backends.registry import is_local_cleaner_backend


@dataclass(frozen=True)
class CleanedChunkResult:
    """Normalized per-chunk cleaning output shared by all execution paths."""

    cleaned_text: str
    llm_input_text: str
    trace_row: dict[str, Any] | None


class CleaningStageHooks(Protocol):
    """Stage-owned operations needed by cleaning orchestration."""

    def run_coroutine_sync(self, coro: Any) -> Any: ...

    def clean_chunk_safely(
        self,
        *,
        cleaner: Any,
        chunk: str,
        llm_cleaning_enabled: bool,
    ) -> tuple[str, bool, dict[str, Any] | None]: ...


def save_pre_clean_markdown(
    runtime: Any,
    *,
    chapter: Any,
    output_dir: Path,
    original_chunks: list[str],
) -> None:
    """Persist original text for the library's cleaning comparison."""
    try:
        before_clean_md = output_dir / f"chapter_{chapter.number:02d}_before_cleaning.md"
        before_clean_md.write_text(
            f"# {chapter.title}\n\n" + "\n\n".join(original_chunks),
            encoding="utf-8",
        )
        runtime.log_verbose(
            f"[CLEANER] Saved pre-clean text: {before_clean_md.name}",
            "info",
        )
    except Exception as exc:
        runtime.log_verbose(
            f"[CLEANER] Failed to save pre-clean md: {exc}",
            "warning",
        )


def log_cleaner_stats_delta(
    runtime: Any,
    *,
    stats_start: dict[str, int] | None,
    stats_end: dict[str, int] | None,
) -> None:
    """Log cleaner usage deltas between two stats snapshots."""
    if stats_start is None or stats_end is None:
        return

    llm_success = stats_end["llm_success"] - stats_start["llm_success"]
    llm_fallback = stats_end["llm_fallback"] - stats_start["llm_fallback"]
    rule_only = stats_end["rule_only"] - stats_start["rule_only"]
    runtime.log_verbose(
        "[CLEANER] LLM usage: "
        f"llm_success={llm_success} "
        f"llm_fallback={llm_fallback} "
        f"rule_only={rule_only}",
        "info",
    )


def save_cleaning_artifacts(
    runtime: Any,
    *,
    chapter: Any,
    output_dir: Path,
    cleaned_chunks: list[str],
    llm_input_chunks: list[str],
    cleaning_trace_rows: list[dict[str, Any]],
) -> None:
    """Persist cleaned output and optional debug artifacts."""
    try:
        if runtime.config.save_cleaning_debug and llm_input_chunks:
            llm_input_md = output_dir / f"chapter_{chapter.number:02d}_llm_input.md"
            llm_input_md.write_text(
                f"# {chapter.title}\n\n" + "\n\n".join(llm_input_chunks),
                encoding="utf-8",
            )
            runtime.log_verbose(
                f"[CLEANER] Saved LLM-input text: {llm_input_md.name}",
                "info",
            )

        if runtime.config.save_cleaning_debug and cleaning_trace_rows:
            trace_jsonl = output_dir / f"chapter_{chapter.number:02d}_cleaning_trace.jsonl"
            trace_jsonl.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in cleaning_trace_rows) + "\n",
                encoding="utf-8",
            )
            runtime.log_verbose(
                f"[CLEANER] Saved cleaning trace: {trace_jsonl.name}",
                "info",
            )

        clean_md = output_dir / f"chapter_{chapter.number:02d}_cleaned.md"
        clean_md.write_text(
            f"# {chapter.title}\n\n" + "\n\n".join(cleaned_chunks),
            encoding="utf-8",
        )
        runtime.log_verbose(f"[CLEANER] Saved cleaned text: {clean_md.name}", "success")
    except Exception as exc:
        runtime.log_verbose(f"[CLEANER] Failed to save cleaned md: {exc}", "warning")


def _normalize_trace_payload(trace: Any) -> dict[str, Any]:
    if hasattr(trace, "__dict__"):
        trace_payload = dict(trace.__dict__)
        if trace_payload:
            return trace_payload
    if isinstance(trace, dict):
        return dict(trace)
    return {
        key: getattr(trace, key)
        for key in (
            "mode",
            "llm_error",
            "original_text",
            "rule_based_text",
            "cleaned_text",
            "artifact_score",
        )
        if hasattr(trace, key)
    }


def _build_trace_row(
    *,
    chunk_idx: int,
    total_chunks: int,
    original_chunk: str,
    cleaned_chunk: str,
    trace_payload: dict[str, Any],
) -> dict[str, Any]:
    rule_based_text = str(trace_payload.get("rule_based_text", original_chunk))
    return {
        "chunk_index": chunk_idx + 1,
        "total_chunks": total_chunks,
        "mode": trace_payload.get("mode"),
        "llm_error": trace_payload.get("llm_error"),
        "original_text": trace_payload.get("original_text", original_chunk),
        "rule_based_text": rule_based_text,
        "cleaned_text": trace_payload.get("cleaned_text", cleaned_chunk),
        "artifact_score": trace_payload.get("artifact_score"),
        "llm_used": trace_payload.get("mode") in {"llm_success", "llm_fallback", "hallucination_fallback"},
        "raw_chars": len(original_chunk),
        "rule_clean_chars": len(rule_based_text),
        "cleaned_chars": len(str(trace_payload.get("cleaned_text", cleaned_chunk))),
    }


def _build_cleaned_chunk_result(
    *,
    chunk_idx: int,
    total_chunks: int,
    original_chunk: str,
    cleaned_chunk: str,
    trace: Any,
    include_debug_row: bool,
) -> CleanedChunkResult:
    """Normalize one cleaned chunk into a consistent contract."""
    if trace is None:
        return CleanedChunkResult(
            cleaned_text=cleaned_chunk,
            llm_input_text=original_chunk,
            trace_row=None,
        )

    trace_payload = _normalize_trace_payload(trace)
    normalized_cleaned_text = str(trace_payload.get("cleaned_text", cleaned_chunk))
    llm_input_text = str(trace_payload.get("rule_based_text", original_chunk))
    trace_row = None
    if include_debug_row:
        trace_row = _build_trace_row(
            chunk_idx=chunk_idx,
            total_chunks=total_chunks,
            original_chunk=original_chunk,
            cleaned_chunk=normalized_cleaned_text,
            trace_payload=trace_payload,
        )
    return CleanedChunkResult(
        cleaned_text=normalized_cleaned_text,
        llm_input_text=llm_input_text,
        trace_row=trace_row,
    )


def _emit_cleaning_progress(
    runtime: Any,
    *,
    chapter_idx: int,
    total_chapters: int,
    chunk_idx: int,
    total_chunks: int,
    original_chunk: str,
    log_preview: bool,
) -> None:
    runtime.notify_progress(
        chapter_idx=chapter_idx,
        total_chapters=total_chapters,
        chunk_idx=chunk_idx,
        total_chunks=total_chunks,
        message=f"Cleaning chunk {chunk_idx + 1}/{total_chunks}",
    )
    if not log_preview:
        return

    preview = original_chunk[:50].replace("\n", " ") + "..."
    runtime.log_verbose(f"[CLEANER] Chunk {chunk_idx + 1}/{total_chunks}: {preview}", "info")


def _collect_results_payload(
    results: list[CleanedChunkResult],
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    cleaned_chunks = [result.cleaned_text for result in results]
    llm_input_chunks = [result.llm_input_text for result in results]
    cleaning_trace_rows = [result.trace_row for result in results if result.trace_row is not None]
    return cleaned_chunks, llm_input_chunks, cleaning_trace_rows


def _validate_batch_length(batch_results: list[Any], total_chunks: int) -> None:
    if len(batch_results) != total_chunks:
        raise RuntimeError(
            f"Cleaner batch output mismatch: got {len(batch_results)} for {total_chunks} chunks"
        )


def _run_batch_trace_cleaning(
    runtime: Any,
    hooks: CleaningStageHooks,
    *,
    cleaner: Any,
    cleaner_backend: str,
    chapter_idx: int,
    total_chapters: int,
    chunks: list[str],
) -> Optional[tuple[list[CleanedChunkResult], bool]]:
    clean_batch_with_trace_async = getattr(cleaner, "clean_batch_with_trace_async", None)
    if not callable(clean_batch_with_trace_async):
        return None

    total_chunks = len(chunks)
    runtime.log_verbose(
        f"[CLEANER] Batch trace cleaning enabled for backend={cleaner_backend}",
        "info",
    )
    try:
        batch_traces = hooks.run_coroutine_sync(clean_batch_with_trace_async(chunks))
        _validate_batch_length(batch_traces, total_chunks)

        results: list[CleanedChunkResult] = []
        for chunk_idx, (chunk, trace) in enumerate(zip(chunks, batch_traces)):
            if runtime.check_cancelled():
                return results, True

            _emit_cleaning_progress(
                runtime,
                chapter_idx=chapter_idx,
                total_chapters=total_chapters,
                chunk_idx=chunk_idx,
                total_chunks=total_chunks,
                original_chunk=chunk,
                log_preview=True,
            )
            results.append(
                _build_cleaned_chunk_result(
                    chunk_idx=chunk_idx,
                    total_chunks=total_chunks,
                    original_chunk=chunk,
                    cleaned_chunk=chunk,
                    trace=trace,
                    include_debug_row=runtime.config.save_cleaning_debug,
                )
            )
        return results, False
    except Exception as exc:
        runtime.log_verbose(
            f"[CLEANER] Batch trace cleaning failed ({exc}); falling back to compatibility paths",
            "warning",
        )
        return None


def _run_compat_batch_cleaning(
    runtime: Any,
    hooks: CleaningStageHooks,
    *,
    cleaner: Any,
    cleaner_backend: str,
    chapter_idx: int,
    total_chapters: int,
    chunks: list[str],
) -> Optional[tuple[list[CleanedChunkResult], bool]]:
    clean_batch_async = getattr(cleaner, "clean_batch_async", None)
    if not callable(clean_batch_async):
        return None

    total_chunks = len(chunks)
    runtime.log_verbose(
        f"[CLEANER] Batch cleaning compatibility path enabled for backend={cleaner_backend}",
        "info",
    )
    try:
        batch_cleaned = hooks.run_coroutine_sync(clean_batch_async(chunks))
        _validate_batch_length(batch_cleaned, total_chunks)

        results: list[CleanedChunkResult] = []
        for chunk_idx, (chunk, cleaned_chunk) in enumerate(zip(chunks, batch_cleaned)):
            if runtime.check_cancelled():
                return results, True

            _emit_cleaning_progress(
                runtime,
                chapter_idx=chapter_idx,
                total_chapters=total_chapters,
                chunk_idx=chunk_idx,
                total_chunks=total_chunks,
                original_chunk=chunk,
                log_preview=False,
            )
            results.append(
                _build_cleaned_chunk_result(
                    chunk_idx=chunk_idx,
                    total_chunks=total_chunks,
                    original_chunk=chunk,
                    cleaned_chunk=cleaned_chunk,
                    trace=None,
                    include_debug_row=runtime.config.save_cleaning_debug,
                )
            )
        return results, False
    except Exception as exc:
        runtime.log_verbose(
            f"[CLEANER] Batch compatibility cleaning failed ({exc}); falling back to per-chunk cleaning",
            "warning",
        )
        return None


def _run_sync_trace_batch_cleaning(
    runtime: Any,
    *,
    cleaner: Any,
    cleaner_backend: str,
    chapter_idx: int,
    total_chapters: int,
    chunks: list[str],
) -> Optional[tuple[list[CleanedChunkResult], bool]]:
    clean_chunks_with_trace = getattr(cleaner, "clean_chunks_with_trace", None)
    if not callable(clean_chunks_with_trace):
        return None

    total_chunks = len(chunks)
    runtime.log_verbose(
        f"[CLEANER] Local batch trace cleaning enabled for backend={cleaner_backend}",
        "info",
    )
    try:
        batch_traces = clean_chunks_with_trace(chunks)
        _validate_batch_length(batch_traces, total_chunks)
        results: list[CleanedChunkResult] = []
        for chunk_idx, (chunk, trace) in enumerate(zip(chunks, batch_traces)):
            if runtime.check_cancelled():
                return results, True
            _emit_cleaning_progress(
                runtime,
                chapter_idx=chapter_idx,
                total_chapters=total_chapters,
                chunk_idx=chunk_idx,
                total_chunks=total_chunks,
                original_chunk=chunk,
                log_preview=True,
            )
            results.append(
                _build_cleaned_chunk_result(
                    chunk_idx=chunk_idx,
                    total_chunks=total_chunks,
                    original_chunk=chunk,
                    cleaned_chunk=chunk,
                    trace=trace,
                    include_debug_row=runtime.config.save_cleaning_debug,
                )
            )
        return results, False
    except Exception as exc:
        runtime.log_verbose(
            f"[CLEANER] Local batch trace cleaning failed ({exc}); falling back to per-chunk cleaning",
            "warning",
        )
        return None

def _run_per_chunk_cleaning(
    runtime: Any,
    hooks: CleaningStageHooks,
    *,
    cleaner: Any,
    chapter_idx: int,
    total_chapters: int,
    chunks: list[str],
) -> tuple[list[CleanedChunkResult], bool]:
    total_chunks = len(chunks)
    llm_cleaning_enabled = True
    results: list[CleanedChunkResult] = []

    for chunk_idx, chunk in enumerate(chunks):
        if runtime.check_cancelled():
            return results, True

        _emit_cleaning_progress(
            runtime,
            chapter_idx=chapter_idx,
            total_chapters=total_chapters,
            chunk_idx=chunk_idx,
            total_chunks=total_chunks,
            original_chunk=chunk,
            log_preview=True,
        )
        cleaned_chunk, llm_cleaning_enabled, clean_trace = hooks.clean_chunk_safely(
            cleaner=cleaner,
            chunk=chunk,
            llm_cleaning_enabled=llm_cleaning_enabled,
        )
        results.append(
            _build_cleaned_chunk_result(
                chunk_idx=chunk_idx,
                total_chunks=total_chunks,
                original_chunk=chunk,
                cleaned_chunk=cleaned_chunk,
                trace=clean_trace,
                include_debug_row=runtime.config.save_cleaning_debug,
            )
        )

    return results, False


def clean_chunks_with_trace(
    runtime: Any,
    hooks: CleaningStageHooks,
    chapter_idx: int,
    total_chapters: int,
    chunks: list[str],
    cleaner: Any,
) -> tuple[list[str], list[str], list[dict[str, Any]], bool]:
    """
    Clean chunks and collect trace metadata.

    Returns:
        Tuple of (cleaned_chunks, llm_input_chunks, trace_rows, was_cancelled).
    """
    cleaner_backend = str(getattr(getattr(cleaner, "config", None), "backend", "")).lower() or "unknown"

    if is_local_cleaner_backend(cleaner_backend):
        batch_result = _run_sync_trace_batch_cleaning(
            runtime,
            cleaner=cleaner,
            cleaner_backend=cleaner_backend,
            chapter_idx=chapter_idx,
            total_chapters=total_chapters,
            chunks=chunks,
        )
        if batch_result is not None:
            results, was_cancelled = batch_result
            cleaned_chunks, llm_input_chunks, cleaning_trace_rows = _collect_results_payload(results)
            return cleaned_chunks, llm_input_chunks, cleaning_trace_rows, was_cancelled

        results, was_cancelled = _run_per_chunk_cleaning(
            runtime,
            hooks,
            cleaner=cleaner,
            chapter_idx=chapter_idx,
            total_chapters=total_chapters,
            chunks=chunks,
        )
        cleaned_chunks, llm_input_chunks, cleaning_trace_rows = _collect_results_payload(results)
        return cleaned_chunks, llm_input_chunks, cleaning_trace_rows, was_cancelled

    batch_result = _run_batch_trace_cleaning(
        runtime,
        hooks,
        cleaner=cleaner,
        cleaner_backend=cleaner_backend,
        chapter_idx=chapter_idx,
        total_chapters=total_chapters,
        chunks=chunks,
    )
    if batch_result is not None:
        results, was_cancelled = batch_result
        cleaned_chunks, llm_input_chunks, cleaning_trace_rows = _collect_results_payload(results)
        return cleaned_chunks, llm_input_chunks, cleaning_trace_rows, was_cancelled

    batch_result = _run_compat_batch_cleaning(
        runtime,
        hooks,
        cleaner=cleaner,
        cleaner_backend=cleaner_backend,
        chapter_idx=chapter_idx,
        total_chapters=total_chapters,
        chunks=chunks,
    )
    if batch_result is not None:
        results, was_cancelled = batch_result
        cleaned_chunks, llm_input_chunks, cleaning_trace_rows = _collect_results_payload(results)
        return cleaned_chunks, llm_input_chunks, cleaning_trace_rows, was_cancelled

    results, was_cancelled = _run_per_chunk_cleaning(
        runtime,
        hooks,
        cleaner=cleaner,
        chapter_idx=chapter_idx,
        total_chapters=total_chapters,
        chunks=chunks,
    )
    cleaned_chunks, llm_input_chunks, cleaning_trace_rows = _collect_results_payload(results)
    return cleaned_chunks, llm_input_chunks, cleaning_trace_rows, was_cancelled
