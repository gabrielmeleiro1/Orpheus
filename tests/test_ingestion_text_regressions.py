"""Text-fidelity regressions for ingestion heuristics and document cleanup."""

from __future__ import annotations

import tracemalloc
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup
from ebooklib import epub
import pytest

from modules.ingestion import cover_art
from modules.ingestion import pdf_artifact_normalizer
from modules.ingestion.content_filter import classify_chapter_content
from modules.ingestion.epub_parser import EPUBParser
from modules.ingestion.epub_sections import build_epub_sections, soup_to_epub_text
from modules.ingestion.models import Chapter, Document
from modules.ingestion.pdf_artifact_normalizer import (
    RepeatedPageBoundaryPolicy,
    _strip_trailing_reference_block,
    normalize_pdf_artifacts,
)
from modules.ingestion.pdf_parser import PDFParser
from modules.ingestion.text_encoding import decode_source_text
from modules.text.document_normalizer import _reflow_lines, normalize_extracted_document


def _toc_chapters(toc, total_pages):
    entries = PDFParser._select_toc_chapter_entries(toc)
    return [
        (chapter.title, chapter.start_page, chapter.end_page)
        for chapter in PDFParser._chapters_from_entries(entries, total_pages)
    ]


# PDF outline chapter selection


def test_titles_opening_with_numbers_do_not_drop_the_first_chapter():
    toc = [(1, "The Beginning", 1), (1, "10 Days at Sea", 120), (1, "20 Years Later", 240)]

    assert _toc_chapters(toc, 300) == [
        ("The Beginning", 1, 119),
        ("10 Days at Sea", 120, 239),
        ("20 Years Later", 240, 300),
    ]


def test_numbered_chapter_sequence_still_wins_over_a_book_title_entry():
    toc = [(1, "Coastal Ecology", 1), (1, "1 Introduction", 5), (1, "2 Methods", 7)]

    assert [title for title, _, _ in _toc_chapters(toc, 9)] == ["1 Introduction", "2 Methods"]


def test_outlined_back_matter_is_not_folded_into_the_last_chapter():
    toc = [(1, "1 One", 3), (1, "2 Two", 20), (1, "Notes", 40), (1, "Bibliography", 50), (1, "Index", 60)]

    assert _toc_chapters(toc, 70) == [
        ("1 One", 3, 19),
        ("2 Two", 20, 39),
        ("Notes", 40, 49),
        ("Bibliography", 50, 59),
        ("Index", 60, 70),
    ]


# Non-prose classification


def _dated_history_chapter() -> str:
    paragraphs = [
        "The archive holds references to many forgotten voyages, and the historian "
        "reads them slowly by lamplight while the city sleeps."
    ]
    for index in range(60):
        if index % 4 == 0:
            paragraphs.append(
                f"In {1700 + index} the fleet left the harbour again, and the sailors sang as the "
                "wind rose over the grey water and the gulls followed them out to sea."
            )
        else:
            paragraphs.append(
                "The sailors sang as the wind rose over the grey water and the gulls followed "
                "them out to sea, and nobody spoke of home."
            )
    return "\n\n".join(paragraphs)


def test_narrative_chapter_mentioning_references_and_dates_is_kept():
    assert classify_chapter_content("Chapter 3", _dated_history_chapter()).should_skip is False


def test_bibliography_under_a_heading_line_is_still_skipped():
    entries = [
        f"Author{index}, A. ({1950 + index}). A study of harbours. Oxford University Press."
        for index in range(6)
    ]
    content = "\n".join(["Select Bibliography", *entries])

    classification = classify_chapter_content("Back Matter", content)

    assert classification.should_skip is True
    assert classification.reason == "references"


# PDF hyphenation and footnotes


def test_protected_line_break_hyphens_survive_chapter_cleanup():
    pages = [
        {"text": "They pushed themselves-\ntoward the gate and the body-\nmind problem, the pre-\nand post-war years."},
        {"text": "A second page of ordinary prose."},
    ]
    page_text = normalize_pdf_artifacts(pages)[0]["text"]
    document = Document(title="T", chapters=[Chapter(number=1, title="One", content=page_text)])

    content = normalize_extracted_document(document, source_type="pdf").chapters[0].content

    assert "themselves-toward" in content
    assert "body-mind" in content
    assert "pre- and post-war" in content


