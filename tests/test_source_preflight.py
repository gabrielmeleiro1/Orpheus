from __future__ import annotations

from pathlib import Path

from modules.ingestion.preflight import preflight_source
from modules.ingestion.pdf_preflight import analyze_pdf_text_layer


def _create_pdf_with_text(output_path: Path, text: str, *, pages: int = 2) -> None:
    import fitz

    doc = fitz.open()
    line = " ".join(text.split())[:72]
    for _ in range(pages):
        page = doc.new_page()
        for index in range(36):
            page.insert_text((72, 72 + (index * 16)), line, fontsize=10)
    doc.set_metadata({"title": "Native Text Fixture", "author": "Fixture Author"})
    doc.save(output_path)
    doc.close()


def _create_image_only_pdf(output_path: Path, *, pages: int = 2) -> None:
    import fitz

    doc = fitz.open()
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 10, 10), False)
    pixmap.clear_with(230)
    for _ in range(pages):
        page = doc.new_page()
        page.insert_image(page.rect, pixmap=pixmap)
    doc.set_metadata({"title": "Scanned Fixture", "author": "Scanner"})
    doc.save(output_path)
    doc.close()


def _create_mixed_text_image_pdf(output_path: Path) -> None:
    import fitz

    doc = fitz.open()
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
    pixmap.clear_with(180)
    page = doc.new_page()
    page.insert_image(fitz.Rect(250, 100, 550, 500), pixmap=pixmap)
    for line_number in range(3):
        page.insert_text(
            (72, 72 + (line_number * 18)),
            "Native prose remains extractable even when a page contains a large illustration.",
            fontsize=10,
        )
    doc.save(output_path)
    doc.close()


def _create_preflight_story_collection_epub(output_path: Path) -> None:
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("preflight-story-collection")
    book.set_title("Preflight Stories Fixture")
    book.set_language("en")
    book.add_author("Fixture Author")

    copyright_page = epub.EpubHtml(title="Copyright", file_name="copyright.xhtml", lang="en")
    copyright_page.content = """
    <html><body><p>Copyright metadata with enough text to be readable, but not
    enough relevance to become a story chapter in the preflight count.</p></body></html>
    """

    story_one = epub.EpubHtml(title="First Story", file_name="story-one.xhtml", lang="en")
    story_one.content = """
    <html><body><h1>First Story</h1><p>First story sample text that should be
    included in the EPUB preflight sample and counted as one chapter.</p></body></html>
    """

    story_two = epub.EpubHtml(title="Second Story", file_name="story-two.xhtml", lang="en")
    story_two.content = """
    <html><body><h1>Second Story</h1><p>Second story sample text that should be
    included in the EPUB preflight sample and counted as one chapter.</p></body></html>
    """

    about_author = epub.EpubHtml(title="About the Author", file_name="about.xhtml", lang="en")
    about_author.content = """
    <html><body><h1>About the Author</h1><p>Biography text that is readable but
    should not be counted as a story chapter.</p></body></html>
    """

    documents = [copyright_page, story_one, story_two, about_author]
    for document in documents:
        book.add_item(document)

    book.toc = tuple(documents)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + documents
    epub.write_epub(str(output_path), book)


def test_preflight_plain_text_reports_metadata_sample_and_tiny_warning(tmp_path: Path) -> None:
    source = tmp_path / "brief_notes.txt"
    source.write_text("A very small note.", encoding="utf-8")

    payload = preflight_source(source)

    assert payload["title"] == "brief_notes"
    assert payload["author"] is None
    assert payload["source_type"] == "txt"
    assert payload["chapter_count"] == 1
    assert payload["sample"] == "A very small note."
    assert any("very small" in warning for warning in payload["warnings"])


def test_preflight_markdown_uses_first_heading_and_counts_sections(tmp_path: Path) -> None:
    source = tmp_path / "guide.md"
    source.write_text(
        "# Guide Title\n\nOpening context.\n\n## Install\n\nInstall steps.\n\n## Run\n\nRun steps.",
        encoding="utf-8",
    )

    payload = preflight_source(source)

    assert payload["title"] == "Guide Title"
    assert payload["source_type"] == "md"
    assert payload["chapter_count"] == 3
    assert "Opening context." in payload["sample"]
    assert payload["warnings"] == []


