"""A cancelled conversion stops API cleaning without waiting out the chapter batch."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

from modules.async_bridge import run_coroutine_sync
from modules.pipeline.stages.cleaning import clean_chunks_with_trace
from modules.tts.cleaner import TextCleaner
from modules.tts.cleaner_config import CleanerConfig


def _slow_api_cleaner(request_seconds: float) -> tuple[TextCleaner, list[str]]:
    cleaner = TextCleaner(CleanerConfig(backend="api", api_key="test-key"))
    started: list[str] = []

    async def slow_request(text, semaphore, *, api_session=None):
        started.append(text)
        async with semaphore:
            await asyncio.sleep(request_seconds)
        return SimpleNamespace(cleaned_text=text, rule_based_text=text, mode="llm_success")

    cleaner._clean_with_trace_api_async = slow_request
    cleaner._get_api_aiohttp_module = lambda: None
    return cleaner, started


def test_cancel_during_api_batch_returns_promptly_without_starting_more_chunks():
    cleaner, started = _slow_api_cleaner(request_seconds=5.0)
    cancel = threading.Event()
    runtime = SimpleNamespace(
        check_cancelled=cancel.is_set,
        log_verbose=lambda *args, **kwargs: None,
        notify_progress=lambda **kwargs: None,
        config=SimpleNamespace(save_cleaning_debug=False),
    )
    hooks = SimpleNamespace(run_coroutine_sync=run_coroutine_sync)
    chunks = [f"Chunk {index}." for index in range(40)]
    threading.Timer(0.3, cancel.set).start()

    began = time.monotonic()
    cleaned, _llm_inputs, _rows, was_cancelled = clean_chunks_with_trace(
        runtime, hooks, chapter_idx=0, total_chapters=1, chunks=chunks, cleaner=cleaner,
    )
    elapsed = time.monotonic() - began

    assert was_cancelled is True
    assert cleaned == []
    # One concurrency window of requests had started; the rest never began and
    # the in-flight ones were abandoned instead of awaited (5 s each).
    assert elapsed < 2.0
    assert len(started) == cleaner._get_api_batch_concurrency()


def test_api_batch_without_cancellation_still_cleans_every_chunk():
    cleaner, started = _slow_api_cleaner(request_seconds=0.01)
    chunks = [f"Chunk {index}." for index in range(9)]

    traces = run_coroutine_sync(
        cleaner.clean_batch_with_trace_async(chunks, cancel_requested=lambda: False)
    )

    assert [trace.cleaned_text for trace in traces] == chunks
    assert sorted(started) == sorted(chunks)
