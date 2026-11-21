"""Cleaner trace offloading, live batch progress, limiter cancellation and receipt caching."""

from __future__ import annotations

import asyncio
import dataclasses
import multiprocessing as mp
import random
import threading
import time
from types import SimpleNamespace

import pytest

from modules.pipeline.stages.cleaning import clean_chunks_with_trace as stage_clean_chunks
from modules.tts import cleaner_edits, cleaner_trace
from modules.tts.api_rate_limit import GlobalAPILimiter, acquire_global_limiter_async
from modules.tts.cleaner import TextCleaner
from modules.tts.cleaner_api import CleanerAPIStrategy
from modules.tts.cleaner_config import CleanerConfig, CleanTrace


# --- post-LLM trace decision -------------------------------------------------

_SOURCES = [
    "Dr. Smith met Mr. Jones in 1987 on St. James street. The stair-\ncase was long and count ing was hard.",
    "## ould you listen? She said nothing, and the room fell quiet.\n\n42\n\nThe next morning, Mara left at 6:30.",
    "T\nhe wind was cold. Prof. Adams wrote 3.5% of the report on p. 12, see Fig. 4.",
    "CHAPTER ONE\n\nIt was the best of times. The in-\nformation age began in 1,200 rooms.",
    "He paid $4,500 for the car— a bargain—and drove to the 450 th street garage. “Mara,” she said.",
    "Some people w ere there and some were n't. The com-\npany grew. Rev. Black spoke.",
]


def _mutate(rng: random.Random, text: str) -> str:
    out = text
    for _ in range(rng.randint(0, 4)):
        op = rng.randrange(9)
        if op == 0:
            out = out.replace("-\n", "", 1)
        elif op == 1:
            out = out.replace("Dr.", "Doctor").replace("Mr.", "Mister")
        elif op == 2:
            out = out.replace(rng.choice(["Smith", "Mara", "1987", "cold"]), "Nora", 1)
        elif op == 3:
            words = out.split(" ")
            del words[rng.randrange(len(words))]
            out = " ".join(words)
        elif op == 4:
            words = out.split(" ")
            words.insert(rng.randrange(len(words) + 1), rng.choice(["indeed", "Bob", "7"]))
            out = " ".join(words)
        elif op == 5:
            out = out.replace("“", '"').replace("”", '"').replace("—", " - ")
        elif op == 6:
            out = out.replace("count ing", "counting").replace("w ere", "were").replace("## ould", "Would")
        elif op == 7:
            out = out[: len(out) // rng.randint(2, 5)]
        else:
            out = out + " " + out
    return out


def _reference_complete(cleaner, request, response, warning_logger):
    """The decision flow with every helper tokenizing the source on its own."""
    artifact_score = cleaner_trace._resolve_trace_artifact_score(cleaner, request)
    final_text = cleaner._repair_known_leading_letter_fragments(cleaner._extract_result(response))
    final_text = cleaner_edits.repair_source_backed_fragments(request.rule_based_text, final_text)
    source_length = len(request.rule_based_text)
    allowance = cleaner_edits.abbreviation_expansion_allowance(request.rule_based_text)
    guard = None
    if not final_text.strip():
        guard = "LLM output is empty; using rule-based fallback"
    elif len(final_text) > source_length * 1.3 + allowance:
        guard = (
            f"LLM output is suspiciously long ({len(final_text)} chars vs {source_length} chars); "
            "using rule-based fallback"
        )
    elif len(final_text) < source_length * 0.7:
        guard = (
            f"LLM output is suspiciously short ({len(final_text)} chars vs {source_length} chars); "
            "using rule-based fallback"
        )
    common = dict(
        original_text=request.original_text, rule_based_text=request.rule_based_text,
        start_time=request.start_time,
    )
    if guard:
        warning_logger.warning("Cleaner: %s", guard)
        return cleaner_trace.make_fallback_trace(
            cleaner, CleanTrace, **common, llm_output=final_text, llm_error=guard,
            hallucination=True, artifact_score=artifact_score,
        )
    edits = cleaner_edits.reconcile_llm_edits(request.rule_based_text, final_text)
    rejected = len(edits.rejected_edits)
    if rejected:
        artifact_score = {**artifact_score, "llm_rejected_edits": edits.rejected_edits}
    error = edits.alignment_error
    if rejected and rejected / (edits.accepted_edits + rejected) > 0.3:
        error = (
            f"{edits.rejected_edits[0]['reason']}; rejected {rejected}/{edits.accepted_edits + rejected} "
            "edit spans (over 30%); using rule-based fallback"
        )
    if error:
        warning_logger.warning("Cleaner: %s", error)
        return cleaner_trace.make_fallback_trace(
            cleaner, CleanTrace, **common, llm_output=final_text, llm_error=error,
            hallucination=True, artifact_score=artifact_score, llm_rejected_edits=edits.rejected_edits,
        )
    if rejected:
        warning_logger.warning(
            "Cleaner: kept %s accepted edits and reverted %s rejected spans", edits.accepted_edits, rejected,
        )
    return cleaner_trace.make_success_trace(
        cleaner, CleanTrace, **common, cleaned_text=edits.text, llm_output=final_text,
        artifact_score=artifact_score, llm_rejected_edits=edits.rejected_edits,
    )


class _Warnings:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, message: str, *args: object) -> None:
        self.messages.append(message % args)


