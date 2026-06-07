from __future__ import annotations

from modules.ingestion.models import Chapter as IngestionChapter
from modules.ingestion.models import Document as IngestionDocument
from modules.ingestion.markdown_parser import MarkdownParser


def test_markdown_parser_splits_headings_into_chapters(tmp_path):
    source_path = tmp_path / "guide.md"
    source_path.write_text(
        "# Guide\n\nOverview text.\n\n## Install\n\nInstall steps.\n\n## Usage\n\nUsage notes.\n",
        encoding="utf-8",
    )

    document = MarkdownParser(source_path).parse()

    assert isinstance(document, IngestionDocument)
    assert all(isinstance(chapter, IngestionChapter) for chapter in document.chapters)
    assert document.title == "Guide"
    assert document.author is None
    assert document.raw_markdown == source_path.read_text(encoding="utf-8")
    assert [chapter.title for chapter in document.chapters] == ["Guide", "Install", "Usage"]
    assert "Overview text." in document.chapters[0].content
    assert "Install steps." in document.chapters[1].content
    assert "Usage notes." in document.chapters[2].content


def test_markdown_parser_falls_back_to_single_chapter_without_headings(tmp_path):
    source_path = tmp_path / "plain.md"
    source_path.write_text(
        "Paragraph one.\n\nParagraph two.\n",
        encoding="utf-8",
    )

    document = MarkdownParser(source_path).parse()

    assert document.title == "plain"
    assert len(document.chapters) == 1
    assert document.chapters[0].title == "plain"
    assert "Paragraph one." in document.chapters[0].content
    assert "Paragraph two." in document.chapters[0].content


def test_markdown_parser_decodes_utf16_sources_and_splits_headings(tmp_path):
    source_path = tmp_path / "utf16_guide.md"
    source_text = "# Guide\n\nOverview text.\n\n## Usage\n\nUsage notes.\n"
    source_path.write_bytes(source_text.encode("utf-16"))

    document = MarkdownParser(source_path).parse()

    assert document.raw_markdown == source_text
    assert "\x00" not in document.raw_markdown
    assert document.title == "Guide"
    assert [chapter.title for chapter in document.chapters] == ["Guide", "Usage"]
    assert "Overview text." in document.chapters[0].content
