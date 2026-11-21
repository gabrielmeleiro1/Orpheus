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


_SCORE_TABLE = (
    "Round one\nSmith    12\nJones    9\n"
    "Round two\nSmith    15\nJones    11\n"
    "The final went to Smith on the last day."
)


@pytest.mark.parametrize("suffix", [".txt", ".md"])
def test_column_aligned_numbers_in_flowing_text_are_content(tmp_path, suffix):
    from modules.ingestion.markdown_parser import MarkdownParser
    from modules.ingestion.text_parser import TextParser

    source = tmp_path / f"scores{suffix}"
    source.write_text(_SCORE_TABLE, encoding="utf-8")
    parser = TextParser if suffix == ".txt" else MarkdownParser

    content = "\n".join(chapter.content for chapter in parser(source).parse().chapters)

    for line in ("Smith    12", "Jones    9", "Smith    15", "Jones    11"):
        assert line in content


def test_paged_text_still_drops_running_header_folios():
    text = "The Voyage    12\nThe ship sailed.\n\fThe Voyage    13\nThe ship returned."
    assert repair_text_layout(text) == "The ship sailed.\n\fThe ship returned."
