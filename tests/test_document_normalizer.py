import re

from modules.ingestion.models import Chapter, Document
from modules.text.document_normalizer import normalize_extracted_document


def test_pdf_document_normalizer_repairs_headers_page_numbers_hyphenation_and_reflow():
    document = Document(
        title="Example Book",
        author="A. Writer",
        chapters=[
            Chapter(
                number=1,
                title="Chapter 1",
                content=(
                    "Example Book\n"
                    "conver-\n"
                    "sation continued on this line\n"
                    "and this is the same paragraph.\n"
                    "12\n\n"
                    "Example Book\n"
                    "Next paragraph starts here.\n"
                    "13"
                ),
            )
        ],
    )

    normalized = normalize_extracted_document(document, source_type="pdf")

    content = normalized.chapters[0].content
    assert "Example Book" not in content
    assert "12" not in content
    assert "13" not in content
    assert "conversation continued on this line and this is the same paragraph." in content
    assert "\n\nNext paragraph starts here." in content


def test_epub_document_normalizer_drops_nav_and_backlink_noise():
    document = Document(
        title="Novel",
        chapters=[
            Chapter(number=1, title="Table of Contents", content="Chapter 1\nChapter 2"),
            Chapter(
                number=2,
                title="Chapter 1",
                content="Real story text. Back to top ↩\n\nPublisher boilerplate",
            ),
        ],
    )

    normalized = normalize_extracted_document(document, source_type="epub")

    assert [chapter.title for chapter in normalized.chapters] == ["Chapter 1"]
    assert "Back to top" not in normalized.chapters[0].content
    assert "Publisher boilerplate" not in normalized.chapters[0].content


def test_epub_document_normalizer_drops_publisher_and_back_matter_pages():
    document = Document(
        title="Novel",
        author="A. Writer",
        chapters=[
            Chapter(number=1, title="Also by A. Writer", content="Other books in the catalogue."),
            Chapter(
                number=2,
                title="Chapter 1",
                content="Real story text that should become the first audiobook chapter.",
            ),
            Chapter(
                number=3,
                title="About the Publisher",
                content="Publisher imprint and marketing copy that should not be read aloud.",
            ),
            Chapter(
                number=4,
                title="Praise for Novel",
                content="Review blurbs that should stay outside the generated chapter list.",
            ),
        ],
    )

    normalized = normalize_extracted_document(document, source_type="epub")

    assert [chapter.title for chapter in normalized.chapters] == ["Chapter 1"]
    assert normalized.chapters[0].number == 1


def _pdf_chapters(*contents: str) -> Document:
    return Document(
        title="Example Book",
        author="A. Writer",
        chapters=[
            Chapter(number=index, title=f"Chapter {index}", content=content)
            for index, content in enumerate(contents, start=1)
        ],
    )


def test_pdf_normalizer_retains_repeated_body_prose_across_chapters():
    refrain = "All we have is this moment together in the quiet dark."
    document = _pdf_chapters(
        f"{refrain}\n\nThe first chapter continues with unique events.",
        f"{refrain}\n\nThe second chapter continues with different events.",
        f"{refrain}\n\nThe third chapter continues with more unique events.",
        f"{refrain}\n\nThe fourth chapter continues with closing events.",
    )

    normalized = normalize_extracted_document(document, source_type="pdf")

    assert len(normalized.chapters) == 4
    for chapter in normalized.chapters:
        assert refrain in chapter.content
        assert chapter.content.strip()


def test_pdf_normalizer_retains_numbered_opening_sentences():
    document = _pdf_chapters(
        "Opening 1.\n\nFirst chapter body with its own details.",
        "Opening 2.\n\nSecond chapter body with its own details.",
        "Opening 3.\n\nThird chapter body with its own details.",
        "Opening 4.\n\nFourth chapter body with its own details.",
    )

    normalized = normalize_extracted_document(document, source_type="pdf")

    assert len(normalized.chapters) == 4
    assert "Opening 1." in normalized.chapters[0].content
    assert "Opening 2." in normalized.chapters[1].content
    assert "Opening 3." in normalized.chapters[2].content
    assert "Opening 4." in normalized.chapters[3].content


def test_pdf_normalizer_retains_mid_chapter_repeated_prose():
    shared = "She remembered the promise they had made beneath the oak."
    document = _pdf_chapters(
        f"Chapter one begins here.\n\n{shared}\n\nThen the scene changes.",
        f"Chapter two begins here.\n\n{shared}\n\nThen another scene unfolds.",
        f"Chapter three begins here.\n\n{shared}\n\nThen the story closes.",
    )

    normalized = normalize_extracted_document(document, source_type="pdf")

    assert len(normalized.chapters) == 3
    for chapter in normalized.chapters:
        assert shared in chapter.content