def _counters(cleaner: TextCleaner) -> tuple[int, int, int, int]:
    return (
        cleaner._llm_success_count, cleaner._llm_fallback_count,
        cleaner._rule_only_count, cleaner._total_chunks_cleaned,
    )


def test_shared_source_tokens_match_independent_tokenization():
    for source in _SOURCES:
        shared = cleaner_edits.tokenize_source(source)
        assert shared.tokens == tuple(cleaner_edits._tokens(source))
        assert shared.word_forms == cleaner_edits.hyphenation_word_forms(source)


def test_trace_decision_matches_reference_over_varied_inputs():
    rng = random.Random(20261123)
    expected_cleaner = TextCleaner(CleanerConfig(backend="api"))
    actual_cleaner = TextCleaner(CleanerConfig(backend="api"))
    modes = set()
    for _ in range(300):
        original = rng.choice(_SOURCES)
        source = actual_cleaner._rule_based_clean(original)
        response = _mutate(rng, source)
        if rng.random() < 0.2:
            response = f"<cleaned>{response}</cleaned>"
        score = None if rng.random() < 0.5 else {"total": 0.4}
        request = cleaner_trace.TraceDecisionRequest(original, source, time.time(), score, "prompt")
        expected_logs, actual_logs = _Warnings(), _Warnings()

        expected = _reference_complete(expected_cleaner, request, response, expected_logs)
        actual = cleaner_trace._complete_trace_decision_flow(
            actual_cleaner, CleanTrace, request, response, warning_logger=actual_logs,
        )

        assert dataclasses.asdict(actual) == dataclasses.asdict(expected), (source, response)
        assert actual_logs.messages == expected_logs.messages
        assert _counters(actual_cleaner) == _counters(expected_cleaner)
        modes.add(actual.mode)
    assert modes == {"llm_success", "hallucination_fallback"}


def test_trace_decision_tokenizes_the_source_once(monkeypatch):
    source = _SOURCES[0]
    response = source.replace("stair-\ncase", "staircase").replace("Dr.", "Doctor")
    tokenized: list[str] = []
    word_forms: list[str] = []
    real_tokens, real_forms = cleaner_edits._tokens, cleaner_edits.hyphenation_word_forms

    def counting_tokens(text, *args, **kwargs):
        tokenized.append(text)
        return real_tokens(text, *args, **kwargs)

    def counting_forms(text):
        word_forms.append(text)
        return real_forms(text)

    monkeypatch.setattr(cleaner_edits, "_tokens", counting_tokens)
    monkeypatch.setattr(cleaner_edits, "hyphenation_word_forms", counting_forms)
    cleaner = TextCleaner(CleanerConfig(backend="api"))
    request = cleaner_trace.TraceDecisionRequest(source, source, time.time(), None, "prompt")

    trace = cleaner_trace._complete_trace_decision_flow(cleaner, CleanTrace, request, response)

    assert trace.mode == "llm_success"
    assert tokenized.count(source) == 1
    assert word_forms.count(source) == 1


