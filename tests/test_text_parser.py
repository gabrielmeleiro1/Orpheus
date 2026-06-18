from __future__ import annotations

from modules.ingestion.models import Chapter as IngestionChapter
from modules.ingestion.models import Document as IngestionDocument
from modules.ingestion.text_parser import TextParser


def test_text_parser_reads_plain_text_into_single_chapter(tmp_path):
    source_path = tmp_path / "notes.txt"
    source_path.write_text(
        "First paragraph.\n\nSecond paragraph with more detail.\n",
        encoding="utf-8",
    )

    document = TextParser(source_path).parse()

    assert isinstance(document, IngestionDocument)
    assert all(isinstance(chapter, IngestionChapter) for chapter in document.chapters)
    assert document.title == "notes"
    assert document.author is None
    assert document.raw_markdown.splitlines() == source_path.read_text(encoding="utf-8").splitlines()
    assert len(document.chapters) == 1
    assert document.chapters[0].number == 1
    assert document.chapters[0].title == "notes"
    assert "First paragraph." in document.chapters[0].content
    assert "Second paragraph with more detail." in document.chapters[0].content


def test_text_parser_decodes_utf16_sources_without_nul_artifacts(tmp_path):
    source_path = tmp_path / "utf16_notes.txt"
    source_text = "Too much sanity may be madness.\n\nSecond paragraph with more detail.\n"
    source_path.write_bytes(source_text.encode("utf-16"))

    document = TextParser(source_path).parse()

    assert document.raw_markdown == source_text
    assert "\x00" not in document.raw_markdown
    assert document.chapters[0].content.startswith("Too much sanity")
    assert "Second paragraph" in document.chapters[0].content
