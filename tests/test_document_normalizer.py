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
