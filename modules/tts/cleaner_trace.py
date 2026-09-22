"""Trace-construction helpers for TextCleaner."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import time
from typing import Any

from . import cleaner_rules as cleaner_rules_backend
from .cleaner_config import build_cleaner_prompt


@dataclass(frozen=True)
class TraceDecisionRequest:
    """Prepared trace state that only needs a model/API response."""

    original_text: str
    rule_based_text: str
    start_time: float
    artifact_score: dict[str, object] | None
    prompt: str


def _record_completion(cleaner: Any, start_time: float) -> None:
    cleaner._total_chunks_cleaned += 1  # noqa: SLF001
    cleaner._total_time_ms += (time.time() - start_time) * 1000  # noqa: SLF001


def make_rule_only_trace(
    cleaner: Any,
    trace_cls,
    *,
    original_text: str,
    rule_based_text: str,
    start_time: float,
    artifact_score: dict[str, object] | None = None,
):
    """Build a rule-only trace and update counters."""
    cleaner._rule_only_count += 1  # noqa: SLF001
    _record_completion(cleaner, start_time)
    return trace_cls(
        original_text=original_text,
        rule_based_text=rule_based_text,
        cleaned_text=rule_based_text,
        mode="rule_only",
        artifact_score=artifact_score,
    )


def make_success_trace(
    cleaner: Any,
    trace_cls,
    *,
    original_text: str,
    rule_based_text: str,
    cleaned_text: str,
    llm_output: str | None,
    start_time: float,
    artifact_score: dict[str, object] | None = None,
):
    """Build a successful LLM trace and update counters."""
    cleaner._llm_success_count += 1  # noqa: SLF001
    _record_completion(cleaner, start_time)
    return trace_cls(
        original_text=original_text,
        rule_based_text=rule_based_text,
        cleaned_text=cleaned_text,
        llm_output=llm_output,
        llm_error=None,
        mode="llm_success",
        artifact_score=artifact_score,
    )


def make_fallback_trace(
    cleaner: Any,
    trace_cls,
    *,
    original_text: str,
    rule_based_text: str,
    start_time: float,
    llm_error: str,
    llm_output: str | None = None,
    hallucination: bool = False,
    artifact_score: dict[str, object] | None = None,
):
    """Build a fallback trace and update counters."""
    cleaner._llm_fallback_count += 1  # noqa: SLF001
    _record_completion(cleaner, start_time)
    return trace_cls(
        original_text=original_text,
        rule_based_text=rule_based_text,
        cleaned_text=rule_based_text,
        llm_output=llm_output,
        llm_error=llm_error,
        mode="hallucination_fallback" if hallucination else "llm_fallback",
        artifact_score=artifact_score,
    )


def _warn(
    warning_logger: logging.Logger | None,
    message: str,
    *args: object,
) -> None:
    if warning_logger is not None:
        warning_logger.warning(message, *args)


def _resolve_trace_artifact_score(
    cleaner: Any,
    request: TraceDecisionRequest,
) -> dict[str, object]:
    if isinstance(request.artifact_score, dict):
        return request.artifact_score
    return cleaner._artifact_score_for(request.rule_based_text)  # noqa: SLF001


def _start_trace_decision_flow(
    cleaner: Any,
    trace_cls,
    text: str,
) -> TraceDecisionRequest | Any:
    start_time = time.time()
    original_text = text

    if not text.strip():
        return make_rule_only_trace(
            cleaner,
            trace_cls,
            original_text=original_text,
            rule_based_text="",
            start_time=start_time,
        )

    rule_based_text = cleaner._rule_based_clean(text)  # noqa: SLF001
    if getattr(getattr(cleaner, "config", None), "backend", None) == "basic":
        return make_rule_only_trace(
            cleaner,
            trace_cls,
            original_text=original_text,
            rule_based_text=rule_based_text,
            start_time=start_time,
        )
    if cleaner._needs_llm_cleaning(rule_based_text):  # noqa: SLF001
        artifact_score = None
    else:
        artifact_score = cleaner._artifact_score_for(rule_based_text)  # noqa: SLF001
        if cleaner._should_use_rule_only_cleaning(rule_based_text, artifact_score):  # noqa: SLF001
            return make_rule_only_trace(
                cleaner,
                trace_cls,
                original_text=original_text,
                rule_based_text=rule_based_text,
                start_time=start_time,
                artifact_score=artifact_score,
            )

    if not cleaner._loaded:  # noqa: SLF001
        cleaner.load()

    prompt = build_cleaner_prompt(
        rule_based_text,
        reason=cleaner_rules_backend.describe_llm_cleaning_reason(
            rule_based_text,
            source_type=getattr(getattr(cleaner, "config", None), "source_type", None),
        ),
    )
    return TraceDecisionRequest(
        original_text=original_text,
        rule_based_text=rule_based_text,
        start_time=start_time,
        artifact_score=artifact_score,
        prompt=prompt,
    )


def _complete_trace_decision_flow(
    cleaner: Any,
    trace_cls,
    request: TraceDecisionRequest,
    response: str,
    *,
    warning_logger: logging.Logger | None = None,
) -> Any:
    artifact_score = _resolve_trace_artifact_score(cleaner, request)
    cleaned = cleaner._extract_result(response)  # noqa: SLF001
    final_text = cleaned if cleaned else request.rule_based_text
    final_text = cleaner._repair_known_leading_letter_fragments(final_text)  # noqa: SLF001

    if (
        cleaned
        and len(request.rule_based_text) > 0
        and len(final_text) > len(request.rule_based_text) * 1.3
    ):
        guard_message = (
            "LLM output is suspiciously long "
            f"({len(final_text)} chars vs {len(request.rule_based_text)} chars); "
            "using rule-based fallback"
        )
        _warn(warning_logger, "Cleaner: %s", guard_message)
        return make_fallback_trace(
            cleaner,
            trace_cls,
            original_text=request.original_text,
            rule_based_text=request.rule_based_text,
            start_time=request.start_time,
            llm_output=cleaned,
            llm_error=guard_message,
            hallucination=True,
            artifact_score=artifact_score,
        )

    if cleaned:
        preservation_error = cleaner_rules_backend.llm_preservation_violation(
            request.rule_based_text,
            final_text,
        )
        if preservation_error:
            _warn(warning_logger, "Cleaner: %s", preservation_error)
            return make_fallback_trace(
                cleaner,
                trace_cls,
                original_text=request.original_text,
                rule_based_text=request.rule_based_text,
                start_time=request.start_time,
                llm_output=cleaned,
                llm_error=preservation_error,
                hallucination=True,
                artifact_score=artifact_score,
            )

    return make_success_trace(
        cleaner,
        trace_cls,
        original_text=request.original_text,
        rule_based_text=request.rule_based_text,
        cleaned_text=final_text,
        llm_output=cleaned,
        start_time=request.start_time,
        artifact_score=artifact_score,
    )


def _exception_fallback_trace(
    cleaner: Any,
    trace_cls,
    request: TraceDecisionRequest,
    exc: Exception,
    *,
    warning_logger: logging.Logger | None = None,
) -> Any:
    _warn(warning_logger, "LLM cleaning failed: %s, using rule-based fallback", exc)
    return make_fallback_trace(
        cleaner,
        trace_cls,
        original_text=request.original_text,
        rule_based_text=request.rule_based_text,
        start_time=request.start_time,
        llm_error=str(exc),
        artifact_score=_resolve_trace_artifact_score(cleaner, request),
    )


def run_trace_decision_flow(
    cleaner: Any,
    trace_cls,
    text: str,
    response_generator: Callable[[str], str],
    *,
    warning_logger: logging.Logger | None = None,
) -> Any:
    """Run shared trace cleaning flow with a synchronous response generator."""
    request = _start_trace_decision_flow(cleaner, trace_cls, text)
    if not isinstance(request, TraceDecisionRequest):
        return request

    try:
        response = response_generator(request.prompt)
        return _complete_trace_decision_flow(
            cleaner,
            trace_cls,
            request,
            response,
            warning_logger=warning_logger,
        )
    except Exception as exc:
        return _exception_fallback_trace(
            cleaner,
            trace_cls,
            request,
            exc,
            warning_logger=warning_logger,
        )


async def run_trace_decision_flow_async(
    cleaner: Any,
    trace_cls,
    text: str,
    response_generator: Callable[[str], Awaitable[str]],
    *,
    warning_logger: logging.Logger | None = None,
) -> Any:
    """Run shared trace cleaning flow with an async response generator."""
    request = _start_trace_decision_flow(cleaner, trace_cls, text)
    if not isinstance(request, TraceDecisionRequest):
        return request

    try:
        response = await response_generator(request.prompt)
        return _complete_trace_decision_flow(
            cleaner,
            trace_cls,
            request,
            response,
            warning_logger=warning_logger,
        )
    except Exception as exc:
        return _exception_fallback_trace(
            cleaner,
            trace_cls,
            request,
            exc,
            warning_logger=warning_logger,
        )
