"""Regression tests from the external PDF cleaner patch review."""

import sys
from pathlib import Path

import pytest

# Add project root to path, matching existing cleaner tests.
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from modules.ingestion.pdf_artifact_normalizer import repair_line_break_hyphenation
from modules.tts import cleaner_rules as cleaner_rules_backend
from modules.tts.cleaner_rules import llm_preservation_violation


@pytest.mark.parametrize(
    "text",
    [
        "I here give you leave to deal with her.",
        "Charles I as Cromwell appears in the commentary.",
        "Nor was there any reason why I as a sailor should sleep two in a bed.",
    ],
)
def test_review_valid_first_person_and_roman_numeral_phrases_are_preserved(
    text: str,
) -> None:
    assert cleaner_rules_backend.rule_based_clean(text) == text
    assert cleaner_rules_backend.needs_llm_cleaning(text) is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("T\nhe founders were not ready to scale.", "The founders were not ready to scale."),
        ("W\nhen you ask for proof, start with the context.", "When you ask for proof, start with the context."),
        ("W\nhat sort of place was this?", "What sort of place was this?"),
        ("#\nD\neejay gave the crowd a nod.", "Deejay gave the crowd a nod."),
    ],
)
def test_review_repairs_clear_separated_drop_caps(text: str, expected: str) -> None:
    assert cleaner_rules_backend.rule_based_clean(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "The proto-palaces marked the early administrative center.",
        "The auto-da-f\u00e9 drew attention in Seville.",
        "The putto-like figures appear in the commentary.",
        "The coming-into-being of the concept matters here.",
    ],
)
def test_review_valid_hyphenated_compounds_are_preserved_and_not_llm_routed(
    text: str,
) -> None:
    cleaned = cleaner_rules_backend.rule_based_clean(text)

    assert cleaned == text
    assert cleaner_rules_backend.needs_llm_cleaning(cleaned) is False


def test_review_preserves_emphasized_a_i_uppercase_words() -> None:
    assert cleaner_rules_backend.rule_based_clean("I SWEAR I WOULD NEVER") == (
        "I SWEAR I WOULD NEVER"
    )
    assert cleaner_rules_backend.rule_based_clean("A PERSON SPOKE") == "A PERSON SPOKE"
    assert cleaner_rules_backend.rule_based_clean("C AVALRYMEN rode out.") == (
        "CAVALRYMEN rode out."
    )


def test_review_preservation_guard_rejects_a_i_uppercase_merges() -> None:
    assert llm_preservation_violation("I SWEAR I WOULD NEVER", "ISWEAR IWOULD NEVER")
    assert llm_preservation_violation("A PERSON SPOKE", "APERSON SPOKE")
    assert llm_preservation_violation("C AVALRYMEN rode out.", "CAVALRYMEN rode out.") is None


def test_review_repairs_known_multihyphen_pdf_line_break_words() -> None:
    text = (
        "The passage describes auto-sug-\n"
        "gestion and a proto-leg-\n"
        "end, preserves proto-\n"
        "imperial and gameto-\n"
        "technical compounds, repairs get into-\n"
        "just as, and keeps coming-\n"
        "into-being plus coming-into-\n"
        "being intact. Auto-sug-\n"
        "gestion can start a sentence; Proto-\n"
        "imperial can too."
    )

    repaired = repair_line_break_hyphenation(text, known_corpus_repairs=True)

    assert "auto-suggestion" in repaired
    assert "proto-legend" in repaired
    assert "proto-imperial" in repaired
    assert "gameto-technical" in repaired
    assert "into—just" in repaired
    assert "coming-into-being" in repaired
    assert "Auto-suggestion" in repaired
    assert "Proto-imperial" in repaired
    assert "protoimperial" not in repaired
    assert "gametotechnical" not in repaired
    assert "intojust" not in repaired
    assert "cominginto-being" not in repaired