@pytest.mark.parametrize("number", ["3:15", "1.15", "2,015"])
def test_number_parts_are_not_footnote_markers(number):
    page = (
        f"The figure on the board read {number} that afternoon while she waited.\n"
        "15 minutes later she was home and the kettle was on."
    )

    assert _strip_trailing_reference_block(page) == page


def test_footnote_block_without_a_blank_line_keeps_the_body_above_it():
    notes = "\n".join(
        f"{index} Darwin to Hooker, letter number {index}, written in 1859 from Down House."
        for index in range(1, 13)
    )
    page = (
        "A first paragraph mentions a source.1\n\n"
        "The body paragraph line one continues here\n"
        "and the second body line carries on the story\n" + notes
    )

    assert _strip_trailing_reference_block(page) == (
        "A first paragraph mentions a source.1\n\n"
        "The body paragraph line one continues here\n"
        "and the second body line carries on the story"
    )


def test_repeated_boundary_policy_splits_each_page_once(monkeypatch):
    calls = []
    real = pdf_artifact_normalizer._page_boundary_line_indexes

    def counting(lines):
        calls.append(1)
        return real(lines)

    monkeypatch.setattr(pdf_artifact_normalizer, "_page_boundary_line_indexes", counting)
    pages = [{"text": f"RUNNING HEADER\nBody of page {index}."} for index in range(4)]

    cleaned = RepeatedPageBoundaryPolicy().normalize_pages(pages)

    assert [page["text"] for page in cleaned] == [f"Body of page {index}." for index in range(4)]
    assert len(calls) == len(pages)


# PDF chapter cleanup


def test_dialogue_matching_a_running_header_is_kept():
    body = "Lorem ipsum, the harbour was calm and the boats rocked gently at anchor.\n" * 3
    chapters = [
        Chapter(number=index, title=f"Chapter {index}", content=f"EMMA\n\n{body}\n\nEmma!\n\n{body}\n\nEMMA\n{body}")
        for index in range(1, 5)
    ]

    normalized = normalize_extracted_document(Document(title="Emma", chapters=chapters), source_type="pdf")

    assert all("Emma!" in chapter.content for chapter in normalized.chapters)
    assert all("EMMA" not in chapter.content for chapter in normalized.chapters)


def test_wrapped_lines_opening_with_quotes_or_part_words_stay_in_the_sentence():
    text = 'He walked slowly into the\npart of town where she said\n"hello" to him quietly.'

    assert _reflow_lines(text) == 'He walked slowly into the part of town where she said "hello" to him quietly.'


def test_dialogue_and_headings_still_start_their_own_lines():
    text = 'Chapter 2\nThe rain had stopped.\n"Come in," she said.\nPart of him stayed.'

    assert _reflow_lines(text) == 'Chapter 2\nThe rain had stopped.\n"Come in," she said.\nPart of him stayed.'


def test_overlong_paragraph_split_keeps_sentences_in_one_paragraph():
    paragraph = " ".join(f"Sentence number {index} is here." for index in range(1, 11))
    document = Document(title="Long", chapters=[Chapter(number=1, title="Long", content=paragraph)])

    normalized = normalize_extracted_document(
        document, source_type="txt", skip_non_prose_sections=False, max_chapter_words=12,
    )

    assert len(normalized.chapters) > 1
    assert all("\n\n" not in chapter.content for chapter in normalized.chapters)
    assert " ".join(chapter.content for chapter in normalized.chapters) == paragraph


def test_disabled_non_prose_skipping_keeps_epub_notes_sections():
    document = Document(title="B", chapters=[Chapter(number=1, title="Notes", content="Real narrative text. " * 20)])

    normalized = normalize_extracted_document(document, source_type="epub", skip_non_prose_sections=False)

    assert [chapter.title for chapter in normalized.chapters] == ["Notes"]


# EPUB text extraction


def test_phrasing_tags_stay_inside_the_sentence():
    soup = BeautifulSoup(
        "<p>She read <cite>Pride and Prejudice</cite>, then <q>Emma</q> and <abbr>NASA</abbr> notes.</p>",
        "lxml",
    )

    assert soup_to_epub_text(soup) == "She read Pride and Prejudice, then Emma and NASA notes."


