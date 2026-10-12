"""Regression coverage for cleaner edits."""

import pytest

from modules.tts.cleaner_edits import reconcile_llm_edits

from tests.test_cleaner_edits import _cleaner as _mock_cleaner


def _cleaner(monkeypatch, candidate):
    cleaner = _mock_cleaner(monkeypatch, candidate)
    monkeypatch.setattr(cleaner, "_needs_llm_cleaning", lambda _text: True)
    return cleaner


@pytest.mark.parametrize("source, candidate", [
    ("We went in to see her.", "We went into see her."),
    ("They are now here.", "They are nowhere."),
    ("They stood apart.", "They stood a part."),
])
def test_08_ambiguous_word_boundaries_are_rejected(monkeypatch, source, candidate):
    assert reconcile_llm_edits(source, candidate).rejected_edits
    assert _cleaner(monkeypatch, candidate).clean_with_trace(source).cleaned_text == source


def test_09_arbitrary_leading_letter_is_rejected(monkeypatch):
    source = "We ate less today."
    trace = _cleaner(monkeypatch, "We ate bless today.").clean_with_trace(source)
    assert trace.cleaned_text == source
    assert trace.llm_rejected_edits


@pytest.mark.parametrize("source", ["Use vitamin D daily.", "Let X equal two.", "Ask J. Smith today.", "Apply 5 V here."])
def test_10_single_letters_are_preserved(monkeypatch, source):
    candidate = source.replace("D ", "").replace("X ", "").replace("J. ", "").replace("V ", "")
    trace = _cleaner(monkeypatch, candidate).clean_with_trace(source)
    assert trace.cleaned_text == source
    assert trace.llm_rejected_edits


@pytest.mark.parametrize("before, after", [("-12.5", "12.5"), ("+12.5", "12.5"), ("12.5", "12,5"), ("1:30", "1 30"), ("3/4", "3-4")])
def test_11_numeric_punctuation_is_protected(monkeypatch, before, after):
    source = f"The answer is {before}."
    trace = _cleaner(monkeypatch, f"The answer is {after}.").clean_with_trace(source)
    assert trace.cleaned_text == source
    assert trace.llm_rejected_edits


def test_12_typography_repairs_count_toward_edit_budget(monkeypatch):
    source = '“Wait”—Mara said. “Stay”—she added.'
    candidate = '"Wait"–Nora said. "Stay"–she added.'
    edits = reconcile_llm_edits(source, candidate)
    assert edits.accepted_edits >= 4
    traces = _cleaner(monkeypatch, candidate).clean_chunks_with_trace([source, source])
    assert [trace.mode for trace in traces] == ["llm_success", "llm_success"]
    assert all(trace.cleaned_text == candidate.replace("Nora", "Mara") for trace in traces)


def test_14_rejected_abbreviation_does_not_duplicate_period(monkeypatch):
    source = "Dr. Smith was count ing steps and gathering inform ation in the w ind."
    candidate = "Doc. Smith was counting steps and gathering information in the wind."
    trace = _cleaner(monkeypatch, candidate).clean_with_trace(source)
    assert trace.mode == "llm_success"
    assert trace.cleaned_text == candidate.replace("Doc.", "Dr.")


def test_15_rejection_offsets_address_recorded_candidate(monkeypatch):
    source = "At the time Mara was count ing steps by the stair case in the w ind."
    candidate = "At he time Nora was counting steps by the staircase in the wind."
    trace = _cleaner(monkeypatch, candidate).clean_with_trace(source)
    assert trace.llm_rejected_edits
    for edit in trace.llm_rejected_edits:
        assert trace.llm_output[edit["llm_start"]:edit["llm_end"]] == edit["after"]
