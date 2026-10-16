"""Regression coverage for pdf parser."""

from modules.pipeline.chapter_narration import chapter_body
from modules.text.document_normalizer import normalize_extracted_document


def test_13_pdf_heading_provenance_survives_reflow(tmp_path, monkeypatch):
    from modules.ingestion.pdf_parser import PDFParser

    path = tmp_path / "arrival.pdf"
    path.touch()
    parser = PDFParser(path, validate=False)
    parser._metadata = {"title": "Voyage"}
    parser._total_pages = 1
    parser._toc = [[1, "Arrival", 1]]
    body = "The ship approached the shore at dawn. " * 20
    monkeypatch.setattr(parser, "_extract_pages", lambda: [{"text": "Arrival\n" + body}])
    document = normalize_extracted_document(parser.parse(), source_type="pdf", skip_non_prose_sections=False)
    assert chapter_body(document.chapters[0]) == body.strip()
