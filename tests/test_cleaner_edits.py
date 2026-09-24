"""Keep useful local repairs while reverting isolated model inventions."""

import asyncio
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from modules.tts.cleaner import TextCleaner
from modules.tts.cleaner_config import CleanerConfig
from modules.tts.cleaner_edits import reconcile_llm_edits
from modules.tts.cleaner_rules import extract_result, llm_preservation_violation
from tests.test_cleaner_scanned_text import SCAN


# Exact output of the lead's Qwen 2.5 1.5B CUDA/4bit probe after rule repairs.
QWEN_OUTPUT = (
    "Chapter One The keeper climbed the staircase at dusk, counting each of the "
    "one hundred and twelve steps. The lamp was cold; the sea was loud and the "
    "wind pressed against the glass like a hand. He struck a match, and the wick "
    "caught. Figure 3—lamp assembly (see p. 47). Out beyond the rocks, a ship's "
    "light blinked twice, then went dark. He waited, listening, but there was "
    "only the tide."
)


def _cleaner(monkeypatch, response, backend="cuda"):
    cleaner = TextCleaner(CleanerConfig(backend=backend, skip_llm_when_not_needed=False))
    cleaner._loaded = True
    monkeypatch.setattr(cleaner, "_generate_llm_response", lambda _prompt: response)
    return cleaner


def test_exact_scan_accepts_real_qwen_output(monkeypatch):
    cleaner = _cleaner(monkeypatch, QWEN_OUTPUT)
    trace = cleaner.clean_with_trace(SCAN)
    assert trace.mode == "llm_success"
    assert trace.cleaned_text == QWEN_OUTPUT
    assert trace.llm_rejected_edits == []
    assert trace.llm_error is None
    assert cleaner.stats_snapshot()["llm_success"] == 1


@pytest.mark.parametrize("source, expected, candidate", [
    (SCAN, QWEN_OUTPUT, QWEN_OUTPUT + " A dragon landed on the roof."),
    (SCAN, QWEN_OUTPUT, QWEN_OUTPUT.replace("He struck", "A dragon landed on the roof. He struck")),
    (SCAN, QWEN_OUTPUT, "A dragon landed on the roof. " + QWEN_OUTPUT),
    (SCAN.replace("He struck", "Mara struck"), QWEN_OUTPUT.replace("He struck", "Mara struck"), QWEN_OUTPUT.replace("He struck", "Nora struck")),
    (SCAN.replace("twelve steps", "12 steps"), QWEN_OUTPUT.replace("twelve steps", "12 steps"), QWEN_OUTPUT.replace("twelve steps", "112 steps")),
    (SCAN, QWEN_OUTPUT, QWEN_OUTPUT.replace("He struck a match, and the wick caught. ", "")),
])
def test_reverts_only_invented_or_changed_span(monkeypatch, source, expected, candidate):
    cleaner = _cleaner(monkeypatch, candidate)
    trace = cleaner.clean_with_trace(source)
    assert trace.mode == "llm_success", trace.llm_error
    assert trace.cleaned_text == expected
    assert trace.llm_output == candidate
    assert trace.llm_error is None
    assert len(trace.llm_rejected_edits) == 1
    edit = trace.llm_rejected_edits[0]
    assert trace.rule_based_text[edit["source_start"]:edit["source_end"]] == edit["before"]
    assert candidate[edit["llm_start"]:edit["llm_end"]] == edit["after"]
    assert "preservation guard" in edit["reason"]
    assert cleaner.stats_snapshot() == {"llm_success": 1, "llm_fallback": 0, "rule_only": 0, "total_chunks": 1}


def test_partial_acceptance_does_not_disable_model_for_remaining_chunks(monkeypatch):
    cleaner = _cleaner(monkeypatch, QWEN_OUTPUT + " A dragon landed on the roof.")
    traces = cleaner.clean_chunks_with_trace([SCAN, SCAN])
    assert [trace.mode for trace in traces] == ["llm_success", "llm_success"]
    assert all(trace.cleaned_text == QWEN_OUTPUT for trace in traces)
    assert cleaner.stats_snapshot()["llm_success"] == 2


