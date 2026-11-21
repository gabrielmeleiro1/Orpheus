
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
from modules.ingestion.models import Chapter as IngestionChapter
from modules.ingestion.models import Document as IngestionDocument
from modules.pipeline.stages.ingestion import ingest_source
from modules.ingestion.pdf_parser import PDFParser, Document
from modules.errors import OCRRequiredPDFError


def _create_image_only_pdf(output_path: Path, *, pages: int = 2) -> None:
    import fitz

    doc = fitz.open()
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 10, 10), False)
    pixmap.clear_with(230)
    for _ in range(pages):
        page = doc.new_page()
        page.insert_image(page.rect, pixmap=pixmap)
    doc.save(output_path)
    doc.close()

class TestPDFParser:
    @pytest.fixture
    def mock_parser_deps(self):
        with patch("modules.ingestion.pdf_parser.pymupdf4llm") as mock_llm, \
             patch("fitz.open") as mock_open, \
             patch("modules.ingestion.pdf_parser.validate_pdf", return_value=True):
            
            # Setup pymupdf4llm to return page chunks
            mock_llm.to_markdown.return_value = [
                {"text": "Page 1 Content"},
                {"text": "Page 2 Content"},
                {"text": "Page 3 Content"}
            ]
            
            # Setup fitz doc
            mock_doc = MagicMock()
            mock_doc.__len__.return_value = 3
            mock_doc.metadata = {"title": "Test Book", "author": "Tester"}
            # Return TOC: 2 chapters. Ch1 starts pg1, Ch2 starts pg3
            # TOC format: [level, title, page_num]
            mock_doc.get_toc.return_value = [
                [1, "Chapter 1", 1],
                [1, "Chapter 2", 3]
            ]
            
            mock_open.return_value.__enter__.return_value = mock_doc
            
            yield mock_open

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            PDFParser("nonexistent.pdf")

    @pytest.mark.usefixtures("mock_parser_deps")
    def test_parse_success(self, sample_pdf):
        parser = PDFParser(sample_pdf)
        doc = parser.parse()
        
        assert isinstance(doc, Document)
        assert isinstance(doc, IngestionDocument)
        assert all(isinstance(chapter, IngestionChapter) for chapter in doc.chapters)
        assert doc.title == "Test Book"
        assert len(doc.chapters) == 2
        
        # Verify content mapping
        # Ch1 is pg1-2 (inclusive logic in extract_chapters: start 1, next starts 3 -> end 2)
        # However, 0-indexed slicing:
        # Ch1: start_page=1 (idx 0), end_page=2. Slice [0:2] -> Page 1, Page 2
        assert "Page 1 Content" in doc.chapters[0].content
        assert "Page 2 Content" in doc.chapters[0].content
        assert "Page 3 Content" not in doc.chapters[0].content
        
        # Ch2: start_page=3 (idx 2), end_page=3. Slice [2:3] -> Page 3
        assert "Page 3 Content" in doc.chapters[1].content
        assert "Page 1 Content" not in doc.chapters[1].content

    def test_parse_strips_trailing_reference_block(self, sample_pdf):
        with patch("modules.ingestion.pdf_parser.pymupdf4llm") as mock_llm, \
             patch("fitz.open") as mock_open, \
             patch("modules.ingestion.pdf_parser.validate_pdf", return_value=True):
            mock_llm.to_markdown.return_value = [
                {
                    "text": (
                        "This is body text with inline marker.15\n\n"
                        "13. The concept of design space was proposed.\n"
                        "14. Throughout the text the term paradigm appears.\n"
                        "15. Chandler (1977) pp. 281-3."
                    )
                }
            ]

            mock_doc = MagicMock()
            mock_doc.__len__.return_value = 1
            mock_doc.metadata = {"title": "Test Book", "author": "Tester"}
            mock_doc.get_toc.return_value = [[1, "Chapter 1", 1]]
            mock_open.return_value.__enter__.return_value = mock_doc

            parser = PDFParser(sample_pdf)
            doc = parser.parse()

        content = doc.chapters[0].content
        assert "This is body text with inline marker.15" in content
        assert "13. The concept of design space was proposed." not in content
        assert "14. Throughout the text the term paradigm appears." not in content
        assert "15. Chandler (1977) pp. 281-3." not in content

    def test_parse_keeps_regular_numbered_list(self, sample_pdf):
        with patch("modules.ingestion.pdf_parser.pymupdf4llm") as mock_llm, \
             patch("fitz.open") as mock_open, \
             patch("modules.ingestion.pdf_parser.validate_pdf", return_value=True):
            mock_llm.to_markdown.return_value = [
                {
                    "text": (
                        "How to proceed:\n"
                        "1. Gather data\n"
                        "2. Build model\n"
                        "3. Ship"
                    )
                }
            ]

            mock_doc = MagicMock()
            mock_doc.__len__.return_value = 1
            mock_doc.metadata = {"title": "Test Book", "author": "Tester"}
            mock_doc.get_toc.return_value = [[1, "Chapter 1", 1]]
            mock_open.return_value.__enter__.return_value = mock_doc

            parser = PDFParser(sample_pdf)
            doc = parser.parse()

        content = doc.chapters[0].content
        assert "1. Gather data" in content
        assert "2. Build model" in content
        assert "3. Ship" in content

    def test_parse_strips_repeated_page_boundary_boilerplate(self, sample_pdf):
        with patch("modules.ingestion.pdf_parser.pymupdf4llm") as mock_llm, \
             patch("fitz.open") as mock_open, \
             patch("modules.ingestion.pdf_parser.validate_pdf", return_value=True):
            mock_llm.to_markdown.return_value = [
                {
                    "text": (
                        "RUNNING HEADER\n"
                        "Page one body stays here.\n"
                        "101\n"
                        "This content downloaded from 127.0.0.1 on Tue, 28 Apr 2026 10:00:00 UTC\n"
                        "All use subject to http://about.jstor.org/terms"
                    )
                },
                {
                    "text": (
                        "RUNNING HEADER\n"
                        "Page two body stays here.\n"
                        "102\n"
                        "This content downloaded from 127.0.0.1 on Tue, 28 Apr 2026 10:00:00 UTC\n"
                        "All use subject to http://about.jstor.org/terms"
                    )
                },
                {
                    "text": (
                        "RUNNING HEADER\n"
                        "Page three body stays here.\n"
                        "103\n"
                        "This content downloaded from 127.0.0.1 on Tue, 28 Apr 2026 10:00:00 UTC\n"
                        "All use subject to http://about.jstor.org/terms"
                    )
                },
            ]

            mock_doc = MagicMock()
            mock_doc.__len__.return_value = 3
            mock_doc.metadata = {"title": "Test Book", "author": "Tester"}
            mock_doc.get_toc.return_value = [[1, "Chapter 1", 1]]
            mock_open.return_value.__enter__.return_value = mock_doc

            parser = PDFParser(sample_pdf)
            doc = parser.parse()

        content = doc.chapters[0].content
        assert "Page one body stays here." in content
        assert "Page two body stays here." in content
        assert "Page three body stays here." in content
        assert "RUNNING HEADER" not in content
        assert "This content downloaded" not in content
        assert "All use subject" not in content
        assert "\n101\n" not in f"\n{content}\n"

    def test_parse_repairs_pdf_line_break_hyphenation_before_chunking(self, sample_pdf):
        with patch("modules.ingestion.pdf_parser.pymupdf4llm") as mock_llm, \
             patch("fitz.open") as mock_open, \
             patch("modules.ingestion.pdf_parser.validate_pdf", return_value=True):
            mock_llm.to_markdown.return_value = [
                {
                    "text": (
                        "The essence of the belief that bats have expe-\n"
                        " rience is that there is something it is like.\n"
                        "They perceive the sur-\n"
                        " rounding world through sonar.\n"
                        "Knowledge about bat neurophysiol-\n"
                        " ogy should not be spoken with gaps.\n"
                        "Mind and body-\n"
                        " why this matters should keep the dash.\n"
                        "Concepts refer beyond themselves-\n"
                        " toward the phenomenal point of view.\n"
                        "Imagine the non-\n"
                        " occurrence of the physical state."
                    )
                }
            ]

            mock_doc = MagicMock()
            mock_doc.__len__.return_value = 1
            mock_doc.metadata = {"title": "Test Book", "author": "Tester"}
            mock_doc.get_toc.return_value = [[1, "Chapter 1", 1]]
            mock_open.return_value.__enter__.return_value = mock_doc

            parser = PDFParser(sample_pdf)
            doc = parser.parse()

        content = doc.chapters[0].content
        assert "experience" in content
        assert "surrounding" in content
        assert "neurophysiology" in content
        assert "expe-" not in content
        assert "sur-" not in content
        assert "neurophysiol-" not in content
        assert "body-\n why" in content
        assert "themselves-\n toward" in content
        assert "non-\n occurrence" in content

    def test_parse_strips_page_number_residue_and_preserves_numbered_text(self, sample_pdf):
        with patch("modules.ingestion.pdf_parser.pymupdf4llm") as mock_llm, \
             patch("fitz.open") as mock_open, \
             patch("modules.ingestion.pdf_parser.validate_pdf", return_value=True):
            mock_llm.to_markdown.return_value = [
                {
                    "text": (
                        "It is often possible to take up a point of view other than one's own, so the\n"
                        "44I\n\n"
                        "comprehension of such facts is not limited to one's own case.\n"
                        "But if experience does not have an objective nature that can be apprehended from\n"
                        "I The problem I am going to raise can therefore be posed even if the distinction\n"
                        "between more subjective and more objective descriptions can itself be made only\n"
                        "within a larger human point of view.\n\n"
                        "many different points of view, then how can it be supposed?"
                    )
                }
            ]

            mock_doc = MagicMock()
            mock_doc.__len__.return_value = 1
            mock_doc.metadata = {"title": "Test Book", "author": "Tester"}
            mock_doc.get_toc.return_value = [[1, "Chapter 1", 1]]
            mock_open.return_value.__enter__.return_value = mock_doc

            parser = PDFParser(sample_pdf)
            doc = parser.parse()

        content = doc.chapters[0].content
        normalized = " ".join(content.split())
        assert "44I" not in content
        assert "I The problem I am going to raise" in content
        assert "so the comprehension of such facts" in normalized

    def test_parse_strips_single_trailing_reference_note(self, sample_pdf):
        with patch("modules.ingestion.pdf_parser.pymupdf4llm") as mock_llm, \
             patch("fitz.open") as mock_open, \
             patch("modules.ingestion.pdf_parser.validate_pdf", return_value=True):
            mock_llm.to_markdown.return_value = [
                {
                    "text": (
                        "Body text with inline note marker.2\n"
                        "More body text that should stay.\n"
                        "2 This trailing note should not be spoken.\n"
                        "It wraps onto one more extracted line."
                    )
                }
            ]

            mock_doc = MagicMock()
            mock_doc.__len__.return_value = 1
            mock_doc.metadata = {"title": "Test Book", "author": "Tester"}
            mock_doc.get_toc.return_value = [[1, "Chapter 1", 1]]
            mock_open.return_value.__enter__.return_value = mock_doc

            parser = PDFParser(sample_pdf)
            doc = parser.parse()

        content = doc.chapters[0].content
        assert "Body text with inline note marker.2" in content
        assert "More body text that should stay." in content
        assert "This trailing note should not be spoken" not in content
        assert "It wraps onto one more extracted line." not in content

    def test_parse_strips_jstor_cover_page_boilerplate(self, sample_pdf):
        with patch("modules.ingestion.pdf_parser.pymupdf4llm") as mock_llm, \
             patch("fitz.open") as mock_open, \
             patch("modules.ingestion.pdf_parser.validate_pdf", return_value=True):
            mock_llm.to_markdown.return_value = [
                {
                    "text": (
                        "Philosophical Review\n"
                        "What Is It Like to Be a Bat?\n"
                        "Author(s): Thomas Nagel\n"
                        "Stable URL: http://www.jstor.org/stable/2183914\n"
                        "Your use of the JSTOR archive indicates your acceptance of the Terms & Conditions of Use.\n"
                        "JSTOR is a not-for-profit service that helps scholars and researchers."
                    )
                },
                {"text": "Actual article body should start here."},
            ]

            mock_doc = MagicMock()
            mock_doc.__len__.return_value = 2
            mock_doc.metadata = {"title": "Test Book", "author": "Tester"}
            mock_doc.get_toc.return_value = [[1, "Chapter 1", 1]]
            mock_open.return_value.__enter__.return_value = mock_doc

            parser = PDFParser(sample_pdf)
            doc = parser.parse()

        content = doc.chapters[0].content
        assert "Actual article body should start here." in content
        assert "Stable URL" not in content
        assert "JSTOR is a not-for-profit" not in content

    def test_fitz_backend_extracts_full_page_text_when_llm_pages_are_empty(self, sample_pdf):
        class _Page:
            def __init__(self, text: str):
                self._text = text

            def get_text(self, mode: str = "text") -> str:
                assert mode == "text"
                return self._text

        class _Doc:
            metadata = {"title": "Nagel Fixture", "author": "Tester"}

            def __init__(self):
                self.pages = [
                    _Page("front matter only"),
                    _Page("full article body page two " * 20),
                    _Page("full article body page three " * 20),
                ]

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __len__(self):
                return len(self.pages)

            def __iter__(self):
                return iter(self.pages)

            def get_toc(self):
                return []

        with patch("modules.ingestion.pdf_parser.pymupdf4llm") as mock_llm, \
             patch("fitz.open", return_value=_Doc()), \
             patch("modules.ingestion.pdf_parser.validate_pdf", return_value=True):
            mock_llm.to_markdown.return_value = [
                {"text": "front matter only"},
                {"text": ""},
                {"text": ""},
            ]

            parser = PDFParser(sample_pdf, extraction_backend="fitz")
            doc = parser.parse()

        assert "full article body page two" in doc.raw_markdown
        assert "full article body page three" in doc.chapters[0].content
        assert len(doc.raw_markdown) > 500
        mock_llm.to_markdown.assert_not_called()

    def test_auto_backend_falls_back_to_fitz_for_short_sparse_markdown(self, sample_pdf):
        class _Page:
            def __init__(self, text: str):
                self._text = text

            def get_text(self, mode: str = "text") -> str:
                assert mode == "text"
                return self._text

        class _Doc:
            metadata = {"title": "Short Fixture", "author": "Tester"}

            def __init__(self):
                self.pages = [_Page("short but complete article body")]

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __len__(self):
                return len(self.pages)

            def __iter__(self):
                return iter(self.pages)

            def get_toc(self):
                return []

        with patch("modules.ingestion.pdf_parser.pymupdf4llm") as mock_llm, \
             patch("fitz.open", return_value=_Doc()), \
             patch("modules.ingestion.pdf_parser.validate_pdf", return_value=True):
            mock_llm.to_markdown.return_value = [{"text": ""}]

            parser = PDFParser(sample_pdf, extraction_backend="auto")
            doc = parser.parse()

        assert doc.chapters[0].content == "short but complete article body"

    def test_auto_backend_prefers_fitz_when_plain_text_coverage_is_complete(self, sample_pdf):
        class _Page:
            def __init__(self, text: str):
                self._text = text

            def get_text(self, mode: str = "text") -> str:
                assert mode == "text"
                return self._text

        class _Doc:
            metadata = {"title": "Complete Plain Text", "author": "Tester"}

            def __init__(self):
                self.pages = [
                    _Page("complete article page one " * 80),
                    _Page("complete article page two " * 80),
                    _Page("complete article page three " * 80),
                ]

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __len__(self):
                return len(self.pages)

            def __iter__(self):
                return iter(self.pages)

            def get_toc(self):
                return []

        with patch("modules.ingestion.pdf_parser.pymupdf4llm") as mock_llm, \
             patch("fitz.open", return_value=_Doc()), \
             patch("modules.ingestion.pdf_parser.validate_pdf", return_value=True):
            mock_llm.to_markdown.side_effect = AssertionError("legacy extraction should not run")

            parser = PDFParser(sample_pdf, extraction_backend="auto")
            doc = parser.parse()

        assert "complete article page one" in doc.raw_markdown
        assert "complete article page three" in doc.chapters[0].content
        mock_llm.to_markdown.assert_not_called()

    def test_parse_classifies_image_only_pdf_as_ocr_required(self, tmp_path):
        source_path = tmp_path / "image-only.pdf"
        _create_image_only_pdf(source_path)

        parser = PDFParser(source_path)

        with pytest.raises(OCRRequiredPDFError, match="OCR required"):
            parser.parse()