def test_sectioning_header_with_its_heading_and_epigraph_is_kept():
    soup = BeautifulSoup(
        "<header><p>Site banner</p></header><section><header><h1>Chapter 1</h1>"
        "<p>An epigraph line.</p></header><p>Body.</p></section>",
        "lxml",
    )

    assert soup_to_epub_text(soup) == "Chapter 1\n\nAn epigraph line.\n\nBody."


def test_toc_marker_on_the_wrapping_section_splits_its_nested_sections():
    book = epub.EpubBook()
    page = epub.EpubHtml(uid="page", title="Stories", file_name="page.xhtml")
    page.content = (
        '<section id="ch1"><h1>Arrival</h1><p>' + "The chapter opens here. " * 8 + "</p>"
        '<section id="s1"><h2>Harbour</h2><p>' + "Section A carries on. " * 8 + "</p></section>"
        '<section id="s2"><h2>Storm</h2><p>' + "Section B carries on. " * 8 + "</p></section>"
        "</section>"
    )
    book.add_item(page)
    book.spine = [("page", "yes")]
    # Outline titles differ from the printed headings, so only the fragments
    # can split the document.
    book.toc = [
        epub.Link("page.xhtml#ch1", "Chapter 1: Arrival", "ch1"),
        epub.Link("page.xhtml#s1", "Section A: Harbour", "s1"),
        epub.Link("page.xhtml#s2", "Section B: Storm", "s2"),
    ]

    sections = build_epub_sections(book)

    assert [section.title for section in sections] == [
        "Chapter 1: Arrival", "Section A: Harbour", "Section B: Storm",
    ]
    joined = "\n".join(section.content for section in sections)
    for sentence in ("The chapter opens here.", "Section A carries on.", "Section B carries on."):
        assert joined.count(sentence) == 8