def test_async_trace_decision_runs_off_the_loop_and_counts_on_it(monkeypatch):
    threads: dict[str, int] = {}
    real_decide = cleaner_trace._decide_trace_completion
    real_success = cleaner_trace.make_success_trace

    def recording_decide(*args, **kwargs):
        threads["decide"] = threading.get_ident()
        return real_decide(*args, **kwargs)

    def recording_success(*args, **kwargs):
        threads["count"] = threading.get_ident()
        return real_success(*args, **kwargs)

    monkeypatch.setattr(cleaner_trace, "_decide_trace_completion", recording_decide)
    monkeypatch.setattr(cleaner_trace, "make_success_trace", recording_success)
    cleaner = TextCleaner(CleanerConfig(backend="api"))
    cleaner._loaded = True
    cleaner._needs_llm_cleaning = lambda _text: True
    text = "The quick brown fox jumps over the lazy dog near the river bank today."

    async def respond(prompt: str) -> str:
        return text

    async def run():
        threads["loop"] = threading.get_ident()
        return await cleaner_trace.run_trace_decision_flow_async(cleaner, CleanTrace, text, respond)

    trace = asyncio.run(run())

    assert trace.mode == "llm_success"
    assert threads["decide"] != threads["loop"]
    assert threads["count"] == threads["loop"]
    assert cleaner.stats_snapshot() == {"llm_success": 1, "llm_fallback": 0, "rule_only": 0, "total_chunks": 1}


def test_async_trace_decision_failure_still_falls_back(monkeypatch):
    def failing_decide(*_args, **_kwargs):
        raise ValueError("alignment exploded")

    monkeypatch.setattr(cleaner_trace, "_decide_trace_completion", failing_decide)
    cleaner = TextCleaner(CleanerConfig(backend="api"))
    cleaner._loaded = True
    cleaner._needs_llm_cleaning = lambda _text: True
    text = "The quick brown fox jumps over the lazy dog near the river bank today."

    async def respond(prompt: str) -> str:
        return text

    trace = asyncio.run(cleaner_trace.run_trace_decision_flow_async(cleaner, CleanTrace, text, respond))

    assert trace.mode == "llm_fallback"
    assert trace.llm_error == "alignment exploded"
    assert cleaner.stats_snapshot()["llm_fallback"] == 1


# --- live batch progress --------------------------------------------------------


def _trace(text: str) -> CleanTrace:
    return CleanTrace(original_text=text, rule_based_text=text, cleaned_text=text.upper(), mode="llm_success")


def test_local_batch_reports_each_finished_chunk_and_keeps_cancelled_slots():
    cleaner = TextCleaner(CleanerConfig(backend="basic"))
    seen: list[tuple[int, int]] = []
    cleaner.clean_with_trace = _trace  # type: ignore[method-assign]
    stop = {"after": 2}

    traces = cleaner.clean_chunks_with_trace(
        ["ccc", "a", "bb", "dddd"],
        cancel_requested=lambda: len(seen) >= stop["after"],
        progress_callback=lambda done, total: seen.append((done, total)),
    )

    assert seen == [(1, 4), (2, 4)]
    # Chunks run shortest first; the two longest were skipped by cancellation.
    assert [trace.original_text if trace else None for trace in traces] == [None, "a", "bb", None]


def test_local_async_batch_reports_progress_on_the_loop_thread():
    calls: list[tuple[int, int, int]] = []
    cleaner = SimpleNamespace(clean_with_trace=_trace, generation_cancel_requested=None)

    async def run():
        loop_thread = threading.get_ident()
        results = await cleaner_trace.run_local_batch_async(
            cleaner, ["a", "b", "c"], None,
            lambda done, total: calls.append((done, total, threading.get_ident() == loop_thread)),
        )
        return results

    results = asyncio.run(run())
    assert [trace.cleaned_text for trace in results] == ["A", "B", "C"]
    assert calls == [(1, 3, True), (2, 3, True), (3, 3, True)]


def test_api_batch_reports_progress_as_requests_finish():
    cleaner = TextCleaner(CleanerConfig(backend="api", api_batch_concurrency=3))
    delays = {"a": 0.03, "b": 0.0, "c": 0.01, "d": 0.0}
    calls: list[tuple[int, int, bool]] = []

    async def fake_clean(text, semaphore, *, api_session=None):
        await asyncio.sleep(delays[text])
        return _trace(text)

    cleaner._clean_with_trace_api_async = fake_clean  # type: ignore[method-assign]

    async def run():
        loop_thread = threading.get_ident()
        return await CleanerAPIStrategy()._clean_batch_bounded(
            cleaner, list(delays), asyncio.Semaphore(3),
            cancel_requested=lambda: False,
            progress_callback=lambda done, total: calls.append(
                (done, total, threading.get_ident() == loop_thread),
            ),
        )

    results = asyncio.run(run())
    assert [trace.original_text for trace in results] == list(delays)
    assert calls == [(n, 4, True) for n in range(1, 5)]