def test_partial_diagnostics_survive_pipeline_jsonl_and_stats(monkeypatch):
    # Read-only integration coverage of the existing consumer contract. The
    # pipeline preserves artifact_score but has a fixed top-level field list.
    from modules.pipeline.stages.cleaning import _build_cleaned_chunk_result, log_cleaner_stats_delta

    cleaner = _cleaner(monkeypatch, QWEN_OUTPUT + " A dragon landed on the roof.")
    before = cleaner.stats_snapshot()
    trace = cleaner.clean_with_trace(SCAN)
    result = _build_cleaned_chunk_result(
        chunk_idx=0, total_chunks=1, original_chunk=SCAN, cleaned_chunk=trace.cleaned_text,
        trace=trace, include_debug_row=True,
    )
    row = json.loads(json.dumps(result.trace_row))
    assert row["mode"] == "llm_success"
    assert row["llm_used"] is True
    assert row["artifact_score"]["llm_rejected_edits"] == trace.llm_rejected_edits
    assert json.loads(json.dumps(asdict(trace)))["llm_rejected_edits"] == trace.llm_rejected_edits
    messages = []
    log_cleaner_stats_delta(SimpleNamespace(log_verbose=lambda message, _level: messages.append(message)), stats_start=before, stats_end=cleaner.stats_snapshot())
    assert "llm_success=1 llm_fallback=0 rule_only=0" in messages[0]


@pytest.mark.parametrize("short, expanded", [
    ("Fig.", "Figure"), ("Figs.", "Figures"), ("p.", "page"), ("pp.", "pages"),
    ("Dr.", "Doctor"), ("Mr.", "Mister"), ("Mrs.", "Missus"), ("St.", "Saint"),
    ("St.", "Street"), ("Prof.", "Professor"), ("Rev.", "Reverend"),
    ("Jr.", "Junior"), ("Sr.", "Senior"),
])
def test_only_standard_dotted_abbreviation_expansions_pass(short, expanded):
    assert llm_preservation_violation(f"See {short} Smith.", f"See {expanded} Smith.") is None
    assert llm_preservation_violation(f"See {short} Smith.", "See Captain Smith.") is not None
    assert llm_preservation_violation(f"See {short[:-1]} Smith.", f"See {expanded} Smith.") is not None


@pytest.mark.parametrize("before, after", [
    ("12", "112"), ("12", "13"), ("1 2", "12"), ("12.5", "125"),
    ("Anne", "Hanne"), ("Smith", "Jones"), ("p.m.", "page m."),
])
def test_names_numbers_and_non_abbreviations_remain_protected(before, after):
    assert llm_preservation_violation(f"They saw {before} today.", f"They saw {after} today.") is not None


def test_ordinal_repair_preserves_the_number():
    assert llm_preservation_violation("For the 450 [th] time.", "For the 450th time.") is None
    assert llm_preservation_violation("For the 450 [th] time.", "For the 451st time.") is not None


def test_short_abbreviation_output_is_not_mistaken_for_length_hallucination(monkeypatch):
    cleaner = _cleaner(monkeypatch, "Doctor X")
    monkeypatch.setattr(cleaner, "_needs_llm_cleaning", lambda _text: True)
    trace = cleaner.clean_with_trace("Dr. X")
    assert trace.mode == "llm_success"
    assert trace.cleaned_text == "Doctor X"


def test_typographic_normalization_is_allowed():
    before = '“It’s late…” she said—then paused–again.'
    after = '"It\'s late..." she said-then paused—again.'
    edits = reconcile_llm_edits(before, after)
    assert edits.text == after
    assert edits.rejected_edits == []
    assert edits.accepted_edits > 0


def test_adjacent_repairs_survive_a_changed_name():
    edits = reconcile_llm_edits("count ing Mara stair- case w ind", "counting Nora staircase wind")
    assert edits.text == "counting Mara staircase wind"
    assert edits.accepted_edits == 3
    assert len(edits.rejected_edits) == 1


