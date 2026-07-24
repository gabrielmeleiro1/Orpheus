from __future__ import annotations

import pytest

from modules.errors import CorruptedFileError
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
    assert document.raw_markdown.splitlines() == source_path.read_text(encoding="utf-8").splitlines()
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


def test_markdown_parser_raises_on_whitespace_only_source(tmp_path):
    source_path = tmp_path / "blank.md"
    source_path.write_text("   \n\n \t\n", encoding="utf-8")

    with pytest.raises(CorruptedFileError):
        MarkdownParser(source_path).parse()


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_markdown_parser_ignores_headings_inside_fenced_code(tmp_path, fence):
    source_path = tmp_path / "fenced.md"
    source_path.write_text(
        f"# Guide\n\nIntro text.\n\n{fence}python\n## Not a chapter\n{fence}\n\n## Real chapter\nBody text.\n",
        encoding="utf-8",
    )

    document = MarkdownParser(source_path).parse()

    assert [chapter.title for chapter in document.chapters] == ["Guide", "Real chapter"]
    assert "## Not a chapter" in document.chapters[0].content


def test_markdown_parser_strips_leading_yaml_front_matter_from_chapter_speech(tmp_path):
    source_path = tmp_path / "front-matter.md"
    source_path.write_text(
        "---\n"
        "title: Front Matter Book\n"
        "author: Example Author\n"
        "---\n"
        "# Chapter One\n\n"
        "Spoken prose.\n",
        encoding="utf-8",
    )

    document = MarkdownParser(source_path).parse()

    assert [chapter.title for chapter in document.chapters] == ["Chapter One"]
    assert document.chapters[0].content == "Spoken prose."
    assert "Front Matter Book" not in document.chapters[0].content
    assert "Example Author" not in document.chapters[0].content


def test_markdown_parser_preserves_unclosed_front_matter_and_later_thematic_breaks(tmp_path):
    unclosed_path = tmp_path / "unclosed.md"
    unclosed_path.write_text(
        "---\n"
        "title: This is still prose\n"
        "# Chapter One\n\n"
        "Spoken prose.\n",
        encoding="utf-8",
    )
    unclosed = MarkdownParser(unclosed_path).parse()
    assert "title: This is still prose" in unclosed.chapters[0].content

    thematic_path = tmp_path / "thematic.md"
    thematic_path.write_text(
        "# Chapter One\n\n"
        "Before the break.\n\n"
        "---\n\n"
        "After the break.\n\n"
        "## Chapter Two\n\n"
        "Second chapter.\n",
        encoding="utf-8",
    )
    thematic = MarkdownParser(thematic_path).parse()
    assert [chapter.title for chapter in thematic.chapters] == ["Chapter One", "Chapter Two"]
    assert "---" in thematic.chapters[0].content


def test_markdown_parser_strips_front_matter_from_utf16_source(tmp_path):
    source_path = tmp_path / "utf16-front-matter.md"
    source_path.write_bytes(
        (
            "---\n"
            "title: UTF-16 Book\n"
            "---\n"
            "# Chapter One\n\n"
            "Encoded prose.\n"
        ).encode("utf-16")
    )

    document = MarkdownParser(source_path).parse()

    assert document.chapters[0].content == "Encoded prose."
    assert "\x00" not in document.chapters[0].content
