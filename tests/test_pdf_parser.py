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
    ]
    assert [chapter.start_page for chapter in document.chapters] == [3, 4]


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

    document = PDFParser(pdf_path, extraction_backend="fitz").parse(max_chapter_words=10_000)

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

    document = PDFParser(pdf_path, extraction_backend="fitz").parse(max_chapter_words=10_000)

    assert [chapter.title for chapter in document.chapters] == ["Full Document"]


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
        return False
    
    # Test 2: Invalid file handling
    print("\n[3] Testing invalid file handling...")
    try:
        PDFParser("/nonexistent/file.pdf")
        print("✗ Should have raised FileNotFoundError")
        return False
    except FileNotFoundError:
        print("✓ FileNotFoundError raised correctly")
    
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
        return False
    
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
        return False
    
    # Test 5: Chapter extraction
    print("\n[6] Testing chapter extraction...")
    try:
        assert len(doc.chapters) == 3, f"Expected 3 chapters, got {len(doc.chapters)}"
        print(f"✓ Extracted {len(doc.chapters)} chapters")
        for ch in doc.chapters:
            print(f"  - Chapter {ch.number}: {ch.title} (p.{ch.start_page}-{ch.end_page})")
    except Exception as e:
        print(f"✗ Chapter extraction failed: {e}")
        return False
    
    # Test 6: Page count
    print("\n[7] Testing page count...")
    try:
        page_count = parser.get_page_count()
        assert page_count == 3, f"Expected 3 pages, got {page_count}"
        print(f"✓ Page count: {page_count}")
    except Exception as e:
        print(f"✗ Page count failed: {e}")
        return False
    
    # Test 7: Markdown content
    print("\n[8] Testing markdown content...")
    try:
        markdown = parser.get_markdown()
        assert len(markdown) > 0, "Markdown should not be empty"
        assert "Chapter" in markdown, "Markdown should contain 'Chapter'"
        print(f"✓ Markdown content looks good ({len(markdown)} chars)")
        print("  Preview (first 200 chars):")
        print(f"  {markdown[:200]}...")
    except Exception as e:
        print(f"✗ Markdown test failed: {e}")
        return False
    
    # Cleanup
    print("\n" + "="*50)
    print("ALL TESTS PASSED ✓")
    print("="*50 + "\n")
    
    return True


if __name__ == "__main__":
    success = test_pdf_parser()
    sys.exit(0 if success else 1)
