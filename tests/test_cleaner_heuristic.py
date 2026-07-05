"""Heuristic coverage tests for TextCleaner._needs_llm_cleaning."""

import threading
import sys
from pathlib import Path

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from modules.tts.cleaner import TextCleaner
from modules.tts.cleaner_config import CleanerConfig
from tests.fixtures.fake_cleaner_backend import attach_cleaner_backend_strategy


def _make_rule_only_cleaner() -> TextCleaner:
    cleaner = TextCleaner.__new__(TextCleaner)
    cleaner.config = CleanerConfig()
    cleaner._loaded = False
    cleaner._model = None
    cleaner._tokenizer = None
    cleaner._manager = None
    cleaner._model_lock = threading.RLock()
    cleaner._total_chunks_cleaned = 0
    cleaner._total_time_ms = 0
    cleaner._llm_success_count = 0
    cleaner._llm_fallback_count = 0
    cleaner._rule_only_count = 0
    return attach_cleaner_backend_strategy(cleaner)


TRUE_CASES = [
    "This took place on the # W trading floor.",
    "The bat has expe- rience and a sur- rounding world.",
    "The page residue left 44I comprehension in the prose.",
    "The text was interrupted from I The problem I am going to raise.",
    "This content downloaded from 127.0.0.1 should never be spoken.",
    "Iam tired, Ibegin again, and Iwish the wall would stop moving.",
    "Iwould rather not leave this joined into spoken handoff.",
]

FALSE_CASES = [
    "The fox jumped over the dog.",
    "Doctor Smith arrived and reviewed the notes.",
    "Room 4",
    "There are 42 apples on the table.",
    "The crash of 1987 changed market behavior.",
    "Already expanded abbreviations with no artifacts.",
    "This street contained two- and three-story wooden dwellings.",
    "The final sentence ends here.",
    "NASA launched a rocket.",
    "The PDF format was used by the API client.",
    "A long paragraph with NASA acronyms should not trigger additional cleaning.",
    "The W symbol was used as a variable in the sentence.",
    "FREDERICK THE SECOND Wonder of the world: 1194-1250",
]

EDGE_CASES = [
    "",
    "   ",
    "...!!!??",
    "::::::::::AA",
    "::::::::AA:a",
    "ABCDEFGHIJKL",
]


@pytest.mark.parametrize("text", TRUE_CASES)
def test_heuristic_should_trigger(text: str) -> None:
    cleaner = _make_rule_only_cleaner()
    assert cleaner._needs_llm_cleaning(text) is True


@pytest.mark.parametrize("text", FALSE_CASES)
def test_heuristic_should_not_trigger(text: str) -> None:
    cleaner = _make_rule_only_cleaner()
    assert cleaner._needs_llm_cleaning(text) is False


@pytest.mark.parametrize("text", EDGE_CASES)
def test_heuristic_edge_cases(text: str) -> None:
    cleaner = _make_rule_only_cleaner()
    assert cleaner._needs_llm_cleaning(text) is False


def test_rule_based_clean_strips_simple_trailing_source_note() -> None:
    cleaner = _make_rule_only_cleaner()

    cleaned = cleaner._rule_based_clean(  # noqa: SLF001
        "The final sentence ends here. 1. Dublin."
    )

    assert cleaned == "The final sentence ends here."
    assert cleaner._needs_llm_cleaning(cleaned) is False


def test_heuristic_scopes_pdf_artifact_triggers_to_pdf_sources() -> None:
    cleaner = _make_rule_only_cleaner()
    text = "The bat has expe- rience and This content downloaded from a mirror."

    assert cleaner._needs_llm_cleaning(text, source_type="pdf") is True
    assert cleaner._needs_llm_cleaning(text, source_type=None) is True
    assert cleaner._needs_llm_cleaning(text, source_type="epub") is False
    assert cleaner._needs_llm_cleaning(text, source_type="txt") is False
    assert cleaner._needs_llm_cleaning(text, source_type="md") is False


def test_heuristic_reason_scopes_pdf_artifact_triggers_to_pdf_sources() -> None:
    from modules.tts import cleaner_rules as cleaner_rules_backend

    text = "The bat has expe- rience."

    assert (
        cleaner_rules_backend.describe_llm_cleaning_reason(text, source_type="pdf")
        == "possible PDF line-break hyphenation"
    )
    assert (
        cleaner_rules_backend.describe_llm_cleaning_reason(text, source_type="epub")
        == "default conservative cleanup pass"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-o", "addopts="]))
