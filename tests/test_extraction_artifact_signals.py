from modules.ingestion.extraction_artifact_signals import (
    has_joined_first_person_leakage,
    has_pdf_hyphenation_leakage,
    scan_cleaner_handoff_artifacts,
)
from modules.ingestion.models import Chapter, Document
from modules.text.document_normalizer import normalize_extracted_document


def test_scan_cleaner_handoff_artifacts_flags_pdf_residue():
    issues = scan_cleaner_handoff_artifacts(
        "This content downloaded from an old mirror should be flagged."
    )
    assert any(issue.issue_type == "pdf_page_footnote_leakage" for issue in issues)


def test_has_pdf_hyphenation_leakage_ignores_suspended_compounds():
    assert has_pdf_hyphenation_leakage("The self-governed town") is False
    assert has_pdf_hyphenation_leakage("The conver- sation continued") is True


def test_has_joined_first_person_leakage_detects_joined_words():
    assert has_joined_first_person_leakage("Iwanted to leave early.") is True
    assert has_joined_first_person_leakage("I wanted to leave early.") is False


def test_normalize_extracted_document_records_skipped_non_prose_sections():
    document = Document(
        title="Example",
        chapters=[
            Chapter(
                number=1,
                title="Contents",
                content="Chapter 1 ........ 1\nChapter 2 ........ 9\nChapter 3 ........ 22",
            ),
            Chapter(
                number=2,
                title="Chapter 1",
                content="The room was quiet. " * 20,
            ),
        ],
    )
    normalized = normalize_extracted_document(document, source_type="md")
    assert len(normalized.chapters) == 1
    assert normalized.chapters[0].title == "Chapter 1"
    assert normalized.skipped_non_prose_sections[0]["reason"] == "table_of_contents"
