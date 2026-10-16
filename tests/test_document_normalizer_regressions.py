"""Regression coverage for document normalizer."""

import time

import pytest

from modules.ingestion.models import Chapter, Document
from modules.text.document_normalizer import normalize_extracted_document


@pytest.mark.parametrize("opening, closing", [('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")])
def test_overlong_dialogue_preserves_closing_quotation_marks(opening, closing):
    sentences = [
        f"{opening}The ship reached the island.{closing}",
        f"{opening}They returned before the night.{closing}",
    ]
    source = " ".join(sentences * 5)
    document = Document(title="Voyage", chapters=[Chapter(number=1, title="Voyage", content=source)])

    normalized = normalize_extracted_document(
        document, source_type="txt", skip_non_prose_sections=False, max_chapter_words=8,
    )

    assert len(normalized.chapters) == 10
    assert all(len(chapter.content.split()) <= 8 for chapter in normalized.chapters)
    assert " ".join(" ".join(chapter.content.split()) for chapter in normalized.chapters) == source


def _epub_document(content: str) -> Document:
    return Document(title="Book", chapters=[Chapter(number=1, title="One", content=content)])


def test_epub_backlinks_are_removed_without_merging_paragraphs():
    content = "First paragraph.  Back to top ↩\n\nSecond paragraph.\n\nReturn to contents\n\nThird."
    normalized = normalize_extracted_document(
        _epub_document(content), source_type=".epub", skip_non_prose_sections=False,
    )
    assert normalized.chapters[0].content == "First paragraph.\n\nSecond paragraph.\n\nThird."


def test_epub_backlink_phrase_inside_prose_is_kept():
    content = "She went back to top the hill, then feedback to top management."
    normalized = normalize_extracted_document(
        _epub_document(content), source_type=".epub", skip_non_prose_sections=False,
    )
    assert normalized.chapters[0].content == content


@pytest.mark.parametrize(
    "gap", [" " * 200_000, " \n" * 100_000, "\n" * 200_000], ids=["spaces", "blank-lines", "newlines"],
)
def test_epub_cleanup_is_linear_in_whitespace_runs(gap):
    content = f"Opening prose.{gap}Closing prose."
    started = time.perf_counter()
    normalize_extracted_document(
        _epub_document(content), source_type=".epub", skip_non_prose_sections=False,
    )
    assert time.perf_counter() - started < 2.0
