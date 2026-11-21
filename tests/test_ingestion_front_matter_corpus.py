"""Regression corpus for what ingestion narrates and what it leaves out.

Every scenario raised by the ingestion reviews is a small generated book.
Each test checks both directions: the chapter prose is narrated, and the
front matter, navigation and endnotes around it are not.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from modules.ingestion.epub_parser import EPUBParser
from modules.ingestion.models import Chapter, Document
from modules.ingestion.pdf_artifact_normalizer import normalize_pdf_artifacts
from modules.ingestion.pdf_parser import PDFParser
from modules.ingestion.preflight import preflight_source
from modules.ingestion.text_parser import TextParser
from modules.text.document_normalizer import normalize_extracted_document

ebooklib_epub = pytest.importorskip("ebooklib.epub")
fitz = pytest.importorskip("fitz")


def _check(text: str, present: list[str], absent: list[str]) -> None:
    for expected in present:
        assert expected in text, f"missing {expected!r}"
    for unexpected in absent:
        assert unexpected not in text, f"narrated {unexpected!r}"


# EPUB ----------------------------------------------------------------------

_KEEPER = "The keeper climbed the stairs at dusk."
_STORMS = "Storms rolled in from the west."
_MORNING = "Morning came quietly over the rocks."


def _chapter(n: int, sentence: str, *, tag: str = "h2", anchor: bool = True) -> str:
    anchor_attr = f' id="ch{n}"' if anchor else ""
    return f"<{tag}{anchor_attr}>Chapter {n}</{tag}><p>" + (sentence + " ") * 10 + "</p>"


def _three_chapters(**kwargs) -> str:
    return "".join(
        _chapter(n, sentence, **kwargs) for n, sentence in enumerate((_KEEPER, _STORMS, _MORNING), start=1)
    )


_FRAGMENT_TOC = [("page.xhtml#ch1", "Chapter 1"), ("page.xhtml#ch2", "Chapter 2"), ("page.xhtml#ch3", "Chapter 3")]
_FRONT_MATTER = (
    "<h1>The Lighthouse Keeper</h1><p>A Novel</p><p>by Jane Doe</p>"
    "<p>Copyright © 2021 by Jane Doe. All rights reserved. No part of this publication may be "
    "reproduced without the prior written permission of the publisher. ISBN 978-0-00-000000-0.</p>"
    "<h2>Contents</h2><p>Chapter 1</p><p>Chapter 2</p><p>Chapter 3</p>"
)
_FRONT_MATTER_WORDS = ["Lighthouse Keeper", "Jane Doe", "Copyright", "ISBN", "Contents"]


def _parse_epub(tmp_path: Path, pages: list[tuple[str, str]], toc: list[tuple[str, str]]) -> list[Chapter]:
    """Parse and normalize an EPUB built from ``(file stem, markup)`` pages."""
    book = ebooklib_epub.EpubBook()
    book.set_identifier("corpus")
    book.set_title("Corpus")
    book.set_language("en")
    items = []
    for stem, markup in pages:
        item = ebooklib_epub.EpubHtml(uid=stem, title=stem, file_name=f"{stem}.xhtml")
        item.content = markup
        book.add_item(item)
        items.append(item)
    book.spine = items
    book.toc = [ebooklib_epub.Link(href, title, f"toc{index}") for index, (href, title) in enumerate(toc)]
    book.add_item(ebooklib_epub.EpubNcx())
    book.add_item(ebooklib_epub.EpubNav())
    path = tmp_path / "book.epub"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ebooklib_epub.write_epub(str(path), book)
        document = EPUBParser(path).parse()
    return normalize_extracted_document(document, source_type=".epub", skip_non_prose_sections=True).chapters


def _joined(chapters: list[Chapter]) -> str:
    return "\n".join(chapter.content for chapter in chapters)


def _single_file_book(tmp_path: Path, markup: str, toc=_FRAGMENT_TOC) -> list[Chapter]:
    return _parse_epub(tmp_path, [("page", markup)], toc)


@pytest.mark.parametrize(
    ("opening", "front_matter"),
    [
        pytest.param(_FRONT_MATTER, _FRONT_MATTER_WORDS, id="R2-E1-title-copyright-contents"),
        pytest.param(
            "<p>The Lighthouse Keeper</p><p>A Novel</p><p>by Jane Doe</p>"
            "<p>For my mother, who kept the light burning through every storm.</p>",
            ["Lighthouse Keeper", "Jane Doe", "For my mother"],
            id="R3-X1-title-and-dedication",
        ),
        pytest.param(
            "<p>“A haunting, luminous debut that will stay with you long after the last page.”</p>"
            "<p>— The Sunday Times</p>"
            "<p>“I could not put it down, and I did not want it to end.”</p>"
            "<p>— Ann Baker, author of Tides</p>",
            ["luminous debut", "Sunday Times", "could not put it down"],
            id="R3-X2-praise-blurbs",
        ),
        pytest.param(
            "<p>The Project Gutenberg eBook of The Lighthouse Keeper</p>"
            "<p>This ebook is for the use of anyone anywhere in the United States and most other parts "
            "of the world at no cost and with almost no restrictions whatsoever. You may copy it, give it "
            "away or re-use it under the terms of the Project Gutenberg License included with this ebook.</p>"
            "<p>Title: The Lighthouse Keeper</p><p>Release date: May 1, 2020 [eBook #12345]</p>"
            "<p>*** START OF THE PROJECT GUTENBERG EBOOK THE LIGHTHOUSE KEEPER ***</p>",
            ["Gutenberg", "use of anyone", "Release date"],
            id="R3-X3-gutenberg-header",
        ),
    ],
)
def test_epub_untitled_opening_before_the_first_toc_fragment_is_not_narrated(tmp_path, opening, front_matter):
    chapters = _single_file_book(tmp_path, opening + _three_chapters())

    assert [chapter.title for chapter in chapters] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert chapters[0].content.startswith("Chapter 1")
    _check(_joined(chapters), [_KEEPER, _STORMS, _MORNING], front_matter)


def test_epub_front_matter_before_title_matched_headings_is_not_narrated(tmp_path):
    # R2-E2: the TOC names the chapters but has no fragments.
    toc = [("page.xhtml", "Chapter 1"), ("page.xhtml", "Chapter 2"), ("page.xhtml", "Chapter 3")]
    chapters = _single_file_book(tmp_path, _FRONT_MATTER + _three_chapters(anchor=False), toc)

    assert [chapter.title for chapter in chapters] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    _check(_joined(chapters), [_KEEPER, _STORMS, _MORNING], _FRONT_MATTER_WORDS)


def test_epub_front_matter_before_the_first_heading_without_a_toc_is_not_narrated(tmp_path):
    # R2-E3b: no TOC, so the split falls back to headings; the front matter is <p>-only.
    front_matter = _FRONT_MATTER.replace("h1", "p").replace("h2", "p")
    chapters = _single_file_book(tmp_path, front_matter + _three_chapters(tag="h1", anchor=False), [])

    _check(_joined(chapters), [_KEEPER, _STORMS, _MORNING], _FRONT_MATTER_WORDS)


def test_epub_untitled_preamble_before_the_first_heading_without_a_toc_is_not_narrated(tmp_path):
    # R1 INGEST-1 raised the preamble as lost text, but nothing names it, and
    # this shape is how title pages and blurbs arrive; main drops it too.
    preamble = "<p>" + "A preamble precedes the stories. " * 8 + "</p>"
    chapters = _single_file_book(tmp_path, preamble + _three_chapters(tag="h1", anchor=False), [])

    _check(_joined(chapters), [_KEEPER, _STORMS, _MORNING], ["A preamble precedes"])


def test_epub_cover_entry_naming_the_front_matter_drops_it(tmp_path):
    # R2-E6: a fragment-less "Cover" entry names the opening, which is then filtered.
    toc = [("page.xhtml", "Cover"), *_FRAGMENT_TOC]
    chapters = _single_file_book(tmp_path, _FRONT_MATTER + _three_chapters(), toc)

    assert [chapter.title for chapter in chapters] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    _check(_joined(chapters), [_KEEPER, _STORMS, _MORNING], _FRONT_MATTER_WORDS)


def test_epub_navigation_after_the_chapter_wrapper_is_not_narrated(tmp_path):
    # R2-E5
    markup = (
        '<div class="chapter">' + _three_chapters(tag="h1", anchor=False) + "</div>"
        '<div class="nav"><p>Previous | Next | Back to contents</p></div>'
    )
    chapters = _single_file_book(tmp_path, markup, [])

    _check(_joined(chapters), [_KEEPER, _STORMS, _MORNING], ["Previous", "Back to contents"])


def test_epub_notes_after_the_chapter_wrapper_are_not_narrated(tmp_path):
    markup = (
        '<div class="chapter">' + _three_chapters() + "</div>"
        '<div class="footnotes"><p>1. Smith, The Northern Lights (London: Harbour Press, 1998), 112, '
        "describes the keeper's log at length.</p></div>"
    )
    chapters = _single_file_book(tmp_path, markup)

    _check(_joined(chapters), [_KEEPER, _STORMS, _MORNING], ["Harbour Press"])


def test_epub_mixed_depth_fragment_markers_keep_every_chapter(tmp_path):
    # R2-E9
    markup = (
        '<section><h1 id="ch1">Chapter 1</h1><p>' + (_KEEPER + " ") * 10 + "</p></section>"
        '<h1 id="ch2">Chapter 2</h1><p>' + (_STORMS + " ") * 10 + "</p>"
        '<div><div><h1 id="ch3">Chapter 3</h1></div><p>' + (_MORNING + " ") * 10 + "</p></div>"
    )
    chapters = _single_file_book(tmp_path, markup)

    assert [chapter.title for chapter in chapters] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    for chapter, sentence in zip(chapters, (_KEEPER, _STORMS, _MORNING)):
        assert chapter.content.count(sentence) == 10


@pytest.mark.parametrize("wrap_sections", [False, True])
def test_epub_nested_toc_parent_opening_is_narrated(tmp_path, wrap_sections):
    # R1 INGEST-1: the parent entry names the text before its fragments.
    sections = (
        '<h2 id="s1">Section A</h2><p>' + "Section A carries on. " * 8 + "</p>"
        '<h2 id="s2">Section B</h2><p>' + "Section B wraps up. " * 8 + "</p>"
    )
    if wrap_sections:
        sections = f"<div>{sections}</div>"
    pages = [
        ("ch1", "<h1>Chapter 1</h1><p>" + "The chapter opening sets the scene. " * 8 + "</p>" + sections),
        ("ch2", "<h1>Chapter 2</h1><p>" + (_STORMS + " ") * 8 + "</p>"),
    ]
    toc = [
        ("ch1.xhtml", "Chapter 1"), ("ch1.xhtml#s1", "Section A"), ("ch1.xhtml#s2", "Section B"),
        ("ch2.xhtml", "Chapter 2"),
    ]

    chapters = _parse_epub(tmp_path, pages, toc)

    assert [chapter.title for chapter in chapters] == ["Chapter 1", "Section A", "Section B", "Chapter 2"]
    joined = _joined(chapters)
    for sentence in ("The chapter opening sets the scene.", "Section A carries on.", "Section B wraps up.", _STORMS):
        assert joined.count(sentence) == 8, sentence


def test_epub_title_split_keeps_the_opening_named_by_an_unmatched_entry(tmp_path):
    # R1 INGEST-1 (title split) and R2-E8.
    markup = (
        "<h1>Chapter One: The Beginning</h1><p>" + "It began with a letter. " * 10 + "</p>"
        "<h2>Two</h2><p>" + "Second part text. " * 10 + "</p>"
        "<h2>Three</h2><p>" + "Third part text. " * 10 + "</p>"
    )
    toc = [("page.xhtml", "1. The Beginning"), ("page.xhtml", "Two"), ("page.xhtml", "Three")]

    chapters = _single_file_book(tmp_path, markup, toc)

    assert [chapter.title for chapter in chapters] == ["1. The Beginning", "Two", "Three"]
    joined = _joined(chapters)
    for sentence in ("It began with a letter.", "Second part text.", "Third part text."):
        assert joined.count(sentence) == 10, sentence


def _begin_reading_book(tmp_path: Path) -> list[Chapter]:
    pages = [
        ("copyright", "<h1>Copyright</h1><p>" + "All rights reserved. " * 8 + "</p>"),
        ("ch1", "<h1>Chapter 1</h1><p>" + "The first chapter unfolds. " * 8 + "</p>"),
        ("ch2", "<h1>Chapter 2</h1><p>" + "The second chapter unfolds. " * 8 + "</p>"),
    ]
    toc = [
        ("ch1.xhtml", "Begin Reading"), ("copyright.xhtml", "Copyright"),
        ("ch1.xhtml", "Chapter 1"), ("ch2.xhtml", "Chapter 2"),
    ]
    return _parse_epub(tmp_path, pages, toc)


def test_epub_document_listed_twice_is_narrated_once_under_its_own_title(tmp_path):
    # R1 INGEST-7, R2-INGEST-6, R2-E4
    chapters = _begin_reading_book(tmp_path)

    assert [chapter.title for chapter in chapters] == ["Chapter 1", "Chapter 2"]
    joined = _joined(chapters)
    assert joined.count("The first chapter unfolds.") == 8
    assert joined.count("The second chapter unfolds.") == 8
    assert "All rights reserved" not in joined


def test_epub_interleaved_appendix_entries_narrate_each_part_once(tmp_path):
    # R2-E7
    pages = [
        ("ch1", "<h1>Chapter 1</h1><p>" + "Alpha prose goes here. " * 8 + "</p>"),
        ("ch2", "<h1>Chapter 2</h1><p>" + "Beta prose goes here. " * 8 + "</p>"),
        ("app", '<h1 id="a">Appendix A</h1><p>' + "Appendix A text here. " * 8
         + '</p><h1 id="b">Appendix B</h1><p>' + "Appendix B text here. " * 8 + "</p>"),
    ]
    toc = [("ch1.xhtml", "Chapter 1"), ("app.xhtml#a", "Appendix A"), ("ch2.xhtml", "Chapter 2"), ("app.xhtml#b", "Appendix B")]

    joined = _joined(_parse_epub(tmp_path, pages, toc))

    for sentence in ("Alpha prose", "Beta prose", "Appendix A text", "Appendix B text"):
        assert joined.count(sentence) == 8, sentence


def test_chapter_titles_mentioning_navigation_words_are_kept_but_a_contents_page_is_not():
    # R1 INGEST-6
    prose = "The harbour lights were lit one by one as the boats came home. " * 12
    contents = "Contents\nThe Harbour\nWhat the Contents Were\nNavigation by the Stars"
    document = Document(
        title="Stories",
        chapters=[
            Chapter(number=1, title="Contents", content=contents),
            Chapter(number=2, title="The Harbour", content="The Harbour\n" + prose),
            Chapter(number=3, title="What the Contents Were", content="What the Contents Were\n" + prose),
            Chapter(number=4, title="Navigation by the Stars", content="Navigation by the Stars\n" + prose),
        ],
    )

    normalized = normalize_extracted_document(document, source_type=".epub", skip_non_prose_sections=True)

    assert [chapter.title for chapter in normalized.chapters] == [
        "The Harbour", "What the Contents Were", "Navigation by the Stars",
    ]


# PDF -----------------------------------------------------------------------

_TITLE_PAGE = "Coastal Ecology\n\nA Field Guide\n\nJane Doe\n\nSpringer"
_SPRINGER_IMPRINT = (
    "ISSN 1234-5678\nISBN 978-3-030-00000-0 (eBook)\nhttps://doi.org/10.1007/978-3-030-00000-0\n"
    "© The Editor(s) 2021. This book is an open access publication.\n"
    "The publisher remains neutral with regard to jurisdictional claims."
)
_COPYRIGHT_PAGE = (
    "Copyright 2021 by Jane Doe. All rights reserved.\n"
    "No part of this publication may be reproduced without permission.\n"
    "ISBN 978-0-00-000000-0\nFirst Edition"
)
_CONTENTS_PAGE = "Contents\n\n1 Introduction ......... 5\n2 Methods ......... 7\n3 Results ......... 9"
_PREFACE_PAGE = "Preface\n\n" + "\n".join(
    f"This preface sentence {i} explains why the guide was written for students." for i in range(10)
)
_PDF_FRONT_MATTER_WORDS = ["Field Guide", "ISBN", "rights reserved", "........."]


def _prose(tag: str, lines: int = 12) -> str:
    return "\n".join(f"{tag} sentence number {i} continues the story of the keeper and the sea." for i in range(lines))


_NUMBERED_BODY = [
    "1 Introduction\n\n" + _prose("Alpha"), _prose("Alpha2"),
    "2 Methods\n\n" + _prose("Beta"), _prose("Beta2"),
    "3 Results\n\n" + _prose("Gamma"), _prose("Gamma2"),
]
_BODY_WORDS = ["Alpha sentence", "Alpha2 sentence", "Beta sentence", "Gamma2 sentence"]


def _write_pdf(path: Path, pages: list[str], toc=None, title: str | None = None) -> Path:
    document = fitz.open()
    for text in pages:
        document.new_page().insert_textbox(fitz.Rect(50, 50, 560, 800), text, fontsize=9)
    if toc:
        document.set_toc(toc)
    if title:
        document.set_metadata({"title": title})
    document.save(str(path))
    document.close()
    return path


def _parse_pdf(path: Path):
    return PDFParser(path, extraction_backend="fitz").parse()


_PDF_FRONT_MATTER_CASES = [
    pytest.param(
        [_TITLE_PAGE, _SPRINGER_IMPRINT, _CONTENTS_PAGE, _PREFACE_PAGE] + _NUMBERED_BODY,
        [[1, "Front Matter", 1], [1, "1 Introduction", 5], [1, "2 Methods", 7], [1, "3 Results", 9]],
        id="R3-S1-front-matter-bookmark",
    ),
    pytest.param(
        [_TITLE_PAGE, _COPYRIGHT_PAGE, _CONTENTS_PAGE, _PREFACE_PAGE] + _NUMBERED_BODY,
        [[1, "Front Matter", 1], [2, "Preface", 4], [1, "1 Introduction", 5], [1, "2 Methods", 7],
         [1, "3 Results", 9]],
        id="R3-S4-front-matter-parent",
    ),
    pytest.param(
        [_TITLE_PAGE, _COPYRIGHT_PAGE, _CONTENTS_PAGE, _PREFACE_PAGE] + _NUMBERED_BODY,
        [[1, "Half-Title", 1], [1, "Copyright", 2], [1, "Contents", 3], [1, "1 Introduction", 5],
         [1, "2 Methods", 7], [1, "3 Results", 9]],
        id="R3-S2-half-title",
    ),
    pytest.param(
        ["Also from Jane Doe\nThe Harbour\nThe Return", _TITLE_PAGE, _COPYRIGHT_PAGE, _CONTENTS_PAGE]
        + _NUMBERED_BODY,
        [[1, "Books by Jane Doe", 1], [1, "Title", 2], [1, "Copyright", 3], [1, "Contents", 4],
         [1, "1 Introduction", 5], [1, "2 Methods", 7], [1, "3 Results", 9]],
        id="R3-S3-books-by",
    ),
    pytest.param(
        [_TITLE_PAGE, _COPYRIGHT_PAGE, _CONTENTS_PAGE, _PREFACE_PAGE] + _NUMBERED_BODY,
        [[1, "Coastal Ecology", 1], [2, "Introduction", 5], [2, "Methods", 7], [2, "Results", 9]],
        id="R3-S6-book-title-root",
    ),
    pytest.param(
        [_TITLE_PAGE, _COPYRIGHT_PAGE, _CONTENTS_PAGE, _PREFACE_PAGE] + _NUMBERED_BODY,
        [[1, "Coastal Ecology", 1], [1, "1 Introduction", 5], [1, "2 Methods", 7], [1, "3 Results", 9]],
        id="book-title-beside-numbered-chapters",
    ),
    pytest.param(
        [_TITLE_PAGE, _COPYRIGHT_PAGE, _CONTENTS_PAGE] + _NUMBERED_BODY,
        [[1, "The Lighthouse Keeper", 1], [2, "1 Introduction", 4], [2, "2 Methods", 6],
         [2, "3 Results", 8]],
        id="R2-P1-book-title-root",
    ),
    pytest.param(
        [_TITLE_PAGE, _COPYRIGHT_PAGE, _CONTENTS_PAGE] + _NUMBERED_BODY,
        [[1, "Cover", 1], [1, "1 Introduction", 4], [1, "2 Methods", 6], [1, "3 Results", 8]],
        id="R2-P2-cover",
    ),
    pytest.param(
        [_TITLE_PAGE, _COPYRIGHT_PAGE, _CONTENTS_PAGE, "For my mother, who kept the light burning."]
        + _NUMBERED_BODY,
        [[1, "Title Page", 1], [1, "Copyright", 2], [1, "Contents", 3], [1, "Dedication", 4],
         [1, "1 Introduction", 5], [1, "2 Methods", 7], [1, "3 Results", 9]],
        id="R2-P3-dedication",
    ),
]


@pytest.mark.parametrize(("pages", "toc"), _PDF_FRONT_MATTER_CASES)
def test_pdf_outlined_front_matter_is_not_narrated(tmp_path, pages, toc):
    path = _write_pdf(tmp_path / "book.pdf", pages, toc)

    document = _parse_pdf(path)

    _check(_joined(document.chapters), _BODY_WORDS, _PDF_FRONT_MATTER_WORDS + ["my mother", "Also from"])
    assert document.chapters[-1].title in {"3 Results", "Results"}


def test_pdf_preface_listed_before_numbered_chapters_is_narrated(tmp_path):
    # R3-S4 counterpart: main also narrates a listed Preface.
    path = _write_pdf(
        tmp_path / "book.pdf",
        [_TITLE_PAGE, _COPYRIGHT_PAGE, _CONTENTS_PAGE, _PREFACE_PAGE] + _NUMBERED_BODY,
        [[1, "Front Matter", 1], [2, "Preface", 4], [1, "1 Introduction", 5], [1, "2 Methods", 7],
         [1, "3 Results", 9]],
    )

    document = _parse_pdf(path)

    assert [chapter.title for chapter in document.chapters] == ["Preface", "1 Introduction", "2 Methods", "3 Results"]
    _check(_joined(document.chapters), ["preface sentence 3", *_BODY_WORDS], _PDF_FRONT_MATTER_WORDS)


@pytest.mark.parametrize(
    ("toc", "titles"),
    [
        pytest.param(
            [[1, "Copyright", 1], [1, "Contents", 2], [1, "Foreword", 3], [1, "1 Introduction", 5],
             [1, "2 Methods", 7], [1, "3 Results", 9]],
            ["Foreword", "1 Introduction", "2 Methods", "3 Results"],
            id="R1-INGEST-3-foreword",
        ),
        pytest.param(
            [[1, "Copyright", 1], [1, "Contents", 2], [1, "Foreword", 3], [1, "Part I The Sea", 4],
             [2, "1 Introduction", 5], [2, "2 Methods", 7], [2, "3 Results", 9]],
            ["Foreword", "1 Introduction", "2 Methods", "3 Results"],
            id="R2-INGEST-5-foreword-before-parts",
        ),
    ],
)
def test_pdf_foreword_after_outlined_front_matter_is_narrated(tmp_path, toc, titles):
    foreword = "Foreword\n\n" + _prose("Foreword", 6)
    path = _write_pdf(
        tmp_path / "book.pdf",
        [_COPYRIGHT_PAGE, _CONTENTS_PAGE, foreword, "Part I The Sea"] + _NUMBERED_BODY,
        toc,
    )

    document = _parse_pdf(path)

    assert [chapter.title for chapter in document.chapters] == titles
    _check(_joined(document.chapters), ["Foreword sentence", *_BODY_WORDS], _PDF_FRONT_MATTER_WORDS)


@pytest.mark.parametrize("first_page", [1, 2])
def test_pdf_parent_chapter_opening_before_its_sections_is_narrated(tmp_path, first_page):
    # R1 INGEST-3: level 2 wins the outline vote, but the level-1 parent's
    # opening page belongs to the book.
    offset = first_page - 1
    pages = [_CONTENTS_PAGE] * offset + [
        "Chapter One\n\n" + _prose("Opening", 6), _prose("SectionA", 6), _prose("SectionB", 6),
        "Chapter Two\n\n" + _prose("Second", 6), _prose("SectionC", 6),
    ]
    toc = [[1, "Contents", 1]] if offset else []
    toc += [
        [1, "Chapter One", 1 + offset], [2, "A section", 2 + offset], [2, "B section", 3 + offset],
        [1, "Chapter Two", 4 + offset], [2, "C section", 5 + offset],
    ]
    path = _write_pdf(tmp_path / "book.pdf", pages, toc)

    document = _parse_pdf(path)

    assert [chapter.title for chapter in document.chapters] == ["Chapter One", "A section", "B section", "C section"]
    _check(_joined(document.chapters), ["Opening sentence", "SectionA", "Second sentence", "SectionC"], ["........."])


def test_pdf_part_opening_after_unoutlined_front_matter_is_narrated_without_the_front_matter(tmp_path):
    # R3-S5
    path = _write_pdf(
        tmp_path / "book.pdf",
        [_TITLE_PAGE, _COPYRIGHT_PAGE, _CONTENTS_PAGE, "Part One: The Coast\n\n" + _prose("PartIntro")]
        + _NUMBERED_BODY,
        [[1, "Part One: The Coast", 4], [2, "1 Introduction", 5], [2, "2 Methods", 7], [1, "Part Two", 9],
         [2, "3 Results", 9]],
    )

    document = _parse_pdf(path)

    _check(_joined(document.chapters), ["PartIntro sentence", *_BODY_WORDS], _PDF_FRONT_MATTER_WORDS)


@pytest.mark.parametrize(
    "toc",
    [
        pytest.param([[1, "Contents", 1], [2, "A section", 2], [2, "B section", 2], [2, "C section", 4]],
                     id="R1-INGEST-2-sections"),
        pytest.param([[1, "Contents", 1], [1, "Chapter 1", 2], [1, "Interlude", 2], [1, "Chapter 2", 4]],
                     id="R2-P4-interlude"),
    ],
)
def test_pdf_page_shared_by_two_outline_entries_is_narrated_once(tmp_path, toc):
    pages = [_CONTENTS_PAGE] + [f"Marker page {page}. " + _prose(f"Page{page}", 4) for page in range(2, 6)]
    path = _write_pdf(tmp_path / "book.pdf", pages, toc)

    document = _parse_pdf(path)

    joined = _joined(document.chapters)
    assert [joined.count(f"Marker page {page}.") for page in range(2, 6)] == [1, 1, 1, 1]


def test_pdf_without_an_outline_is_one_chapter_named_after_the_document(tmp_path):
    # R1 INGEST-10
    path = _write_pdf(tmp_path / "essay.pdf", [_prose("Essay"), _prose("Essay2")], title="Harbour Essays")

    document = _parse_pdf(path)

    assert [chapter.title for chapter in document.chapters] == ["Harbour Essays"]


def _running_head_pages() -> list[str]:
    # Each section name repeats as a running head on its three pages.
    return [
        f"{section}\n{3 * index + page + 1}\n" + _prose(f"{section}{page}", 30)
        for index, section in enumerate(("Prologue", "Introduction", "Epilogue"))
        for page in range(3)
    ]


@pytest.mark.parametrize(
    ("pages", "toc", "title"),
    [
        *(pytest.param(*case.values, None, id=case.id) for case in _PDF_FRONT_MATTER_CASES),
        pytest.param(
            [_CONTENTS_PAGE, *(f"Marker page {page}." for page in range(2, 9))],
            [[1, "Contents", 1], [1, "Chapter One", 2], [2, "A section", 3], [2, "B section", 3],
             [1, "Chapter Two", 5], [2, "C section", 6], [1, "Index", 8]],
            None,
            id="R1-INGEST-9-outline-levels",
        ),
        pytest.param(
            [_CONTENTS_PAGE, *(f"Marker page {page}." for page in range(2, 6))],
            [[1, "Contents", 1], [1, "Chapter 1", 2], [1, "Interlude", 2], [1, "Chapter 2", 4]],
            None,
            id="R2-P4-same-page",
        ),
        pytest.param([_prose("Essay"), _prose("Essay2")], None, "Harbour Essays", id="R2-INGEST-7-title-only"),
        pytest.param(
            [f"{heading}\n\n" + _prose(heading) for heading in ("Prologue", "Introduction", "Epilogue")],
            None,
            "Harbour Tales",
            id="R2-INGEST-7-page-headings",
        ),
        pytest.param(_running_head_pages(), None, None, id="R3-FINAL-4-running-heads"),
    ],
)
def test_pdf_preflight_lists_the_chapters_the_parser_produces(tmp_path, pages, toc, title):
    path = _write_pdf(tmp_path / "book.pdf", pages, toc, title)

    chapters = _parse_pdf(path).chapters
    preflight = preflight_source(path)

    assert preflight["chapter_titles"] == [chapter.title for chapter in chapters]
    assert preflight["chapter_count"] == len(chapters)


def test_pdf_without_an_outline_splits_on_page_headings(tmp_path):
    # R2-INGEST-7
    pages = [f"{heading}\n\n" + _prose(heading) for heading in ("Prologue", "Introduction", "Epilogue")]
    path = _write_pdf(tmp_path / "tales.pdf", pages, title="Harbour Tales")

    assert [chapter.title for chapter in _parse_pdf(path).chapters] == ["Prologue", "Introduction", "Epilogue"]


def test_pdf_running_heads_without_an_outline_are_not_chapter_headings(tmp_path):
    # R3 R2-FINAL-4
    path = _write_pdf(tmp_path / "running.pdf", _running_head_pages())

    assert [chapter.title for chapter in _parse_pdf(path).chapters] == ["running"]


def test_pdf_endnotes_on_the_last_page_are_not_narrated(tmp_path):
    # R3 pdf_notes: bare-numbered endnotes under a Notes heading.
    notes = (
        "Notes\n1 Interview with the author, March 2015.\n"
        "2 The keeper's log, held at the county archive in Wick.\n3 Ibid.\n"
        "4 Quoted in Smith, Lights of the North, 112.\n"
        "5 The figures come from the harbour board's annual report.\n"
        "6 See chapter 3 for the full account of the wreck."
    )
    path = _write_pdf(
        tmp_path / "notes.pdf",
        ["1 The Start\n\n" + _prose("Alpha"), _prose("Alpha2"), "2 The End\n\n" + _prose("Beta"),
         _prose("Beta2", 4) + "\n" + notes],
        [[1, "1 The Start", 1], [1, "2 The End", 3]],
    )

    joined = _joined(_parse_pdf(path).chapters)

    _check(joined, ["Alpha sentence", "Beta2 sentence number 3"], ["Notes", "Interview with the author", "county archive"])


# PDF page normalization ----------------------------------------------------

_FIRST = "The first page tells of the winter storms that closed the harbour for a month.\nNobody could land."
_SECOND = "On the second page the keeper writes his final entry in the log and closes it.\nThen he goes home."
_THIRD = "The third page tells how the light was finally automated in 1998, ending an era.\nThe last keeper left."


def _normalized_pages(*pages: str) -> list[str]:
    return [str(page["text"]) for page in normalize_pdf_artifacts([{"text": text} for text in pages])]


@pytest.mark.parametrize(
    ("notes", "note_words"),
    [
        pytest.param(
            "Notes\n1 Interview with the author, March 2015.\n"
            "2 The keeper's log, held at the county archive in Wick.\n3 Ibid.\n"
            "4 Quoted in Smith, Lights of the North, 112.\n"
            "5 The figures come from the harbour board's annual report.\n"
            "6 See chapter 3 for the full account of the wreck.",
            ["Interview with the author", "county archive", "annual report", "full account"],
            id="R3-bare-numbered-endnotes",
        ),
        pytest.param(
            "Notes\n2 The keeper's log, held at the county archive in Wick.\n"
            "3 The figures come from the harbour board's annual report.\n"
            "4 See chapter 3 for the full account of the wreck.",
            ["county archive", "annual report", "full account"],
            id="bare-numbered-endnotes-continuing-a-run",
        ),
        pytest.param(
            "Notes\n1. The account comes from the ship's log, which survives in the\n"
            "National Archives at Kew, together with the keeper's letters to his\n"
            "brother in Glasgow and a single photograph of the station.\n"
            "2. On the wreck itself, see the inquiry report published in the\n"
            "following spring by the Board of Trade.",
            ["ship's log", "National Archives", "Board of Trade"],
            id="R3-wrapped-punctuated-endnotes",
        ),
        pytest.param(
            "Bibliography\nAbbott, Edward. The Coast and Its People: A History of the Northern\n"
            "Shore and Its Lights. London: Harbour Books, 1998.\n"
            "Baker, Ann. Tides and Keepers: Lives on the Rock. Edinburgh: Birlinn,\n2004.\n"
            "Carr, Thomas. Wrecks of the Pentland Firth. Wick: North Books, 1971.",
            ["Abbott", "Harbour Books", "Pentland Firth"],
            id="R3-wrapped-bibliography",
        ),
    ],
)
def test_pdf_reference_sections_are_cut_from_the_page(notes, note_words):
    first, second, third = _normalized_pages(_FIRST, _SECOND + "\n" + notes, _THIRD)

    _check("\n".join((first, second, third)), [_FIRST, _SECOND, _THIRD], note_words + ["Notes\n", "Bibliography"])


@pytest.mark.parametrize(
    ("page", "kept"),
    [
        pytest.param(
            "Recipe 1 Lemon Cake\nThe kitchen was quiet that morning and the oven ticked as it warmed.\n"
            "Notes\nUse unwaxed lemons for the zest.\nRecipe 2 Plum Tart\n4 ripe plums\n200 g flour\n"
            "100 g butter\n1 egg\n2 tbsp sugar\nRub the butter into the flour until it looks like sand.",
            ["Use unwaxed lemons", "Recipe 2 Plum Tart", "4 ripe plums", "Rub the butter"],
            id="R1-INGEST-4-cookbook-notes-box",
        ),
        pytest.param(
            "The war ended and the town rebuilt.\nNotes\nThe archive was moved in 1946.\n"
            "In 1948 the council voted to rebuild the old bridge, which had stood since 1802,\n"
            "and by 1951 the work was finished. The mayor, who had served since 1939,\n"
            "opened it in a short ceremony. Few people came.",
            ["The archive was moved", "In 1948 the council", "Few people came."],
            id="R2-INGEST-3-history-notes-box",
        ),
    ],
)
def test_pdf_notes_box_inside_body_text_is_kept(page, kept):
    normalized, _ = _normalized_pages(page, "Page two continues with more text about the town and its people.")

    _check(normalized, kept, [])


@pytest.mark.parametrize(
    ("page", "kept", "removed"),
    [
        pytest.param(
            "The town grew quickly. As shown in\nFigure 2, the population grew by forty percent over the decade,\n"
            "which surprised everyone.",
            ["Figure 2, the population grew"], [],
            id="R1-INGEST-5-wrapped-figure-reference",
        ),
        pytest.param(
            "The results were clear.\nFigure 2 (left) shows the growth rate over the decade, and\n"
            "the right panel shows the decline in the same period.",
            ["Figure 2 (left) shows the growth rate"], [],
            id="R2-INGEST-4-panel-reference",
        ),
        pytest.param(
            "2.3 Growth of the Cities\nFigure 2.1 Map of the cities in 1900\n"
            "The cities grew quickly after the railway arrived, and many people moved.",
            ["The cities grew quickly"], ["Map of the cities"],
            id="R2-INGEST-4-caption-after-heading",
        ),
        pytest.param(
            "The harbour was rebuilt in stone.\nFigure 3. The harbour wall in 1880.\nThe work took ten years.",
            ["The work took ten years."], ["harbour wall in 1880"],
            id="standalone-caption",
        ),
    ],
)
def test_pdf_figure_captions_are_removed_but_figure_references_in_prose_are_kept(page, kept, removed):
    normalized, _ = _normalized_pages(page, "Another page of prose here that ends properly.")

    _check(normalized, kept, removed)


# TXT and Markdown ------------------------------------------------------------

def test_txt_column_aligned_scores_are_kept(tmp_path):
    # R1 INGEST-8
    path = tmp_path / "scores.txt"
    path.write_text(
        "Round one\nSmith    12\nJones    9\nRound two\nSmith    15\nJones    11\n"
        "The final was decided on the last throw of the evening.\n",
        encoding="utf-8",
    )

    content = TextParser(path).parse().chapters[0].content

    _check(content, ["Smith    12", "Jones    9", "Smith    15", "Jones    11", "last throw"], [])


def test_markdown_preflight_counts_the_chapters_the_parser_produces(tmp_path):
    # R1 INGEST-9
    from modules.ingestion.markdown_parser import MarkdownParser

    path = tmp_path / "pre.md"
    path.write_text(
        "A preamble line before the title.\n\n# The Book\n\nIntro text.\n\n## One\n\nFirst text.\n\n"
        "## Two\n\nSecond text.\n",
        encoding="utf-8",
    )

    chapters = MarkdownParser(path).parse().chapters

    assert preflight_source(path)["chapter_count"] == len(chapters)
