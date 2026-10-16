"""Regression coverage for text layout."""

import time

import pytest

from modules.ingestion.text_layout import (
    corroborated_folio_titles,
    numbered_folio_title,
    repair_text_layout,
)


@pytest.mark.parametrize("text", [
    "CHAPTER 1\nThe voyage began.",
    "The Wind Rises\nA first verse.\nThe Wind Rises\nA second verse.",
])
def test_02_headings_and_refrains_are_not_furniture(text):
    assert repair_text_layout(text) == text


# Long runs used to backtrack quadratically: 200k characters took minutes.
_RUN = 200_000


@pytest.mark.parametrize("text", [
    "a" * _RUN + "!",
    "a" * _RUN + "-x",
    "Title" + " " * _RUN + "x",
    "Title" + " " * _RUN + "1x",
], ids=["letters", "letters-hyphen", "spaces", "spaces-digit"])
def test_long_letter_and_space_runs_repair_in_linear_time(text):
    started = time.perf_counter()
    assert repair_text_layout(text) == text
    assert time.perf_counter() - started < 2.0


def test_trailing_folio_titles_still_corroborate_across_pages():
    lines = ["The Voyage    12", "Prose line.", "The Voyage    14"]
    assert corroborated_folio_titles(lines) == {"the voyage"}
    assert numbered_folio_title("12 The Voyage 13") == "The Voyage"


def test_wrapped_words_still_join_after_linear_anchor():
    assert repair_text_layout("an extra-\nordinary voyage") == "an extraordinary voyage"
