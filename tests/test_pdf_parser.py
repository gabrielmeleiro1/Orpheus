"""
Test PDF Parser Module
======================
Unit tests for the PDF parser using a generated sample PDF.
"""

import sys
import tempfile
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from modules.ingestion.pdf_parser import PDFParser, Document


def create_sample_pdf(output_path: Path) -> None:
    """
    Create a simple sample PDF for testing.
    
    Uses PyMuPDF (fitz) to generate a multi-page PDF with chapters.
    """
    import fitz  # PyMuPDF
    
    doc = fitz.open()
    
    # Set metadata
    doc.set_metadata({
        "title": "Sample Audiobook Test",
        "author": "Test Author",
        "subject": "PDF Parser Test"
    })
    
    # Chapter 1
    page1 = doc.new_page()
    text1 = """Chapter 1: The Beginning

This is the first chapter of our sample audiobook.
It contains several paragraphs of text that will be
parsed and converted to markdown format.

Lorem ipsum dolor sit amet, consectetur adipiscing elit.
Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua."""
    page1.insert_text((72, 72), text1, fontsize=12)
    
    # Chapter 2  
    page2 = doc.new_page()
    text2 = """Chapter 2: The Middle

This is the second chapter with more content.
We are testing the parser's ability to extract text
from multiple pages correctly.

Ut enim ad minim veniam, quis nostrud exercitation
ullamco laboris nisi ut aliquip ex ea commodo consequat."""
    page2.insert_text((72, 72), text2, fontsize=12)
    
    # Chapter 3
    page3 = doc.new_page()
    text3 = """Chapter 3: The End

This is the final chapter of our test document.
It concludes the sample PDF with a proper ending.

Duis aute irure dolor in reprehenderit in voluptate
velit esse cillum dolore eu fugiat nulla pariatur."""
    page3.insert_text((72, 72), text3, fontsize=12)
    
    # Add a table of contents
    toc = [
        [1, "Chapter 1: The Beginning", 1],
        [1, "Chapter 2: The Middle", 2],
        [1, "Chapter 3: The End", 3]
    ]
    doc.set_toc(toc)
    
    doc.save(output_path)
    doc.close()
    print(f"✓ Created sample PDF: {output_path}")


def _write_pdf_with_pages(output_path: Path, pages: list[str], toc: list[list[object]] | None = None) -> None:
    import fitz

    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=12)
    if toc:
        doc.set_toc(toc)
    doc.save(output_path)
    doc.close()


def test_pdf_parser_prefers_numbered_toc_chapters_over_part_containers(tmp_path: Path) -> None:
    pdf_path = tmp_path / "handbook.pdf"
    _write_pdf_with_pages(
        pdf_path,
        [
            "Contents\nPART I\n1. First Essay\n2. Second Essay\nSubject Index",
            "PART I: BIOGRAPHY\nContainer page.",
            "chapter 1\nfirst essay\nThis is the first essay body.",
            "chapter 2\nsecond essay\nThis is the second essay body.",
            "Subject Index\nA\nB\nC",
        ],
        toc=[
            [1, "Contents", 1],
            [1, "PART I: BIOGRAPHY", 2],
            [2, "1. First Essay by A. Writer", 3],
            [2, "2. Second Essay by B. Writer", 4],
            [1, "Subject Index", 5],
            [2, "A", 5],
        ],
    )

    document = PDFParser(pdf_path, extraction_backend="fitz").parse()

    assert [chapter.title for chapter in document.chapters] == [
        "1. First Essay by A. Writer",
        "2. Second Essay by B. Writer",
        "Subject Index",
    ]
    assert [chapter.start_page for chapter in document.chapters] == [3, 4, 5]


