"""Batch cleaning preserves progress and cancellation ordering."""

import asyncio
from types import SimpleNamespace

import pytest

from modules.pipeline.stages.cleaning import clean_chunks_with_trace


@pytest.mark.parametrize("mode", ["local", "trace", "compat"])
@pytest.mark.parametrize("cancel_after", [None, 0, 1])
def test_batch_progress_and_cancellation_order(mode, cancel_after):
    events = []
    progress = []
    chunks = ["first\nchunk", "second chunk"]
    outputs = [
        {"cleaned_text": f"clean {i}", "rule_based_text": f"rule {i}"}
        for i in range(2)
    ]
    if mode == "compat":
        outputs = [f"clean {i}" for i in range(2)]

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
        "compat": "clean_batch_async",
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
    assert inputs == (chunks[:count] if mode == "compat" else [f"rule {i}" for i in range(count)])
    assert [row["chunk_index"] for row in rows] == ([] if mode == "compat" else list(range(1, count + 1)))
    assert was_cancelled is (cancel_after is not None)
    assert progress == [
        dict(chapter_idx=2, total_chapters=4, chunk_idx=i, total_chunks=2,
             message=f"Chapter 3/4: cleaning chunk {i + 1}/2")
        for i in range(count)
    ]
    label = {"local": "Local batch trace cleaning", "trace": "Batch trace cleaning",
             "compat": "Batch cleaning compatibility path"}[mode]
    expected = [(f"[CLEANER] {label} enabled for backend={cleaner.config.backend}", "info"), "batch"]
    # Batch paths that can stop early check cancellation as soon as the batch returns.
    if mode in {"local", "trace"}:
        expected.append("cancel")
    for i in range(count):
        expected.extend(["cancel", "progress"])
        if mode != "compat":
            preview = chunks[i][:50].replace("\n", " ") + "..."
            expected.append((f"[CLEANER] Chunk {i + 1}/2: {preview}", "info"))
    if cancel_after is not None and (mode == "compat" or count):
        expected.append("cancel")
    assert events == expected