def test_pdf_normalizer_still_removes_credible_running_headers_at_edges():
    document = _pdf_chapters(
        "RUNNING HEADER\n\nPage one body stays here.",
        "RUNNING HEADER\n\nPage two body stays here.",
        "RUNNING HEADER\n\nPage three body stays here.",
        "RUNNING HEADER\n\nPage four body stays here.",
    )

    normalized = normalize_extracted_document(document, source_type="pdf")

    assert len(normalized.chapters) == 4
    for chapter in normalized.chapters:
        assert "RUNNING HEADER" not in chapter.content
        assert "body stays here." in chapter.content


def test_pdf_normalizer_retains_short_sentence_prose_never_again():
    """Blank-adjacent short sentences are prose, not page-edge headers."""
    refrain = "Never again."
    document = _pdf_chapters(
        f"{refrain}\n\nFirst chapter continues with unique events.",
        f"{refrain}\n\nSecond chapter continues with different events.",
        f"{refrain}\n\nThird chapter continues with more unique events.",
        f"{refrain}\n\nFourth chapter continues with closing events.",
    )

    normalized = normalize_extracted_document(document, source_type="pdf")

    assert len(normalized.chapters) == 4
    for chapter in normalized.chapters:
        assert refrain in chapter.content
        assert chapter.content.strip()


def test_pdf_normalizer_strips_long_title_headers_but_keeps_body_refrains():
    """Long Title-by-Author headers strip across multi-chapter books; lowercase refrains stay."""
    long_header = "The Life and Times of Michael K by J. M. Coetzee"
    refrain = "the wind rises"
    chapter_count = 15
    document = _pdf_chapters(
        *(
            f"{long_header}\n\n"
            f"Unique opening prose for chapter {index} continues across the page.\n\n"
            f"{refrain}\n\n"
            f"More narrative unique to chapter {index} wraps the scene."
            for index in range(1, chapter_count + 1)
        )
    )

    normalized = normalize_extracted_document(document, source_type="pdf")

    assert len(normalized.chapters) == chapter_count
    for chapter in normalized.chapters:
        assert long_header not in chapter.content
        assert refrain in chapter.content
        assert "Unique opening prose" in chapter.content


def test_pdf_normalizer_retains_titlecase_refrain_across_chapters():
    """A recurring standalone Title-Case body line is prose, not a running header.

    Bug 1: `The Wind Rises` appears once per chapter with no page-number or
    ALL-CAPS/by-author corroboration, so it must survive in every chapter.
    """
    refrain = "The Wind Rises"
    chapter_count = 12
    document = _pdf_chapters(
        *(
            f"Unique opening prose for chapter {index} continues across the page.\n\n"
            f"{refrain}\n\n"
            f"More narrative unique to chapter {index} wraps the scene."
            for index in range(1, chapter_count + 1)
        )
    )

    normalized = normalize_extracted_document(document, source_type="pdf")

    assert len(normalized.chapters) == chapter_count
    for chapter in normalized.chapters:
        assert refrain in chapter.content
        assert "Unique opening prose" in chapter.content


def test_pdf_normalizer_retains_recurring_titlecase_subheading():
    """A recurring one-word Title-Case subheading (`Summary`) is not a running header."""
    subheading = "Summary"
    chapter_count = 12
    document = _pdf_chapters(
        *(
            f"Opening narrative for chapter {index} sets the scene distinctly.\n\n"
            f"{subheading}\n\n"
            f"Closing narrative for chapter {index} resolves the thread."
            for index in range(1, chapter_count + 1)
        )
    )

    normalized = normalize_extracted_document(document, source_type="pdf")

    assert len(normalized.chapters) == chapter_count
    for chapter in normalized.chapters:
        assert subheading in chapter.content


def test_pdf_normalizer_retains_uppercase_roman_letter_words():
    """Standalone uppercase words built from roman-numeral letters are body text.

    Bug 2: `I`, `DID`, `MIX`, `MILD`, `VIVID`, `CIVIC` must never be stripped as
    page numbers just because their letters come from {i,v,x,l,c,d,m}.
    """
    words = ["I", "DID", "MIX", "MILD", "VIVID", "CIVIC"]
    body = "\n\n".join(
        f"{word}\n\nContext follows on the next narrative sentence here."
        for word in words
    )
    document = _pdf_chapters(body)

    normalized = normalize_extracted_document(document, source_type="pdf")

    content = normalized.chapters[0].content
    for word in words:
        assert re.search(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", content), word


def test_pdf_normalizer_still_strips_digit_and_lowercase_roman_page_numbers():
    """Legitimate page-number stripping (digits, lowercase roman) is preserved."""
    document = _pdf_chapters(
        "Chapter body one continues here.\n\n12\n\n"
        "More chapter body follows along.\n\niv"
    )

    normalized = normalize_extracted_document(document, source_type="pdf")

    content = normalized.chapters[0].content
    assert "Chapter body one continues here." in content
    assert "More chapter body follows along." in content
    assert "12" not in content
    assert not re.search(r"(?<![A-Za-z])iv(?![A-Za-z])", content)