def test_pdf_parser_splits_large_no_toc_anthology_on_page_headings(tmp_path: Path) -> None:
    pdf_path = tmp_path / "anthology.pdf"
    page_texts = [
        "Anthology\n\n1\n\nContents\n1. History........7\ni. A first selection........9",
        "Anthology\n\n2\n\nPrologue\nThis opening section is long enough to be readable.",
        "Anthology\n\n3\n\nThe prologue continues with body text.",
        "Anthology\n\n4\n\nFrom Book V of the Histories by Herodotus\nIntro note.\nSelection body.",
        "Anthology\n\n5\n\nThis selection continues with more body text.",
        "Anthology\n\n6\n\nA passage from In Praise of Folly by Erasmus\nIntro note.\nSelection body.",
    ]
    _write_pdf_with_pages(pdf_path, page_texts)

    document = PDFParser(pdf_path, extraction_backend="fitz").parse()

    assert [chapter.title for chapter in document.chapters] == [
        "Prologue",
        "From Book V of the Histories by Herodotus",
        "A passage from In Praise of Folly by Erasmus",
    ]
    assert [chapter.start_page for chapter in document.chapters] == [2, 4, 6]


def test_pdf_parser_does_not_split_no_toc_body_lines_that_start_with_lowercase_from(tmp_path: Path) -> None:
    pdf_path = tmp_path / "essay.pdf"
    _write_pdf_with_pages(
        pdf_path,
        [
            "Essay\n\n1\n\nfrom this point onward the body continues without a section heading.",
            "Essay\n\n2\n\nfrom under his feet the text continues as ordinary prose.",
        ],
    )

    document = PDFParser(pdf_path, extraction_backend="fitz").parse()

    assert [chapter.title for chapter in document.chapters] == ["essay"]


def test_chapters_from_entries_gives_a_shared_start_page_to_one_chapter() -> None:
    chapters = PDFParser._chapters_from_entries(
        [("Alpha", 5), ("Beta", 5), ("Gamma", 8)],
        total_pages=10,
    )

    assert [
        (chapter.title, chapter.start_page, chapter.end_page) for chapter in chapters
    ] == [
        ("Alpha", 5, 7),
        ("Gamma", 8, 10),
    ]


def _page_marker_counts(document: Document, page_count: int) -> list[int]:
    joined = "\n".join(chapter.content for chapter in document.chapters)
    return [joined.count(f"Marker page {page}.") for page in range(1, page_count + 1)]


def test_pdf_parser_narrates_a_page_shared_by_two_outline_entries_once(tmp_path: Path) -> None:
    pdf_path = tmp_path / "sections.pdf"
    _write_pdf_with_pages(
        pdf_path,
        [f"Marker page {page}." for page in range(1, 6)],
        toc=[[1, "Contents", 1], [2, "A section", 2], [2, "B section", 2], [2, "C section", 4]],
    )

    document = PDFParser(pdf_path, extraction_backend="fitz").parse()

    assert [chapter.title for chapter in document.chapters] == ["A section", "C section"]
    assert _page_marker_counts(document, 5) == [0, 1, 1, 1, 1]


def test_pdf_parser_keeps_parent_opening_pages_before_the_selected_outline_level(tmp_path: Path) -> None:
    pdf_path = tmp_path / "nested.pdf"
    _write_pdf_with_pages(
        pdf_path,
        [f"Marker page {page}." for page in range(1, 9)],
        toc=[
            [1, "Contents", 1],
            [1, "Chapter One", 2],
            [2, "A section", 3],
            [2, "B section", 3],
            [1, "Chapter Two", 5],
            [2, "C section", 6],
            [1, "Index", 8],
        ],
    )

    document = PDFParser(pdf_path, extraction_backend="fitz").parse()

    assert [chapter.title for chapter in document.chapters] == ["Chapter One", "A section", "C section", "Index"]
    assert _page_marker_counts(document, 8) == [0, 1, 1, 1, 1, 1, 1, 1]