def test_reverting_name_after_expansion_does_not_restore_abbreviation_period():
    edits = reconcile_llm_edits("Dr. Smith saw the stair- case.", "Doctor Jones saw the staircase.")
    assert edits.text == "Doctor Smith saw the staircase."


def test_reverting_name_preserves_surrounding_typographic_edits():
    edits = reconcile_llm_edits('“Mara”—she said, count ing.', '"Nora"–she said, counting.')
    assert edits.text == '"Mara"–she said, counting.'
    assert len(edits.rejected_edits) == 1


@pytest.mark.parametrize("candidate, reason", [
    ("", "empty"), ("   \n---\n", "empty"),
    ("Chapter One.", "suspiciously short"),
    (QWEN_OUTPUT * 2, "suspiciously long"),
    (QWEN_OUTPUT.replace("keeper", "pirate").replace("lamp", "bell").replace("tide", "rain"), "over 30%"),
])
def test_catastrophic_output_falls_back_entirely(monkeypatch, candidate, reason):
    cleaner = _cleaner(monkeypatch, candidate)
    trace = cleaner.clean_with_trace(SCAN)
    assert trace.mode == "hallucination_fallback"
    assert trace.cleaned_text == trace.rule_based_text
    assert reason in trace.llm_error
    assert cleaner.stats_snapshot()["llm_fallback"] == 1


@pytest.mark.parametrize("repair_wind, mode", [(True, "llm_success"), (False, "hallucination_fallback")])
def test_rejected_span_budget_counts_edits_not_unchanged_words(monkeypatch, repair_wind, mode):
    source = "Mara repaired the stair- case, count ing steps in the w ind at sunset."
    candidate = "Nora repaired the staircase, counting steps in the w ind at sunset."
    if repair_wind:
        candidate = candidate.replace("w ind", "wind")
    trace = _cleaner(monkeypatch, candidate).clean_with_trace(source)
    assert trace.mode == mode  # One rejected span: 1/4 passes; 1/3 falls back.
    assert trace.cleaned_text == (candidate.replace("Nora", "Mara") if repair_wind else source)
    assert len(trace.llm_rejected_edits) == 1


def test_long_chunk_uses_unchanged_anchors_and_keeps_valid_repairs(monkeypatch):
    prefix = "The tide was rising. " * 150
    candidate = prefix + QWEN_OUTPUT + " A dragon landed on the roof."
    trace = _cleaner(monkeypatch, candidate).clean_with_trace(prefix + SCAN)
    assert trace.mode == "llm_success"
    assert trace.cleaned_text == prefix + QWEN_OUTPUT
    assert len(trace.llm_rejected_edits) == 1


def test_unalignable_output_falls_back_entirely(monkeypatch):
    source = " ".join(["source"] * 600)
    cleaner = _cleaner(monkeypatch, " ".join(["output"] * 600))
    trace = cleaner.clean_with_trace(source)
    assert trace.mode == "hallucination_fallback"
    assert "could not align" in trace.llm_error
    assert trace.cleaned_text == source


def test_paragraphs_are_not_silently_discarded_before_alignment(monkeypatch):
    candidate = QWEN_OUTPUT + "\n\nA dragon landed on the roof."
    assert extract_result(candidate) == candidate
    trace = _cleaner(monkeypatch, candidate).clean_with_trace(SCAN)
    assert trace.mode == "llm_success"
    assert trace.cleaned_text == QWEN_OUTPUT
    assert trace.llm_rejected_edits[0]["after"] == "A dragon landed on the roof"


def test_async_and_sync_partial_traces_match(monkeypatch):
    candidate = QWEN_OUTPUT + " A dragon landed on the roof."
    cleaner = _cleaner(monkeypatch, candidate, backend="api")

    async def generate(_prompt, **_kwargs):
        return candidate

    monkeypatch.setattr(cleaner, "_generate_api_response_async", generate)
    sync = cleaner.clean_with_trace(SCAN)
    asynchronous = asyncio.run(cleaner._clean_with_trace_api_async(SCAN, asyncio.Semaphore(1)))
    assert asdict(sync) == asdict(asynchronous)
    assert sync.mode == "llm_success"
