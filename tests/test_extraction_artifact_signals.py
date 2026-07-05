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


def test_scan_cleaner_handoff_artifacts_scopes_pdf_checks_to_pdf_sources():
    text = "Hel- lo and inde-pendence. This content downloaded from an old mirror."

    assert {
        issue.issue_type
        for issue in scan_cleaner_handoff_artifacts(text, source_type="pdf")
    } >= {
        "pdf_hyphenation_leakage",
        "pdf_internal_hyphen_fragment",
        "pdf_page_footnote_leakage",
    }
    assert not any(
        issue.issue_type.startswith("pdf_")
        for source_type in ("epub", "txt", "md")
        for issue in scan_cleaner_handoff_artifacts(text, source_type=source_type)
    )
    assert any(
        issue.issue_type.startswith("pdf_")
        for issue in scan_cleaner_handoff_artifacts(text, source_type=None)
    )


def test_scan_cleaner_handoff_artifacts_skips_llm_only_checks_for_rule_text():
    issues = scan_cleaner_handoff_artifacts(
        "In this commentary, the text has been transmitted carefully. ---",
        llm_output=False,
    )

    assert {issue.issue_type for issue in issues}.isdisjoint(
        {"model_commentary_leakage", "prompt_delimiter_leakage"}
    )


def test_has_pdf_hyphenation_leakage_ignores_suspended_compounds():
    assert has_pdf_hyphenation_leakage("The self-governed town") is False
    assert has_pdf_hyphenation_leakage("The conver- sation continued") is True


def test_scan_cleaner_handoff_artifacts_flags_internal_hyphen_fragments():
    issues = scan_cleaner_handoff_artifacts(
        "The text still says inde-pendence, expe-rience, experi-ence, and gover-nment."
    )

    assert any(issue.issue_type == "pdf_internal_hyphen_fragment" for issue in issues)


def test_internal_hyphen_fragment_detector_allows_common_compounds():
    text = (
        "The self-reliant card-sharper discussed well-being, "
        "post-war order, proto-imperial symbols, co-operation, "
        "co-dependence, de-industrialization, anti-discrimination, "
        "micro-organisms, inter-action, and mind-bending work."
    )

    assert not any(
        issue.issue_type == "pdf_internal_hyphen_fragment"
        for issue in scan_cleaner_handoff_artifacts(text)
    )


def test_internal_hyphen_fragment_detector_allows_capitalized_compounds():
    text = (
        "There is no difference between Time and any of the three "
        "dimensions of Space except that the Time-Dimension moves, "
        "as the Space-Dimension does not; the Four-Dimension geometry "
        "held the Time-Traveller's attention despite his ill-treatment "
        "and long-expected far-sighted full-preparation."
    )

    assert not any(
        issue.issue_type == "pdf_internal_hyphen_fragment"
        for issue in scan_cleaner_handoff_artifacts(text)
    )
    # Sentence-start hyphenation artifacts are still caught.
    assert any(
        issue.issue_type == "pdf_internal_hyphen_fragment"
        for issue in scan_cleaner_handoff_artifacts("Gover-nment matters here.")
    )


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


def test_normalize_extracted_document_splits_overlong_chapters_at_paragraphs():
    cap = 10
    document = Document(
        title="Example",
        chapters=[
            Chapter(
                number=1,
                title="Long",
                content="\n\n".join(["one two three four five"] * 3),
            ),
            Chapter(number=2, title="Next", content="final paragraph"),
        ],
    )

    normalized = normalize_extracted_document(
        document,
        source_type="txt",
        max_chapter_words=cap,
        skip_non_prose_sections=False,
    )

    assert [(chapter.number, chapter.title) for chapter in normalized.chapters] == [
        (1, "Long (part 1)"),
        (2, "Long (part 2)"),
        (3, "Next"),
    ]
    assert all(len(chapter.content.split()) <= cap for chapter in normalized.chapters)


def test_normalize_extracted_document_leaves_under_cap_chapters_untouched():
    document = Document(
        title="Example",
        chapters=[Chapter(number=1, title="Short", content="one two three")],
    )

    normalized = normalize_extracted_document(
        document,
        source_type="txt",
        max_chapter_words=10,
        skip_non_prose_sections=False,
    )

    assert normalized.chapters == document.chapters