def test_pdf_parser_keeps_unnumbered_front_matter_before_numbered_chapters(tmp_path: Path) -> None:
    pdf_path = tmp_path / "numbered.pdf"
    _write_pdf_with_pages(
        pdf_path,
        [f"Marker page {page}." for page in range(1, 8)],
        toc=[
            [1, "Copyright", 1],
            [1, "Contents", 2],
            [1, "Foreword", 3],
            [1, "1 Beginnings", 5],
            [1, "2 Endings", 6],
        ],
    )

    document = PDFParser(pdf_path, extraction_backend="fitz").parse()

    assert [chapter.title for chapter in document.chapters] == ["Foreword", "1 Beginnings", "2 Endings"]
    assert _page_marker_counts(document, 7) == [0, 0, 1, 1, 1, 1, 1]


@pytest.mark.parametrize(
    ("toc", "titles", "first_body_page"),
    [
        pytest.param(
            [[1, "The Lighthouse Keeper", 1], [2, "Chapter 1", 4], [2, "Chapter 2", 6], [2, "Chapter 3", 8]],
            ["Chapter 1", "Chapter 2", "Chapter 3"],
            4,
            id="book-title-root",
        ),
        pytest.param(
            [[1, "Cover", 1], [1, "1 The Start", 4], [1, "2 The Middle", 6], [1, "3 The End", 8]],
            ["1 The Start", "2 The Middle", "3 The End"],
            4,
            id="cover",
        ),
        pytest.param(
            [[1, "Title Page", 1], [1, "Copyright", 2], [1, "Contents", 3], [1, "Dedication", 4],
             [1, "1 The Start", 5], [1, "2 The Middle", 7]],
            ["1 The Start", "2 The Middle"],
            5,
            id="dedication",
        ),
        pytest.param(
            [[1, "Contents", 3], [1, "Also by Jane Doe", 4], [1, "1 The Start", 5], [1, "2 The Middle", 7]],
            ["1 The Start", "2 The Middle"],
            5,
            id="also-by",
        ),
    ],
)
def test_pdf_parser_does_not_narrate_outlined_front_matter_as_a_lead_chapter(
    tmp_path: Path, toc, titles, first_body_page,
) -> None:
    pdf_path = tmp_path / "front.pdf"
    _write_pdf_with_pages(pdf_path, [f"Marker page {page}." for page in range(1, 10)], toc=toc)

    document = PDFParser(pdf_path, extraction_backend="fitz").parse()

    assert [chapter.title for chapter in document.chapters] == titles
    assert _page_marker_counts(document, 9) == [int(page >= first_body_page) for page in range(1, 10)]


def test_pdf_parser_keeps_a_foreword_before_part_dividers(tmp_path: Path) -> None:
    pdf_path = tmp_path / "parts.pdf"
    _write_pdf_with_pages(
        pdf_path,
        [f"Marker page {page}." for page in range(1, 9)],
        toc=[
            [1, "Copyright", 1],
            [1, "Contents", 2],
            [1, "Foreword", 3],
            [1, "Part I The Sea", 5],
            [2, "1 The Start", 6],
            [2, "2 The Middle", 7],
        ],
    )

    document = PDFParser(pdf_path, extraction_backend="fitz").parse()

    assert [chapter.title for chapter in document.chapters] == ["Foreword", "1 The Start", "2 The Middle"]
    assert _page_marker_counts(document, 8) == [0, 0, 1, 1, 1, 1, 1, 1]


def test_pdf_parser_keeps_a_parent_chapter_opening_on_the_first_page(tmp_path: Path) -> None:
    pdf_path = tmp_path / "nested.pdf"
    _write_pdf_with_pages(
        pdf_path,
        [f"Marker page {page}." for page in range(1, 7)],
        toc=[
            [1, "Chapter One", 1],
            [2, "A section", 2],
            [2, "B section", 3],
            [1, "Chapter Two", 4],
            [2, "C section", 5],
        ],
    )

    document = PDFParser(pdf_path, extraction_backend="fitz").parse()

    assert [chapter.title for chapter in document.chapters] == ["Chapter One", "A section", "B section", "C section"]
    assert _page_marker_counts(document, 6) == [1] * 6


