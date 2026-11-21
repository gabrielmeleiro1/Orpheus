"""Batch cleaning preserves progress and cancellation ordering."""

import asyncio
from types import SimpleNamespace

import pytest

from modules.pipeline.stages.cleaning import clean_chunks_with_trace


@pytest.mark.parametrize("mode", ["local", "trace"])
@pytest.mark.parametrize("cancel_after", [None, 0, 1])
def test_batch_progress_and_cancellation_order(mode, cancel_after):
    events = []
    progress = []
    chunks = ["first\nchunk", "second chunk"]
    outputs = [
        {"cleaned_text": f"clean {i}", "rule_based_text": f"rule {i}"}
        for i in range(2)
    ]

    def cancelled():
        events.append("cancel")
        return cancel_after is not None and len(progress) >= cancel_after

    def notify(**payload):
        events.append("progress")
        progress.append(payload)

    def batch(texts, *, cancel_requested=None):
        assert texts == chunks
        if mode == "local":
            assert cancel_requested is cancelled
        events.append("batch")
        return outputs

    async def async_batch(texts):
        return batch(texts)

    cleaner = SimpleNamespace(config=SimpleNamespace(backend="mlx" if mode == "local" else "api"))
    setattr(cleaner, {
        "local": "clean_chunks_with_trace",
        "trace": "clean_batch_with_trace_async",
    }[mode], batch if mode == "local" else async_batch)
    runtime = SimpleNamespace(
        config=SimpleNamespace(save_cleaning_debug=True),
        check_cancelled=cancelled,
        notify_progress=notify,
        log_verbose=lambda message, level: events.append((message, level)),
    )

    cleaned, inputs, rows, was_cancelled = clean_chunks_with_trace(
        runtime, SimpleNamespace(run_coroutine_sync=asyncio.run),
        chapter_idx=2, total_chapters=4, chunks=chunks, cleaner=cleaner,
    )

    count = 2 if cancel_after is None else cancel_after
    assert cleaned == [f"clean {i}" for i in range(count)]
    assert inputs == [f"rule {i}" for i in range(count)]
    assert [row["chunk_index"] for row in rows] == list(range(1, count + 1))
    assert was_cancelled is (cancel_after is not None)
    assert progress == [
        dict(chapter_idx=2, total_chapters=4, chunk_idx=i, total_chunks=2,
             message=f"Chapter 3/4: cleaning chunk {i + 1}/2")
        for i in range(count)
    ]
    label = {"local": "Local batch trace cleaning", "trace": "Batch trace cleaning"}[mode]
    expected = [(f"[CLEANER] {label} enabled for backend={cleaner.config.backend}", "info"), "batch"]
    # Batch paths check cancellation as soon as the batch returns.
    expected.append("cancel")
    for i in range(count):
        expected.extend(["cancel", "progress"])
        preview = chunks[i][:50].replace("\n", " ") + "..."
        expected.append((f"[CLEANER] Chunk {i + 1}/2: {preview}", "info"))
    if cancel_after is not None and count:
        expected.append("cancel")
    assert events == expected


def test_failed_trace_batch_falls_back_to_per_chunk_without_resending_the_batch():
    batch_calls = []
    chunk_calls = []

    async def failing_trace_batch(texts, *, cancel_requested=None):
        batch_calls.append(list(texts))
        raise RuntimeError("session setup failed")

    async def batch_without_cancellation(texts):
        batch_calls.append(list(texts))
        return list(texts)

    def clean_chunk_safely(*, cleaner, chunk, llm_cleaning_enabled):
        chunk_calls.append(chunk)
        return f"clean {chunk}", llm_cleaning_enabled, None

    cleaner = SimpleNamespace(
        config=SimpleNamespace(backend="api"),
        clean_batch_with_trace_async=failing_trace_batch,
        clean_batch_async=batch_without_cancellation,
    )
    runtime = SimpleNamespace(
        config=SimpleNamespace(save_cleaning_debug=False),
        check_cancelled=lambda: False,
        notify_progress=lambda **_payload: None,
        log_verbose=lambda _message, _level: None,
    )
    hooks = SimpleNamespace(run_coroutine_sync=asyncio.run, clean_chunk_safely=clean_chunk_safely)

    cleaned, _inputs, _rows, was_cancelled = clean_chunks_with_trace(
        runtime, hooks, chapter_idx=0, total_chapters=1, chunks=["a", "b"], cleaner=cleaner,
    )

    assert batch_calls == [["a", "b"]]
    assert chunk_calls == ["a", "b"]
    assert cleaned == ["clean a", "clean b"]
    assert was_cancelled is False
