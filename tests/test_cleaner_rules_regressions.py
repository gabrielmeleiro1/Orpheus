"""Regression coverage for cleaner rules."""

import pytest

from modules.ingestion.text_layout import repair_text_layout
from modules.tts import cleaner_rules as cleaner_rules_backend
from modules.tts.cleaner_rules import rule_based_clean
from tests.test_cleaner_scanned_text import _guard_rejects


@pytest.mark.parametrize("text, expected", [
    ("Her self-\ncontrol returned.", "Her self-control returned."),
    ("Her self- control returned.", "Her self-control returned."),
    ("Their well-\nbeing mattered.", "Their well-being mattered."),
])
def test_05_compound_hyphens_survive(text, expected):
    assert rule_based_clean(text) == expected
    if "\n" in text:
        assert repair_text_layout(text) == expected


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
    assert _guard_rejects("I SWEAR I WOULD NEVER", "ISWEAR IWOULD NEVER")
    assert _guard_rejects("A PERSON SPOKE", "APERSON SPOKE")
    assert not _guard_rejects("C AVALRYMEN rode out.", "CAVALRYMEN rode out.")