def test_validate_pdf_accepts_valid_pdf_smaller_than_tail_window(tmp_path: Path) -> None:
    from modules.errors import validate_pdf

    pdf_path = tmp_path / "tiny.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n" + b"0" * 64 + b"\n%%EOF\n")
    assert 25 <= pdf_path.stat().st_size < 1024

    assert validate_pdf(pdf_path) is True


def test_pdf_parser():
    """Run all PDF parser tests."""
    
    print("\n" + "="*50)
    print("PDF PARSER TEST SUITE")
    print("="*50 + "\n")

    with tempfile.TemporaryDirectory(prefix="orpheus-pdf-parser-") as temp_dir:
        sample_pdf = Path(temp_dir) / "sample.pdf"

        # Create sample PDF in isolated temp output (never tracked fixtures).
        print("[1] Creating sample PDF...")
        create_sample_pdf(sample_pdf)
        return _run_pdf_parser_checks(sample_pdf)


def _run_pdf_parser_checks(sample_pdf: Path) -> bool:
    
    # Test 1: File validation
    print("\n[2] Testing file validation...")
    try:
        parser = PDFParser(sample_pdf)
        print("✓ PDFParser initialized successfully")
    except Exception as e:
        print(f"✗ Failed to initialize parser: {e}")
        raise
    
    # Test 3: Parse document
    print("\n[4] Testing document parsing...")
    try:
        doc = parser.parse()
        assert isinstance(doc, Document), "parse() should return Document"
        print("✓ Parsed document successfully")
        print(f"  - Title: {doc.title}")
        print(f"  - Author: {doc.author}")
        print(f"  - Pages: {doc.total_pages}")
        print(f"  - Chapters: {len(doc.chapters)}")
        print(f"  - Markdown length: {len(doc.raw_markdown)} chars")
    except Exception as e:
        print(f"✗ Failed to parse document: {e}")
        raise
    
    # Test 4: TOC extraction
    print("\n[5] Testing TOC extraction...")
    try:
        toc = parser.extract_toc()
        assert len(toc) == 3, f"Expected 3 TOC entries, got {len(toc)}"
        print(f"✓ Extracted {len(toc)} TOC entries")
        for level, title, page in toc:
            print(f"  - [{level}] {title} (p.{page})")
    except Exception as e:
        print(f"✗ Failed to extract TOC: {e}")
        raise
    
    # Test 5: Chapter extraction
    print("\n[6] Testing chapter extraction...")
    try:
        assert len(doc.chapters) == 3, f"Expected 3 chapters, got {len(doc.chapters)}"
        print(f"✓ Extracted {len(doc.chapters)} chapters")
        for ch in doc.chapters:
            print(f"  - Chapter {ch.number}: {ch.title} (p.{ch.start_page}-{ch.end_page})")
    except Exception as e:
        print(f"✗ Chapter extraction failed: {e}")
        raise
    
    # Test 6: Page count
    print("\n[7] Testing page count...")
    try:
        page_count = doc.total_pages
        assert page_count == 3, f"Expected 3 pages, got {page_count}"
        print(f"✓ Page count: {page_count}")
    except Exception as e:
        print(f"✗ Page count failed: {e}")
        raise
    
    # Test 7: Markdown content
    print("\n[8] Testing markdown content...")
    try:
        markdown = doc.raw_markdown
        assert len(markdown) > 0, "Markdown should not be empty"
        assert "Chapter" in markdown, "Markdown should contain 'Chapter'"
        print(f"✓ Markdown content looks good ({len(markdown)} chars)")
        print("  Preview (first 200 chars):")
        print(f"  {markdown[:200]}...")
    except Exception as e:
        print(f"✗ Markdown test failed: {e}")
        raise
    
    # Cleanup
    print("\n" + "="*50)
    print("ALL TESTS PASSED ✓")
    print("="*50 + "\n")
    
    return True


if __name__ == "__main__":
    success = test_pdf_parser()
    sys.exit(0 if success else 1)