def _stage_runtime(events: list, *, cancelled=lambda: False):
    return SimpleNamespace(
        config=SimpleNamespace(save_cleaning_debug=False),
        check_cancelled=cancelled,
        notify_progress=lambda **payload: events.append(("progress", payload["chunk_idx"], payload["message"])),
        log_verbose=lambda message, _level: events.append(("log", message)),
    )


@pytest.mark.parametrize("backend", ["mlx", "api"])
def test_stage_emits_progress_while_the_batch_runs_without_repeating_it(backend):
    events: list = []
    chunks = ["one", "two", "three"]
    seen_during_batch: list[int] = []

    def run_batch(texts, progress_callback):
        traces = []
        for index, text in enumerate(texts):
            traces.append(_trace(text))
            progress_callback(index + 1, len(texts))
            seen_during_batch.append(sum(1 for event in events if event[0] == "progress"))
        return traces

    if backend == "mlx":
        def batch(texts, *, cancel_requested=None, progress_callback=None):
            return run_batch(texts, progress_callback)
        cleaner = SimpleNamespace(config=SimpleNamespace(backend=backend), clean_chunks_with_trace=batch)
    else:
        async def batch(texts, *, cancel_requested=None, progress_callback=None):
            return run_batch(texts, progress_callback)
        cleaner = SimpleNamespace(config=SimpleNamespace(backend=backend), clean_batch_with_trace_async=batch)

    cleaned, _inputs, _rows, was_cancelled = stage_clean_chunks(
        _stage_runtime(events), SimpleNamespace(run_coroutine_sync=asyncio.run),
        chapter_idx=0, total_chapters=1, chunks=chunks, cleaner=cleaner,
    )

    assert was_cancelled is False
    assert cleaned == ["ONE", "TWO", "THREE"]
    assert seen_during_batch == [1, 2, 3]
    progress = [event for event in events if event[0] == "progress"]
    assert progress == [
        ("progress", i, f"Chapter 1/1: cleaning chunk {i + 1}/3") for i in range(3)
    ]
    # Previews are still logged once per chunk, in chunk order, after progress.
    previews = [event[1] for event in events if event[0] == "log" and "Chunk" in event[1]]
    assert previews == [f"[CLEANER] Chunk {i + 1}/3: {chunk}..." for i, chunk in enumerate(chunks)]


def test_stage_defers_progress_reported_from_another_thread():
    events: list = []
    emitting_threads: list[int] = []
    chunks = ["one", "two", "three"]
    stage_thread = threading.get_ident()

    def notify(**payload):
        emitting_threads.append(threading.get_ident())
        events.append(("progress", payload["chunk_idx"]))

    def batch(texts, *, cancel_requested=None, progress_callback=None):
        def report():
            for done in (2, 1, 2):  # out-of-order and repeated reports
                progress_callback(done, len(texts))
        worker = threading.Thread(target=report)
        worker.start()
        worker.join()
        return [_trace(text) for text in texts]

    runtime = _stage_runtime(events)
    runtime.notify_progress = notify
    cleaner = SimpleNamespace(config=SimpleNamespace(backend="mlx"), clean_chunks_with_trace=batch)

    stage_clean_chunks(
        runtime, SimpleNamespace(run_coroutine_sync=asyncio.run),
        chapter_idx=0, total_chapters=1, chunks=chunks, cleaner=cleaner,
    )

    assert [event for event in events if event[0] == "progress"] == [
        ("progress", 0), ("progress", 1), ("progress", 2),
    ]
    assert set(emitting_threads) == {stage_thread}