def test_preflight_markdown_ignores_headings_inside_fenced_code(tmp_path: Path) -> None:
    for fence in ("```", "~~~"):
        source = tmp_path / f"fenced-{len(fence)}.md"
        source.write_text(
            f"# Guide\n\nIntro text.\n\n{fence}\n## Not a chapter\n{fence}\n\n## Real chapter\nBody text.",
            encoding="utf-8",
        )

        payload = preflight_source(source)

        assert payload["title"] == "Guide"
        assert payload["chapter_count"] == 2
        assert "## Not a chapter" in payload["sample"]


def test_preflight_text_decodes_utf16_sample_without_nul_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "utf16_notes.txt"
    source.write_bytes("Too much sanity may be madness.\n\nSecond paragraph.".encode("utf-16"))

    payload = preflight_source(source)

    assert payload["source_type"] == "txt"
    assert payload["chapter_count"] == 1
    assert payload["sample"].startswith("Too much sanity")
    assert "Second paragraph." in payload["sample"]
    assert "\x00" not in payload["sample"]
    assert "ÿþ" not in payload["sample"]


def test_pdf_text_layer_preflight_classifies_native_text_pdf(tmp_path: Path) -> None:
    source = tmp_path / "native-text.pdf"
    _create_pdf_with_text(source, "Native PDF text layer with enough readable words. " * 80)

    analysis = analyze_pdf_text_layer(source)
    payload = preflight_source(source)

    assert analysis.classification == "native_text"
    assert analysis.requires_ocr is False
    assert analysis.confidence >= 0.8
    assert analysis.text_chars >= 1000
    assert payload["requires_ocr"] is False
    assert payload["pdf_text_layer"]["classification"] == "native_text"
    assert payload["pdf_text_layer"]["confidence"] >= 0.8
    assert not any("OCR required" in warning for warning in payload["warnings"])


def test_pdf_preflight_warns_when_pdf_requires_ocr(tmp_path: Path) -> None:
    source = tmp_path / "scanned.pdf"
    _create_image_only_pdf(source)

    analysis = analyze_pdf_text_layer(source)
    payload = preflight_source(source)

    assert analysis.classification == "ocr_required"
    assert analysis.requires_ocr is True
    assert analysis.confidence >= 0.9
    assert analysis.text_chars == 0
    assert analysis.image_pages == analysis.sampled_pages
    assert payload["requires_ocr"] is True
    assert payload["chapter_count"] == 0
    assert payload["sample"] == ""
    assert payload["pdf_text_layer"]["classification"] == "ocr_required"
    assert payload["pdf_text_layer"]["confidence"] >= 0.9
    assert any("OCR required" in warning for warning in payload["warnings"])
    assert any("not text-extractable" in warning for warning in payload["warnings"])


def test_pdf_preflight_does_not_require_ocr_for_mixed_native_text_and_image(tmp_path: Path) -> None:
    source = tmp_path / "mixed.pdf"
    _create_mixed_text_image_pdf(source)

    analysis = analyze_pdf_text_layer(source)

    assert analysis.requires_ocr is False
    assert analysis.text_chars > 100
    assert analysis.word_count > 20
    assert analysis.image_pages == 1


def test_preflight_markdown_decodes_utf16_headings_without_nul_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "utf16_guide.md"
    source.write_bytes("# Guide Title\n\nOpening context.\n\n## Run\n\nRun steps.".encode("utf-16"))

    payload = preflight_source(source)

    assert payload["title"] == "Guide Title"
    assert payload["source_type"] == "md"
    assert payload["chapter_count"] == 2
    assert "Opening context." in payload["sample"]
    assert "\x00" not in payload["sample"]


def test_preflight_epub_counts_story_sections_not_structural_pages(tmp_path: Path) -> None:
    source = tmp_path / "preflight-stories.epub"
    _create_preflight_story_collection_epub(source)

    payload = preflight_source(source)

    assert payload["title"] == "Preflight Stories Fixture"
    assert payload["source_type"] == "epub"
    assert payload["chapter_count"] == 2
    assert "First story sample text" in payload["sample"]
    assert "Second story sample text" in payload["sample"]
    assert "Copyright metadata" not in payload["sample"]
    assert "Biography text" not in payload["sample"]


def test_preflight_rejects_unsupported_source_type(tmp_path: Path) -> None:
    source = tmp_path / "notes.docx"
    source.write_text("not supported", encoding="utf-8")

    try:
        preflight_source(source)
    except ValueError as exc:
        assert "Unsupported source type" in str(exc)
    else:
        raise AssertionError("expected unsupported source to fail")