def _write_epub(path: Path, *, extra_manifest: str = "", members: dict[str, bytes] | None = None) -> Path:
    container = (
        '<?xml version="1.0"?><container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    )
    chapter = (
        '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>c</title></head><body><h1>Chapter One</h1>"
        + "<p>The ferry crossed the grey water before the storm arrived.</p>" * 10
        + "</body></html>"
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Book</dc:title>'
        '<dc:identifier id="id">x</dc:identifier><dc:language>en</dc:language>'
        '<meta name="cover" content="cover"/></metadata>'
        '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
        f"{extra_manifest}</manifest>"
        '<spine><itemref idref="c1"/></spine></package>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/ch1.xhtml", chapter)
        for name, data in (members or {}).items():
            archive.writestr(name, data)
    return path


def test_epub_with_missing_stylesheet_and_remote_font_is_readable(tmp_path):
    path = _write_epub(
        tmp_path / "book.epub",
        extra_manifest=(
            '<item id="css" href="style.css" media-type="text/css"/>'
            '<item id="font" href="https://example.org/font.woff" media-type="font/woff"/>'
        ),
    )

    document = EPUBParser(path).parse()

    assert "ferry crossed" in document.chapters[0].content


def test_epub_parser_validates_once_and_reuses_parsed_headings(tmp_path, monkeypatch):
    import modules.ingestion.epub_parser as epub_parser_module

    path = _write_epub(tmp_path / "book.epub")
    calls = []
    real_validate = epub_parser_module.validate_epub
    monkeypatch.setattr(
        epub_parser_module, "validate_epub", lambda source: calls.append(source) or real_validate(source),
    )
    parser = EPUBParser(path)
    parser._extract_sections()

    def no_reparse(markup):
        raise AssertionError("section markup parsed again")

    monkeypatch.setattr(epub_parser_module, "parse_epub_markup", no_reparse)
    chapters = parser.extract_chapters()

    assert len(calls) == 1
    assert chapters[0].heading_text == "Chapter One"


# Cover art


def test_cover_meta_pointing_at_an_xhtml_page_falls_back_to_the_cover_image(tmp_path):
    path = _write_epub(
        tmp_path / "cover.epub",
        extra_manifest=(
            '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="cover-img" href="images/cover.jpg" media-type="image/jpeg"/>'
        ),
        members={"OEBPS/cover.xhtml": b"<html/>", "OEBPS/images/cover.jpg": b"\xff\xd8jpeg-bytes"},
    )

    cover = cover_art._epub_cover(path)

    assert cover == cover_art.CoverImage(b"\xff\xd8jpeg-bytes", "image/jpeg")


def test_cover_read_is_bounded_when_the_size_header_understates_the_member(tmp_path):
    path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("cover.jpg", b"\0" * (48 * 1024 * 1024))
    with zipfile.ZipFile(path) as archive:
        archive.getinfo("cover.jpg").file_size = 100  # a forged header
        tracemalloc.start()
        try:
            try:
                cover_art._read_member(archive, "cover.jpg")
            except zipfile.BadZipFile:
                pass
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    assert peak < 24 * 1024 * 1024


# Text decoding


def test_utf16_detection_samples_a_prefix_with_the_same_result():
    text = "A plain text book without a byte order mark. " * 5000

    assert decode_source_text(text.encode("utf-16-le")) == text
    assert decode_source_text(text.encode("utf-16-be")) == text


_CLEANUP_PROSE = (
    "It was a bright cold day in April, and the clocks were striking thirteen. "
    "She walked down the long road toward the village, thinking about the letter.\n\n"
) * 6
_CLEANUP_VERSE = "\n".join(
    ["The rose", "is red", "the sky", "is grey", "and I", "am here", "today", "alone", "again", "at dusk"]
)
_CLEANUP_BODIES = {
    "prose": _CLEANUP_PROSE,
    "verse": _CLEANUP_VERSE,
    "list": "\n".join(f"Item {index}" for index in range(1, 20)),
    "toc": "Contents\n" + "\n".join(f"Chapter {index} ........ {index * 10}" for index in range(1, 12)),
    "index": "Index\n" + "\n".join(f"{letter}apple, {n}, {n + 3}" for n, letter in enumerate("ABCDEFGHIJ", 1)),
    "copyright": (
        "Copyright © 2020 Someone\nAll rights reserved.\nISBN 978-1-23456-789-0\n"
        "Published by Press\nPrinted in USA"
    ),
    "references": "References\n" + "\n".join(
        f"Smith, J. ({1990 + n}). Title {n}. Journal, {n}(2), 10-20." for n in range(8)
    ),
    "padded": "  " + _CLEANUP_PROSE + "  \n\n\n",
    "blank": "   ",
}
_CLEANUP_BOOKS = [
    [("Chapter 1", "prose"), ("Chapter 2", "prose")],
    [("Contents", "toc"), ("Chapter 1", "prose"), ("A Poem", "verse"), ("Index", "index")],
    [("Poem 1", "verse"), ("Poem 2", "verse"), ("Poem 3", "verse"), ("Chapter 1", "prose")],
    [("Copyright", "copyright"), ("Index", "index")],
    [("A Poem", "verse")],
    [("Copyright", "copyright"), ("Appendix", "list"), ("Chapter 1", "padded"), ("Notes", "blank")],
    [("  Notes  ", "references"), ("About the Author", "list"), ("Chapter 1", "prose")],
    [("Table of Contents", "toc"), ("Poem", "verse"), ("Bibliography", "references")],
]


def _cleanup_document(book):
    return Document(
        title="Book",
        author="Author",
        chapters=[
            Chapter(number=number, title=title, content=_CLEANUP_BODIES[body])
            for number, (title, body) in enumerate(book, start=1)
        ],
    )


class _UncachedChapterClassifier:
    """Reference behaviour: every question recomputed from scratch."""

    def classify(self, title, content, *, enable_low_prose_density=True):
        return classify_chapter_content(
            title, content, enable_low_prose_density=enable_low_prose_density
        )

    def is_verse_like(self, content):
        from modules.ingestion.content_filter import is_verse_like

        return is_verse_like(content)


@pytest.mark.parametrize("source_type", ["pdf", "epub", "txt", "md"])
@pytest.mark.parametrize("skip_non_prose_sections", [True, False])
@pytest.mark.parametrize("book", _CLEANUP_BOOKS)
def test_memoised_chapter_classification_matches_recomputing_every_time(
    monkeypatch, source_type, skip_non_prose_sections, book
):
    import modules.text.document_normalizer as document_normalizer

    kwargs = {"source_type": source_type, "skip_non_prose_sections": skip_non_prose_sections}
    memoised = normalize_extracted_document(_cleanup_document(book), **kwargs)
    monkeypatch.setattr(document_normalizer, "ChapterClassifier", _UncachedChapterClassifier)
    recomputed = normalize_extracted_document(_cleanup_document(book), **kwargs)

    assert repr(memoised) == repr(recomputed)


def test_chapter_classifier_answers_like_classify_chapter_content():
    from modules.ingestion.content_filter import ChapterClassifier, is_verse_like

    classifier = ChapterClassifier()
    titles = ["Chapter 1", "Contents", "Index", "Copyright", "References", "List of Maps", ""]
    for _ in range(2):
        for title in titles:
            for content in _CLEANUP_BODIES.values():
                for flag in (True, False):
                    assert classifier.classify(
                        title, content, enable_low_prose_density=flag
                    ) == classify_chapter_content(title, content, enable_low_prose_density=flag)
                assert classifier.is_verse_like(content) == is_verse_like(content)


def test_document_cleanup_classifies_each_chapter_once(monkeypatch):
    from modules.ingestion import content_filter

    structural_calls: list[tuple[str, str]] = []
    verse_calls: list[str] = []
    original_structural = content_filter._structural_classification
    original_verse = content_filter.is_verse_like

    def counting_structural(title, content, lines):
        structural_calls.append((title, content))
        return original_structural(title, content, lines)

    def counting_verse(content):
        verse_calls.append(content)
        return original_verse(content)

    monkeypatch.setattr(content_filter, "_structural_classification", counting_structural)
    monkeypatch.setattr(content_filter, "is_verse_like", counting_verse)

    # Front matter before the first prose chapter is classified by the
    # prose-body check, the per-chapter filter and, when normalization leaves
    # it unchanged, the post-normalization filter; verse detection reads every
    # chapter again.
    book = [("Contents", "toc"), ("Chapter 1", "prose"), ("A Poem", "verse"), ("Chapter 2", "prose")]
    result = normalize_extracted_document(_cleanup_document(book), source_type="epub")

    assert [chapter.title for chapter in result.chapters] == ["Chapter 1", "A Poem", "Chapter 2"]
    assert structural_calls, "classification never ran"
    assert len(structural_calls) == len(set(structural_calls))
    assert len(verse_calls) == len(set(verse_calls))


# PDF outline coverage


def _assert_outline_pages_covered(toc, total_pages):
    entries = PDFParser._select_toc_chapter_entries(toc) or [("Document", 1)]
    covered = set()
    for chapter in PDFParser._chapters_from_entries(entries, total_pages):
        covered.update(range(chapter.start_page, chapter.end_page + 1))
    first = min(page for _, _, page in toc)
    assert set(range(first, total_pages + 1)) <= covered, (toc, sorted(covered))


def test_numbered_sections_of_named_chapters_do_not_drop_the_chapter_openings():
    toc = [(1, "The Sea", 2), (1, "The Land", 10), (2, "1 Harbour", 30), (2, "2 Storm", 40)]

    assert _toc_chapters(toc, 60) == [("The Sea", 2, 9), ("The Land", 10, 60)]
    _assert_outline_pages_covered(toc, 60)


def test_named_chapter_after_numbered_sections_keeps_its_own_chapter():
    toc = [(1, "The Sea", 2), (1, "The Land", 10), (2, "1 Harbour", 30), (2, "2 Storm", 40), (1, "The Sky", 50)]

    assert [title for title, _, _ in _toc_chapters(toc, 60)] == ["The Sea", "The Land", "The Sky"]
    _assert_outline_pages_covered(toc, 60)


def test_generated_outlines_leave_no_page_gap_after_the_first_outlined_page():
    import random

    names = ["The Sea", "The Land", "Interlude", "Epilogue", "Morning", "10 Days", "20 Years"]
    for seed in range(400):
        rng = random.Random(seed)
        toc, page, number = [], rng.randint(2, 4), 1
        for index in range(rng.randint(1, 7)):
            level = 1 if index == 0 else rng.choice([1, 2])
            if rng.random() < 0.5:
                title, number = f"{number} Part", number + 1
            else:
                title = rng.choice(names)
            toc.append((level, title, page))
            page += rng.randint(1, 5)
        _assert_outline_pages_covered(toc, page + 3)


# References headings


_CITATION_ENTRIES = [
    f"Author{index}, A. ({1950 + index}). A study of harbours. Oxford University Press."
    for index in range(8)
]


@pytest.mark.parametrize("heading", [
    "Notes and References", "References and Further Reading", "Works cited", "REFERENCES CITED",
    "Sources and Bibliography", "Bibliography and Notes", "Sources & References", "## References",
    "194   Bibliography",
])
def test_bibliography_under_compound_heading_is_skipped(heading):
    classification = classify_chapter_content("Back Matter", "\n".join([heading, *_CITATION_ENTRIES]))

    assert (classification.should_skip, classification.reason) == (True, "references")


def test_chicago_style_bibliography_with_trailing_years_is_skipped():
    entries = [
        f"Writer{index}, Given. A History of the Coast. New York: Harbour House, {1950 + index}."
        for index in range(8)
    ]

    assert classify_chapter_content("Bibliography", "\n".join(entries)).reason == "references"


def test_dated_narrative_titled_with_references_is_kept():
    paragraphs = ["The archive holds references to forgotten voyages."]
    for index in range(40):
        paragraphs.append(
            f"In {1700 + index} the fleet left harbour again and the sailors sang."
            if index % 3 == 0
            else "The sailors sang as the wind rose over the grey water."
        )

    assert classify_chapter_content("Cross References", "\n\n".join(paragraphs)).should_skip is False


# Inline note markers


@pytest.mark.parametrize("page", [
    "She took vitamin B12 every morning before work.\n12 hours later she woke and the fever had broken.",
    "He drove the old F150 out past the quarry.\n150 yards on, the road ended at the river.",
])
def test_digits_glued_to_a_code_are_not_note_markers(page):
    assert _strip_trailing_reference_block(page) == page


@pytest.mark.parametrize("body", [
    "He wrote to his friend about the finches.3",
    "He called it “a mystery of mysteries”3 in the letter.",
    "The theory of descent3 was new to them.",
])
def test_spaced_footnote_after_a_real_marker_is_still_removed(body):
    assert _strip_trailing_reference_block(f"{body}\n3 Darwin to Hooker, 1859.") == body


# Cover hrefs


@pytest.mark.parametrize("href, member", [
    ("/OEBPS/images/c.jpg", "OEBPS/images/c.jpg"),
    ("images/c.jpg?v=2#x", "OEBPS/images/c.jpg"),
])
def test_cover_href_resolves_root_relative_and_decorated_paths(tmp_path, href, member):
    path = _write_epub(
        tmp_path / "cover.epub",
        extra_manifest=f'<item id="cover" href="{href.replace("&", "&amp;")}" media-type="image/jpeg"/>',
        members={member: b"\xff\xd8jpeg-bytes"},
    )

    assert cover_art._epub_cover(path) == cover_art.CoverImage(b"\xff\xd8jpeg-bytes", "image/jpeg")


def test_extensionless_cover_uses_the_declared_media_type(tmp_path):
    path = _write_epub(
        tmp_path / "cover.epub",
        extra_manifest='<item id="cover" href="images/cover" media-type="image/jpeg"/>',
        members={"OEBPS/images/cover": b"\xff\xd8jpeg-bytes"},
    )

    assert cover_art._epub_cover(path) == cover_art.CoverImage(b"\xff\xd8jpeg-bytes", "image/jpeg")


# Note markers after a year


@pytest.mark.parametrize("body, notes", [
    (
        "The armistice was signed in November 1918.7 The troops went home slowly, and the town waited.\n",
        "7 See Keegan, The First World War, p. 412.",
    ),
    (
        "The fleet sailed in 1588.3 It was scattered by storms in 1589.4 Few ships returned home.",
        "3 Mattingly, The Armada, p. 12.\n4 Ibid., p. 40.",
    ),
])
def test_note_marker_after_a_year_ending_a_sentence_is_still_stripped(body, notes):
    assert _strip_trailing_reference_block(f"{body}\n{notes}") == body.rstrip()


@pytest.mark.parametrize("page", [
    "The figure on the board read 3:15 that afternoon.\n15 minutes later she was home.",
    "The ratio settled at 1.2 overall.\n2 weeks later it had not moved.",
    "The crowd numbered 1,500 that night.\n500 of them stayed until dawn.",
    "They shipped the 2019.1 release that spring.\n1 bug report arrived the next day.",
    "The coat cost 1999.99 dollars in the end.\n99 people had asked about it already.",
])
def test_numbers_after_a_digit_separator_stay_prose(page):
    assert _strip_trailing_reference_block(page) == page