def test_ingest_source_passes_pipeline_pdf_extraction_backend(tmp_path):
    source_path = tmp_path / "book.pdf"
    source_path.write_bytes(b"%PDF-1.4\n" + b"x" * 128 + b"\n%%EOF")
    pipeline = SimpleNamespace(
        config=SimpleNamespace(pdf_extraction_backend="fitz"),
        log_verbose=lambda *_args, **_kwargs: None,
        notify_progress=lambda *_args, **_kwargs: None,
    )
    document = SimpleNamespace(
        chapters=[SimpleNamespace(title="Book", content="Readable body text.")],
        title="Book",
        skipped_non_prose_sections=[],
    )

    with patch("modules.ingestion.pdf_parser.PDFParser") as mock_parser:
        mock_parser.return_value.parse.return_value = document

        assert ingest_source(pipeline, source_path) is document

    mock_parser.assert_called_once_with(source_path, extraction_backend="fitz")


def test_ingest_source_raises_when_document_has_no_readable_content(tmp_path):
    from modules.errors import CorruptedFileError

    source_path = tmp_path / "empty.txt"
    source_path.write_text("placeholder", encoding="utf-8")
    pipeline = SimpleNamespace(
        config=SimpleNamespace(source_type=None, pre_chunk_document_cleanup=False),
        log_verbose=lambda *_args, **_kwargs: None,
        notify_progress=lambda *_args, **_kwargs: None,
    )
    # Parser returns only whitespace-content chapters: this must surface as a
    # clear, user-facing error instead of an opaque downstream packaging crash.
    document = SimpleNamespace(
        chapters=[SimpleNamespace(title="x", content="   \n\t ")],
        title="Empty",
    )

    with patch("modules.ingestion.text_parser.TextParser") as mock_parser:
        mock_parser.return_value.parse.return_value = document

        with pytest.raises(CorruptedFileError):
            ingest_source(pipeline, source_path)


def test_pdf_parser_raises_when_all_chapters_are_whitespace(sample_pdf):
    from modules.errors import CorruptedFileError

    with patch("modules.ingestion.pdf_parser.validate_pdf", return_value=True):
        parser = PDFParser(sample_pdf, extraction_backend="fitz")

    text_layer = SimpleNamespace(
        requires_ocr=False, confidence=0.99, sampled_pages=2, text_chars=0
    )
    mock_doc = MagicMock()
    mock_doc.__len__.return_value = 2
    mock_doc.metadata = {"title": "Blank", "author": ""}
    mock_doc.get_toc.return_value = []

    with patch.object(
        PDFParser, "_extract_pages", return_value=[{"text": "   "}, {"text": "\n \n"}]
    ), patch(
        "modules.ingestion.pdf_parser.analyze_extracted_pdf_text_layer",
        return_value=text_layer,
    ), patch(
        "modules.ingestion.pdf_parser.normalize_pdf_artifacts",
        side_effect=lambda pages, *args, **kwargs: pages,
    ), patch("fitz.open") as mock_open:
        mock_open.return_value.__enter__.return_value = mock_doc

        with pytest.raises(CorruptedFileError):
            parser.parse()
