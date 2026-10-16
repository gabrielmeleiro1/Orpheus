"""A PDF chapter is announced once: its printed heading and running header are not narrated."""

from __future__ import annotations

from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")

from modules.ingestion.pdf_parser import PDFParser  # noqa: E402
from modules.ingestion.text_layout import opening_heading_text  # noqa: E402
from modules.pipeline.chapter_narration import chapter_announcement, chapter_body  # noqa: E402
from modules.text.document_normalizer import normalize_extracted_document  # noqa: E402

_TITLE = "The Lantern at Harrow Point"
_CHAPTERS = ["The Keeper's Daughter", "Salt and Iron", "A Letter from Falmouth"]
_PROSE = (
    "The vicar carried the brass lantern after the storm, and Clara watched from the "
    "harbour wall as the light changed over the water. "
)


def _book(path: Path) -> Path:
    """Outline chapters, a printed heading, and per-page running header and folio."""
    doc = pymupdf.open()
    width, height = 595, 842
    toc = []
    page_no = 1
    for number, name in enumerate(_CHAPTERS, start=1):
        for page_in_chapter in range(2):
            page = doc.new_page(width=width, height=height)
            page.insert_text((60, 40), _TITLE.upper(), fontsize=8, fontname="helv")
            page.insert_text((width - 140, 40), f"Chapter {number}", fontsize=8, fontname="helv")
            page.insert_text((width / 2 - 6, height - 30), str(page_no), fontsize=9, fontname="helv")
            top = 80
            if page_in_chapter == 0:
                toc.append([1, f"Chapter {number}: {name}", page_no])
                page.insert_textbox(
                    pymupdf.Rect(60, 90, width - 60, 160), f"CHAPTER {number}\n{name}",
                    fontsize=20, fontname="tiro", align=1,
                )
                top = 180
            page.insert_textbox(
                pymupdf.Rect(60, top, width - 60, height - 60), "    " + _PROSE * 12,
                fontsize=11, fontname="tiro", align=3,
            )
            page_no += 1
    doc.set_toc(toc)
    doc.save(path)
    doc.close()
    return path


def test_pdf_chapters_announce_their_title_once(tmp_path):
    document = normalize_extracted_document(
        PDFParser(_book(tmp_path / "book.pdf")).parse(), source_type="pdf",
    )

    assert [chapter.title for chapter in document.chapters] == [
        f"Chapter {index}: {name}" for index, name in enumerate(_CHAPTERS, start=1)
    ]
    for index, chapter in enumerate(document.chapters, start=1):
        body = chapter_body(chapter)
        assert body.startswith("The vicar carried the brass lantern"), body[:80]
        assert f"Chapter {index}" not in body and f"CHAPTER {index}" not in body
        assert _TITLE.upper() not in body
        assert chapter_announcement(chapter) == f"Chapter number {index}. {_CHAPTERS[index - 1]}."


@pytest.mark.parametrize(
    ("content", "title", "expected"),
    [
        ("Chapter 1\nCHAPTER 1\nThe Keeper's Daughter\nProse.", "Chapter 1: The Keeper's Daughter",
         "Chapter 1\nCHAPTER 1\nThe Keeper's Daughter"),
        ("CHAPTER 2\nSalt and Iron\nProse.", "Chapter 2: Salt and Iron", "CHAPTER 2\nSalt and Iron"),
        ("Salt and Iron\nProse.", "Salt and Iron", "Salt and Iron"),
        # A first sentence that merely starts like the title is prose, not a heading.
        ("Salt and Iron were the cargo.\nMore.", "Salt and Iron", ""),
        ("Chapter 1\nChapter 1 was short.", "Chapter 1: The Keeper's Daughter", ""),
    ],
)
def test_opening_heading_matches_printed_headings_only(content, title, expected):
    assert opening_heading_text(content, title) == expected
