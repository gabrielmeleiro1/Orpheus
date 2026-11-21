from modules.ingestion.extraction_artifact_signals import (
    has_joined_first_person_leakage,
    has_pdf_hyphenation_leakage,
    has_pdf_page_footnote_leakage,
)
from modules.ingestion.models import Chapter, Document
from modules.text.document_normalizer import normalize_extracted_document


def test_has_pdf_page_footnote_leakage_flags_pdf_residue():
    assert has_pdf_page_footnote_leakage(
        "This content downloaded from an old mirror should be flagged."
    )


def test_has_pdf_hyphenation_leakage_ignores_suspended_compounds():
    assert has_pdf_hyphenation_leakage("The self-governed town") is False
    assert has_pdf_hyphenation_leakage("The conver- sation continued") is True


def test_has_pdf_hyphenation_leakage_flags_internal_hyphen_fragments():
    assert has_pdf_hyphenation_leakage(
        "The text still says inde-pendence, expe-rience, experi-ence, and gover-nment."
    )


def test_internal_hyphen_fragment_detector_allows_common_compounds():
    text = (
        "The self-reliant card-sharper discussed well-being, "
        "post-war order, proto-imperial symbols, co-operation, "
        "co-dependence, de-industrialization, anti-discrimination, "
        "micro-organisms, inter-action, and mind-bending work."
    )

    assert has_pdf_hyphenation_leakage(text) is False


def test_internal_hyphen_fragment_detector_allows_capitalized_compounds():
    text = (
        "There is no difference between Time and any of the three "
        "dimensions of Space except that the Time-Dimension moves, "
        "as the Space-Dimension does not; the Four-Dimension geometry "
        "held the Time-Traveller's attention despite his ill-treatment "
        "and long-expected far-sighted full-preparation."
    )

    assert has_pdf_hyphenation_leakage(text) is False
    # Sentence-start hyphenation artifacts are still caught.
    assert has_pdf_hyphenation_leakage("Gover-nment matters here.") is True


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


def test_footnote_leak_detector_ignores_part_headings_before_a_problem_title():
    assert has_pdf_page_footnote_leakage("Part I The Problem of Knowledge") is False
    assert has_pdf_page_footnote_leakage("Book I The problem of induction is posed.") is False
    assert has_pdf_page_footnote_leakage("interrupted from I The problem I am going to raise.")