def test_stage_cancelled_batch_returns_no_results_after_live_progress():
    events: list = []
    state = {"cancelled": False}

    def batch(texts, *, cancel_requested=None, progress_callback=None):
        progress_callback(1, len(texts))
        state["cancelled"] = True
        progress_callback(2, len(texts))
        assert cancel_requested()
        return [_trace(texts[0]), _trace(texts[1]), None]

    cleaner = SimpleNamespace(config=SimpleNamespace(backend="mlx"), clean_chunks_with_trace=batch)

    cleaned, inputs, rows, was_cancelled = stage_clean_chunks(
        _stage_runtime(events, cancelled=lambda: state["cancelled"]),
        SimpleNamespace(run_coroutine_sync=asyncio.run),
        chapter_idx=0, total_chapters=1, chunks=["a", "b", "c"], cleaner=cleaner,
    )

    assert (cleaned, inputs, rows, was_cancelled) == ([], [], [], True)
    assert [event for event in events if event[0] == "progress"] == [
        ("progress", 0, "Chapter 1/1: cleaning chunk 1/3"),
    ]


# --- global limiter cancellation ---------------------------------------------------


async def _cancel_queued_acquire(semaphore) -> bool:
    entered = asyncio.Event()

    async def wait_for_permit() -> None:
        async with acquire_global_limiter_async(semaphore):
            entered.set()

    task = asyncio.create_task(wait_for_permit())
    await asyncio.sleep(0.1)
    task.cancel()
    done, _pending = await asyncio.wait({task}, timeout=3)
    return bool(done) and task.cancelled() and not entered.is_set()


@pytest.mark.asyncio
async def test_cancelled_acquire_on_a_full_parent_limiter_returns_promptly():
    limiter = GlobalAPILimiter(max_concurrency=1)
    semaphore = limiter.semaphore
    assert semaphore.acquire(False) is True
    try:
        assert await _cancel_queued_acquire(semaphore)
        # Abandoning the wait took no permit: only the holder's remains.
        assert semaphore.acquire(False) is False
    finally:
        semaphore.release()
    assert semaphore.acquire(False) is True
    semaphore.release()
    assert limiter.snapshot()["wait_count"] == 0


@pytest.mark.asyncio
async def test_cancelled_acquire_on_a_full_worker_semaphore_returns_promptly():
    semaphore = mp.get_context("spawn").BoundedSemaphore(1)
    assert semaphore.acquire(False) is True
    try:
        assert await _cancel_queued_acquire(semaphore)
        assert semaphore.acquire(False) is False
    finally:
        semaphore.release()
    assert semaphore.acquire(False) is True
    semaphore.release()


@pytest.mark.asyncio
async def test_worker_semaphore_acquire_still_honours_timeout_and_success():
    semaphore = mp.get_context("spawn").BoundedSemaphore(1)
    async with acquire_global_limiter_async(semaphore, timeout=1):
        assert semaphore.acquire(False) is False
    assert semaphore.acquire(False) is True
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            async with acquire_global_limiter_async(semaphore, timeout=0.12):
                pass
        assert time.monotonic() - started >= 0.1
    finally:
        semaphore.release()


# --- default receipt verifier --------------------------------------------------------


def test_default_asset_check_verifier_uses_the_shared_integrity_cache(monkeypatch):
    from modules.runtime_setup import hub_cache
    from modules.tts import model_snapshots

    created: list[dict] = []

    class RecordingVerifier:
        def __init__(self, **kwargs) -> None:
            created.append(kwargs)

    monkeypatch.setattr(hub_cache, "ModelReceiptVerifier", RecordingVerifier)
    monkeypatch.setattr(hub_cache, "_check_pinned_model_group_assets", lambda groups, verifier: {})

    hub_cache._check_model_group_assets(("tts-mlx",))
    expected = model_snapshots._integrity_cache_path()
    assert expected is not None
    assert created == [{"cache_path": expected}]

    monkeypatch.delenv("AUDIOBOOK_DATA_DIR")
    hub_cache._check_model_group_assets(("tts-mlx",))
    assert created[-1] == {"cache_path": None}


def test_explicit_asset_check_verifier_is_used_as_given(monkeypatch):
    from modules.runtime_setup import hub_cache

    given = object()
    received: list[object] = []
    monkeypatch.setattr(hub_cache, "ModelReceiptVerifier", lambda **_kwargs: pytest.fail("unexpected verifier"))
    monkeypatch.setattr(
        hub_cache, "_check_pinned_model_group_assets", lambda groups, verifier: received.append(verifier) or {},
    )

    hub_cache._check_model_group_assets(("tts-mlx",), receipt_verifier=given)
    assert received == [given]

