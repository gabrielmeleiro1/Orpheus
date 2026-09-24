"""Scanned chapter cleanup must keep repairs without weakening content checks."""

import pytest

from modules.tts.cleaner import TextCleaner
from modules.tts.cleaner_config import CleanerConfig
from modules.tts.cleaner_rules import (
    llm_preservation_violation,
    page_furniture_lines,
    rule_based_clean,
    strip_page_furniture,
)


SCAN = """# Chapter One

THE LIGHTHOUSE KEEPER                                   12

The keep-
er climbed the stair- case at dusk, count ing each of the
one hundred and twelve steps.   The lamp was cold ; the sea was
loud and the w ind pressed against the glass like a hand.

13                    THE LIGHTHOUSE KEEPER

He struck a match , and the wick caught. Fig. 3 — lamp assembly (see p. 47) ¶
Out beyond the rocks a ship's light blinked twice, then went dark. He wa-
ited, listening, but there was only the tide."""

CLEANED = (
    "Chapter One The keeper climbed the staircase at dusk, counting each of the "
    "one hundred and twelve steps. The lamp was cold; the sea was loud and the "
    "wind pressed against the glass like a hand. He struck a match, and the wick "
    "caught. Fig. 3 — lamp assembly (see p. 47) "
    "Out beyond the rocks a ship's light blinked twice, then went dark. "
    "He waited, listening, but there was only the tide."
)


def test_scan_rules_repair_hyphenation_and_remove_page_furniture():
    cleaned = rule_based_clean(SCAN)
    assert cleaned.startswith("Chapter One The keeper climbed")
    assert "He waited, listening" in cleaned
    assert "LIGHTHOUSE" not in cleaned
    assert "12" not in cleaned
    assert "13" not in cleaned
    # Mid-line spacing needs the LLM; rules must not guess word boundaries.
    assert "stair- case" in cleaned
    assert "count ing" in cleaned
    assert "w ind" in cleaned


def test_scan_fake_llm_trace_accepts_cleaned_text(monkeypatch):
    cleaner = TextCleaner(CleanerConfig(backend="cuda", skip_llm_when_not_needed=False))
    cleaner._loaded = True
    monkeypatch.setattr(cleaner, "_generate_llm_response", lambda _prompt: CLEANED)
    trace = cleaner.clean_with_trace(SCAN)
    assert trace.mode == "llm_success"
    assert trace.llm_error is None
    assert trace.cleaned_text == CLEANED
    assert trace.rule_based_text == rule_based_clean(SCAN)
    assert llm_preservation_violation(SCAN, CLEANED) is None


@pytest.mark.parametrize("source, expected", [
    ("The keep-\ner wa-\nited.", "The keeper waited."),
    ("The keep-  \r\n  er waited.", "The keeper waited."),
    ("The stair- case is well-known.", "The stair- case is well-known."),
    ("She paused—\nthen spoke–\nsoftly.", "She paused— then spoke– softly."),
    ("He said-\nNo.", "He said- No."),
    ("# THE LIGHTHOUSE KEEPER 12\n# THE LIGHTHOUSE KEEPER 13", "THE LIGHTHOUSE KEEPER 12 THE LIGHTHOUSE KEEPER 13"),
    ("12\nThe lamp was cold.\n13", "The lamp was cold."),
    ("THE LIGHTHOUSE KEEPER 12\nThe lamp was cold.", "The lamp was cold."),
    ("13 THE LIGHTHOUSE KEEPER\nThe lamp was cold.", "The lamp was cold."),
    ("The Lighthouse Keeper 12\nThe lamp was cold.\n13 The Lighthouse Keeper", "The lamp was cold."),
    ("The Lighthouse Keeper\nThe lamp was cold.\nThe Lighthouse Keeper", "The lamp was cold."),
])
def test_layout_rules(source, expected):
    assert rule_based_clean(source) == expected


@pytest.mark.parametrize("source", [
    "The lamp was cold.\nThe lamp was cold.",
    "The lamp was cold\nThe lamp was cold",
    '"COME BACK"\n"COME BACK"',
    "“Come back,” he said.\n“Come back,” he said.",
    "1. First item\n2. First item\n3) Third item",
    "1. OPEN THE DOOR\n2. OPEN THE DOOR",
    "- OPEN THE DOOR\n- OPEN THE DOOR",
    "It happened in 1912.\nThey returned in 1912.",
    "in 1912\nin 1912",
    "In 1912\nIn 1912",
    "No\nNo",
    "I\nI",
    "# Chapter One\n# Chapter One",
])
def test_furniture_detector_preserves_prose_dialogue_lists_and_headings(source):
    assert page_furniture_lines(source) == set()
    assert strip_page_furniture(source) == source


@pytest.mark.parametrize("source, cleaned", [
    ("The keep er climbed the stair- case in the w ind.", "The keeper climbed the staircase in the wind."),
    ("12\nThe keep er waited.\n13", "The keeper waited."),
    ("12\nThe keep er waited.\n13", "12 The keeper waited."),
    ("THE KEEPER 12\nThe keep er waited.\n13 THE KEEPER", "The keeper waited."),
    ("THE KEEPER 12\nThe keep er waited.\n13 THE KEEPER", "THE KEEPER 12 The keeper waited."),
])
def test_guard_allows_only_identified_furniture_and_exact_merges(source, cleaned):
    assert llm_preservation_violation(source, cleaned) is None


@pytest.mark.parametrize("source, cleaned", [
    ("He returned in 1912.", "He returned."),
    ("The keeper waited for 12 hours.", "The keeper waited for hours."),
    ("THE KEEPER 12\nThe keep er waited.", "The keeper left."),
    ("THE KEEPER 12\nThe keep er waited.", "The keeper."),
    ("THE KEEPER 12\nThe keeper met the keeper.", "The keeper met."),
    ("The keeper waited, waited, waited.", "The keeper waited."),
    ("1. First item\n2. Second item", "First item Second item"),
    ("# Chapter One\nThe keeper waited.", "The keeper waited."),
    ("The keep er climbed the stair- case.", "The keeper climbed the stairs."),
    (SCAN, CLEANED.replace("Fig.", "Figure")),
])
def test_guard_still_rejects_content_loss_or_substitution(source, cleaned):
    assert llm_preservation_violation(source, cleaned) is not None


@pytest.mark.parametrize("sampler_api", ["sampler", "temp", "temperature"])
def test_mlx_cleanup_is_greedy_even_with_legacy_temperature(monkeypatch, sampler_api):
    import modules.tts.cleaner_mlx as mlx

    cleaner = TextCleaner(CleanerConfig(backend="mlx", temperature=0.35))
    calls = []

    def generate(_model, _tokenizer, **kwargs):
        if sampler_api == "temperature" and "temp" in kwargs:
            raise TypeError("unexpected keyword argument 'temp'")
        calls.append(kwargs)
        return "The keeper waited."

    monkeypatch.setattr(mlx, "generate", generate)
    monkeypatch.setattr(mlx, "make_sampler", (lambda temp: ("greedy", temp)) if sampler_api == "sampler" else None)
    assert cleaner._generate_mlx_response("prompt") == "The keeper waited."
    assert calls[0][sampler_api] == (("greedy", 0.0) if sampler_api == "sampler" else 0.0)
