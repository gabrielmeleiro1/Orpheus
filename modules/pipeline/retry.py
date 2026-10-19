"""Pipeline retry and runtime error classification helpers."""

from __future__ import annotations

from typing import Any, Callable, Optional

from modules.tts.cleaner_errors import is_memory_pressure_error


INFRASTRUCTURE_ERROR_MARKERS = (
    "fds_to_keep",
    "brokenprocesspool",
    "can't pickle",
    "cannot pickle",
    "pickle",
    "resource_tracker",
    "process pool",
)

def should_retry_sequential_from_parallel_errors(results: list[Any]) -> bool:
    """
    Return True when parallel worker failures look infrastructure-related.
    """
    if not results:
        return False

    if any(not getattr(result, "error", None) for result in results):
        return False

    sample_errors = " ".join((getattr(result, "error", "") or "").lower() for result in results[:5])
    return any(marker in sample_errors for marker in INFRASTRUCTURE_ERROR_MARKERS)


def should_degrade_sequential_from_memory_errors(results: list[Any]) -> bool:
    """Return True when parallel worker results indicate memory pressure."""
    if not results:
        return False
    errors = [getattr(result, "error", None) for result in results]
    return any(is_memory_pressure_error(error or "") for error in errors)


def is_infrastructure_runtime_error(error: str) -> bool:
    """Return True when an error indicates worker/runtime infrastructure instability."""
    lowered = str(error or "").lower()
    return any(marker in lowered for marker in INFRASTRUCTURE_ERROR_MARKERS)


def rule_based_clean_fallback(cleaner: Any, text: str) -> str:
    """Best-effort rule-based cleaner fallback."""
    cleaner_fn = getattr(cleaner, "_rule_based_clean", None)
    if callable(cleaner_fn):
        try:
            return cleaner_fn(text)
        except Exception:
            return text
    return text


def clean_chunk_safely(
    *,
    runtime: Any,
    cleaner: Any,
    chunk: str,
    llm_cleaning_enabled: bool,
) -> tuple[str, bool, Optional[dict[str, Any]]]:
    """
    Clean one chunk and degrade to rule-only mode on infrastructure LLM failures.
    """
    if not llm_cleaning_enabled:
        fallback = rule_based_clean_fallback(cleaner, chunk)
        return (
            fallback,
            False,
            {
                "original_text": chunk,
                "rule_based_text": fallback,
                "cleaned_text": fallback,
                "mode": "rule_only",
                "llm_output": None,
                "llm_error": None,
            },
        )

    try:
        clean_with_trace = getattr(cleaner, "clean_with_trace", None)
        if callable(clean_with_trace):
            trace = clean_with_trace(chunk)
            trace_mode = getattr(trace, "mode", "llm_success")
            trace_payload = {
                "original_text": getattr(trace, "original_text", chunk),
                "rule_based_text": getattr(trace, "rule_based_text", chunk),
                "cleaned_text": getattr(trace, "cleaned_text", chunk),
                "mode": trace_mode,
                "llm_output": getattr(trace, "llm_output", None),
                "llm_error": getattr(trace, "llm_error", None),
            }
            continue_llm = trace_mode != "hallucination_fallback"
            if not continue_llm:
                runtime.log_verbose(
                    "[CLEANER] LLM preservation guard rejected cleanup; "
                    "using rule-based cleaning for remaining chunks",
                    "warning",
                )
            return trace_payload["cleaned_text"], continue_llm, trace_payload

        cleaned = cleaner.clean(chunk)
        return cleaned, True, None
    except Exception as exc:
        error_text = str(exc)
        if is_infrastructure_runtime_error(error_text):
            runtime.log_verbose(
                f"[CLEANER] LLM cleaner runtime failed ({error_text}); "
                "falling back to rule-based cleaning for this and remaining chunks",
                "warning",
            )
            fallback = rule_based_clean_fallback(cleaner, chunk)
            return (
                fallback,
                False,
                {
                    "original_text": chunk,
                    "rule_based_text": fallback,
                    "cleaned_text": fallback,
                    "mode": "llm_fallback",
                    "llm_output": None,
                    "llm_error": error_text,
                },
            )
        raise


def retry_batch_results_serially(
    *,
    runtime: Any,
    batch_items: list[Any],
    batch_results: list[Any],
    total_chunks: int,
    on_batch_progress: Callable[[int, int], None],
) -> list[Any]:
    """Retry grouped chunk synthesis serially when all grouped results are errors."""
    if not batch_results or any(not batch_result.error for batch_result in batch_results):
        return batch_results

    if runtime.tts_engine is None:
        return batch_results

    from modules.tts.strategies.base import SynthesisResult

    first_error = batch_results[0].error or "unknown error"
    if is_infrastructure_runtime_error(first_error):
        runtime.log_verbose(
            "[TTS] Grouped chunk synthesis runtime instability detected; retrying serial chunk synthesis",
            "warning",
        )
    else:
        runtime.log_verbose(
            f"[TTS] Grouped chunk synthesis failed for all chunks ({first_error}); retrying serial chunk synthesis",
            "warning",
        )

    recovered_results: list[Any] = []
    for item in batch_items:
        if runtime.check_cancelled():
            break
        try:
            audio = runtime.tts_engine.synthesize(
                text=item.text,
                voice=item.voice,
                speed=item.speed,
            )
            duration_ms = int(len(audio) / runtime.tts_engine.sample_rate * 1000)
            recovered_results.append(
                SynthesisResult(
                    audio=audio,
                    index=item.index,
                    error=None,
                    duration_ms=duration_ms,
                )
            )
        except Exception as serial_exc:
            recovered_results.append(
                SynthesisResult(
                    audio=None,
                    index=item.index,
                    error=str(serial_exc),
                    duration_ms=0,
                )
            )

        if runtime.check_cancelled():
            break
        on_batch_progress(item.index + 1, total_chunks)

    return recovered_results
