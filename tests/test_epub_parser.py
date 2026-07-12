"""
Test EPUB Parser Module
=======================
Unit tests for the EPUB parser using a generated sample EPUB.
"""

import sys
import tempfile
import warnings
from pathlib import Path
from bs4 import XMLParsedAsHTMLWarning
import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from modules.errors import ChapterTooLongError, EmptyChapterError
from modules.ingestion.epub_parser import EPUBParser, Document
from modules.ingestion.epub_sections import EPUBSection


def create_sample_epub(output_path: Path) -> None:
    """
    Create a simple sample EPUB for testing.
    
    Uses ebooklib to generate a multi-chapter EPUB.
    """
    from ebooklib import epub
    
    book = epub.EpubBook()
    
    # Set metadata
    book.set_identifier("test-audiobook-123")
    book.set_title("Sample Audiobook EPUB")
    book.set_language("en")
    book.add_author("Test Author")
    book.add_metadata("DC", "publisher", "Test Publisher")
    
    # Create chapters
    chapters = []
    
    # Chapter 1
    ch1 = epub.EpubHtml(title="Chapter 1: The Beginning", file_name="ch1.xhtml", lang="en")
    ch1.content = """
    <html>
    <head><title>Chapter 1</title></head>
    <body>
        <h1>Chapter 1: The Beginning</h1>
        <p>This is the first chapter of our sample EPUB audiobook.
        It contains several paragraphs of text that will be parsed and 
        converted to clean text format suitable for TTS.</p>
        <p>Lorem ipsum dolor sit amet, consectetur adipiscing elit.
        Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.</p>
    </body>
    </html>
    """
    chapters.append(ch1)
    
    # Chapter 2
    ch2 = epub.EpubHtml(title="Chapter 2: The Middle", file_name="ch2.xhtml", lang="en")
    ch2.content = """
    <html>
    <head><title>Chapter 2</title></head>
    <body>
        <h1>Chapter 2: The Middle</h1>
        <p>This is the second chapter with more content.
        We are testing the parser's ability to extract text from 
        multiple chapters correctly.</p>
        <p>Ut enim ad minim veniam, quis nostrud exercitation
        ullamco laboris nisi ut aliquip ex ea commodo consequat.</p>
    </body>
    </html>
    """
    chapters.append(ch2)
    
    # Chapter 3
    ch3 = epub.EpubHtml(title="Chapter 3: The End", file_name="ch3.xhtml", lang="en")
    ch3.content = """
    <html>
    <head><title>Chapter 3</title></head>
    <body>
        <h1>Chapter 3: The End</h1>
        <p>This is the final chapter of our test document.
        It concludes the sample EPUB with a proper ending.</p>
        <p>Duis aute irure dolor in reprehenderit in voluptate
        velit esse cillum dolore eu fugiat nulla pariatur.</p>
    </body>
    </html>
    """
    chapters.append(ch3)
    
    # Add chapters to book
    for ch in chapters:
        book.add_item(ch)
    
    # Create table of contents
    book.toc = tuple(chapters)
    
    # Add navigation
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    
    # Create spine
    book.spine = ["nav"] + chapters
    
    # Write EPUB
    epub.write_epub(str(output_path), book)
    print(f"✓ Created sample EPUB: {output_path}")


def create_short_story_collection_epub(output_path: Path) -> None:
    """Create an EPUB shaped like a short-story collection with front/back matter."""
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("short-story-collection")
    book.set_title("Collected Stories Fixture")
    book.set_language("en")
    book.add_author("Fixture Author")

    titlepage = epub.EpubHtml(title="Title Page", file_name="titlepage.xhtml", lang="en")
    titlepage.content = "<html><body><h1>Collected Stories Fixture</h1></body></html>"

    toc_page = epub.EpubHtml(title="Table of Contents", file_name="contents.xhtml", lang="en")
    toc_page.content = """
    <html><body>
      <h1>Table of Contents</h1>
      <p>Title Page</p>
      <p>Copyright</p>
      <p>First Story</p>
      <p>Second Story</p>
      <p>About the Author</p>
    </body></html>
    """

    copyright_page = epub.EpubHtml(title="Copyright", file_name="copyright.xhtml", lang="en")
    copyright_page.content = """
    <html><body>
      <p>Copyright 1955 by Fixture Author. All rights reserved. This page is
      publishing metadata and must not become an audiobook chapter.</p>
    </body></html>
    """

    story_one = epub.EpubHtml(title="First Story", file_name="story-one.xhtml", lang="en")
    story_one.content = """
    <html><body>
      <h1>First Story</h1>
      <p>First story opening paragraph with enough text to be treated as a
      real story section by the parser and the preflight counter.</p>
      <p>The first story continues with a second paragraph that should stay in
      the first generated audiobook chapter.</p>
    </body></html>
    """

    story_two = epub.EpubHtml(title="Second Story", file_name="story-two.xhtml", lang="en")
    story_two.content = """
    <html><body>
      <h1>Second Story</h1>
      <p>Second story opening paragraph with enough text to be treated as a
      real story section by the parser and the preflight counter.</p>
      <p>The second story continues with a second paragraph that should stay in
      the second generated audiobook chapter.</p>
    </body></html>
    """

    about_author = epub.EpubHtml(title="About the Author", file_name="about.xhtml", lang="en")
    about_author.content = """
    <html><body>
      <h1>About the Author</h1>
      <p>Fixture Author biography text that is long enough to be readable but
      should remain outside the generated story chapter list.</p>
    </body></html>
    """

    documents = [titlepage, toc_page, copyright_page, story_one, story_two, about_author]
    for document in documents:
        book.add_item(document)

    book.toc = tuple(documents)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + documents
    epub.write_epub(str(output_path), book)


def create_single_document_story_collection_epub(output_path: Path) -> None:
    """Create an EPUB where multiple stories live in one XHTML spine item."""
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("single-document-short-story-collection")
    book.set_title("Single Document Stories Fixture")
    book.set_language("en")
    book.add_author("Fixture Author")

    copyright_page = epub.EpubHtml(title="Copyright", file_name="copyright.xhtml", lang="en")
    copyright_page.content = """
    <html><body>
      <p>Copyright page with enough publishing metadata text to be readable,
      but it should not become a generated audiobook chapter.</p>
    </body></html>
    """

    stories = epub.EpubHtml(title="Stories", file_name="stories.xhtml", lang="en")
    stories.content = """
    <html><body>
      <h1 id="first-story">First Story</h1>
      <p>Only the first story should contain this sentence in its audiobook
      chapter after section splitting.</p>
      <p>More first story body text keeps the section above the minimum length.</p>
      <h1 id="second-story">Second Story</h1>
      <p>Only the second story should contain this sentence in its audiobook
      chapter after section splitting.</p>
      <p>More second story body text keeps the section above the minimum length.</p>
    </body></html>
    """

    about_author = epub.EpubHtml(title="About the Author", file_name="about.xhtml", lang="en")
    about_author.content = """
    <html><body>
      <h1>About the Author</h1>
      <p>Fixture biography text that is long enough to be readable but should
      be excluded from the story chapter list.</p>
    </body></html>
    """

    for document in [copyright_page, stories, about_author]:
        book.add_item(document)

    book.toc = (
        epub.Link("copyright.xhtml", "Copyright", "copyright"),
        epub.Link("stories.xhtml#first-story", "First Story", "first-story"),
        epub.Link("stories.xhtml#second-story", "Second Story", "second-story"),
        epub.Link("about.xhtml", "About the Author", "about-author"),
    )
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", copyright_page, stories, about_author]
    epub.write_epub(str(output_path), book)


def create_epub_with_social_backmatter(output_path: Path) -> None:
    """Create an EPUB with a readable publisher social page after story content."""
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("social-backmatter")
    book.set_title("Social Backmatter Fixture")
    book.set_language("en")
    book.add_author("Fixture Author")

    story = epub.EpubHtml(title="Actual Story", file_name="story.xhtml", lang="en")
    story.content = """
    <html><body>
      <h1>Actual Story</h1>
      <p>This is the story body that should become the only generated
      audiobook chapter in this fixture.</p>
      <p>More story body text keeps the section comfortably above the minimum
      length threshold used by the parser.</p>
    </body></html>
    """

    social = epub.EpubHtml(title="Follow Penguin", file_name="follow.xhtml", lang="en")
    social.content = """
    <html><body>
      <h1>Follow Penguin</h1>
      <p>Let the conversation begin. Follow the Penguin on Twitter and keep
      up-to-date with all our stories at youtube.com/penguinbooks and
      facebook.com/penguinbooks.</p>
    </body></html>
    """

    for document in [story, social]:
        book.add_item(document)

    book.toc = (
        epub.Link("story.xhtml", "Actual Story", "actual-story"),
        epub.Link("follow.xhtml", "Follow Penguin", "follow-penguin"),
    )
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", story, social]
    epub.write_epub(str(output_path), book)


def create_epub_with_sparse_toc(output_path: Path) -> None:
    """Create an EPUB whose TOC omits readable body chapters from the spine."""
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("sparse-toc-missing-body")
    book.set_title("Sparse TOC Fixture")
    book.set_language("en")
    book.add_author("Fixture Author")

    title_page = epub.EpubHtml(title="The Fixture Book", file_name="front.xhtml", lang="en")
    title_page.content = """
    <html><body>
      <h1>The Fixture Book</h1>
      <p>The Fixture Book by Fixture Author is presented here as title-page
      front matter with enough text to otherwise look readable.</p>
    </body></html>
    """

    copyright_page = epub.EpubHtml(title="Publication Data", file_name="legal.xhtml", lang="en")
    copyright_page.content = """
    <html><body>
      <p>Copyright page with publishing metadata and enough text to be readable,
      but it should be excluded from generated audiobook chapters.</p>
    </body></html>
    """

    intro = epub.EpubHtml(title="Introduction", file_name="intro.xhtml", lang="en")
    intro.content = """
    <html><body>
      <h1>Introduction</h1>
      <p>Introductory text that is intentionally present in the sparse table of
      contents and should remain first in the generated sections.</p>
    </body></html>
    """

    chapter_one = epub.EpubHtml(title="Chapter One", file_name="chapter-one.xhtml", lang="en")
    chapter_one.content = """
    <html><body>
      <h1>Chapter One</h1>
      <p>First real chapter body text that is present in the reading spine but
      missing from the sparse table of contents fixture.</p>
      <p>Additional first chapter text keeps this section above the parser's
      minimum readable length threshold.</p>
    </body></html>
    """

    chapter_two = epub.EpubHtml(title="Chapter Two", file_name="chapter-two.xhtml", lang="en")
    chapter_two.content = """
    <html><body>
      <h1>Chapter Two</h1>
      <p>Second real chapter body text that is present in the reading spine but
      missing from the sparse table of contents fixture.</p>
      <p>Additional second chapter text keeps this section above the parser's
      minimum readable length threshold.</p>
    </body></html>
    """

    appendix = epub.EpubHtml(title="Appendix", file_name="appendix.xhtml", lang="en")
    appendix.content = """
    <html><body>
      <h1>Appendix</h1>
      <p>Appendix backmatter text is intentionally present in the sparse table
      of contents and should remain after the body chapters.</p>
    </body></html>
    """

    about_author = epub.EpubHtml(title="About the Author", file_name="about.xhtml", lang="en")
    about_author.content = """
    <html><body>
      <h1>About the Author</h1>
      <p>Biography text is readable but structural and must not become an
      audiobook chapter when merging sparse TOC and spine sections.</p>
    </body></html>
    """

    bonus = epub.EpubHtml(title="Bonus Chapter", file_name="bonus.xhtml", lang="en")
    bonus.content = """
    <html><body>
      <h1>Bonus Chapter</h1>
      <p>This readable non-spine document is an auxiliary item and must not be
      inserted into audiobook reading order by sparse TOC recovery.</p>
    </body></html>
    """

    documents = [title_page, copyright_page, intro, chapter_one, chapter_two, appendix, about_author]
    for document in documents:
        book.add_item(document)
    book.add_item(bonus)

    book.toc = (
        epub.Link("front.xhtml", "Title Page", "title-page"),
        epub.Link("legal.xhtml", "Copyright Page", "copyright-page"),
        epub.Link("intro.xhtml", "Introduction", "intro"),
        epub.Link("appendix.xhtml", "Appendix", "appendix"),
        epub.Link("about.xhtml", "About the Author", "about-author"),
    )
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + documents
    epub.write_epub(str(output_path), book)


def create_epub_with_fragmented_structural_toc_document(output_path: Path) -> None:
    """Create a sparse TOC where structural front matter shares an XHTML file."""
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("fragmented-structural-toc")
    book.set_title("Fragmented Structural TOC Fixture")
    book.set_language("en")
    book.add_author("Fixture Author")

    mixed = epub.EpubHtml(title="Mixed", file_name="mixed.xhtml", lang="en")
    mixed.content = """
    <html><body>
      <h1 id="title-page">Title Page</h1>
      <p>Readable front matter that should remain excluded even though it
      shares a document with the first real chapter.</p>
      <h1 id="chapter-one">Chapter One</h1>
      <p>First real chapter body text that shares a document with structural
      front matter and is missing from the sparse table of contents.</p>
      <p>Additional chapter prose keeps the recovered body above the parser's
      minimum readable length threshold.</p>
    </body></html>
    """

    appendix = epub.EpubHtml(title="Appendix", file_name="appendix.xhtml", lang="en")
    appendix.content = """
    <html><body>
      <h1>Appendix</h1>
      <p>Appendix body text is present in the sparse TOC and should remain
      after the recovered mixed-document chapter.</p>
    </body></html>
    """

    book.add_item(mixed)
    book.add_item(appendix)
    book.toc = (
        epub.Link("mixed.xhtml#title-page", "Title Page", "title-page"),
        epub.Link("appendix.xhtml", "Appendix", "appendix"),
    )
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", mixed, appendix]
    epub.write_epub(str(output_path), book)


def create_epub_with_split_toc_markers(output_path: Path) -> None:
    """Create an EPUB whose TOC links point to empty split marker documents."""
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("split-toc-markers")
    book.set_title("Split TOC Marker Fixture")
    book.set_language("en")
    book.add_author("Fixture Author")

    intro = epub.EpubHtml(title="Introduction", file_name="intro.xhtml", lang="en")
    intro.content = """
    <html><body>
      <h1>Introduction</h1>
      <p>Introductory text that should remain before the numbered chapters
      in the generated audiobook section list.</p>
    </body></html>
    """

    chapter_one_marker = epub.EpubHtml(title="", file_name="chapter-one-split-000.xhtml", lang="en")
    chapter_one_marker.content = "<html><body><p>I</p></body></html>"
    part_marker = epub.EpubHtml(title="", file_name="part-one-split-000.xhtml", lang="en")
    part_marker.content = "<html><body><p>PART I</p></body></html>"
    part_title = epub.EpubHtml(title="Part One", file_name="part-one-split-001.xhtml", lang="en")
    part_title.content = "<html><body><h1>Part One</h1></body></html>"
    chapter_one_body = epub.EpubHtml(
        title="First Body Heading",
        file_name="chapter-one-split-001.xhtml",
        lang="en",
    )
    chapter_one_body.content = """
    <html><body>
      <h1>First Body Heading</h1>
      <p>First chapter body text that lives after an empty split marker
      document and must still be recovered from reading order.</p>
    </body></html>
    """
    chapter_one_tail = epub.EpubHtml(title="", file_name="chapter-one-split-002.xhtml", lang="en")
    chapter_one_tail.content = """
    <html><body>
      <p>Short final line.</p>
    </body></html>
    """

    chapter_two_marker = epub.EpubHtml(title="", file_name="chapter-two-split-000.xhtml", lang="en")
    chapter_two_marker.content = "<html><body><p>II</p></body></html>"
    chapter_two_body = epub.EpubHtml(
        title="Second Body Heading",
        file_name="chapter-two-split-001.xhtml",
        lang="en",
    )
    chapter_two_body.content = """
    <html><body>
      <h1>Second Body Heading</h1>
      <p>Second chapter body text that also lives after an empty split marker
      document and should become its own generated section.</p>
    </body></html>
    """

    appendix = epub.EpubHtml(title="Appendix", file_name="appendix.xhtml", lang="en")
    appendix.content = """
    <html><body>
      <h1>Appendix</h1>
      <p>Appendix body text should remain after the recovered chapters.</p>
    </body></html>
    """

    documents = [
        intro,
        part_marker,
        part_title,
        chapter_one_marker,
        chapter_one_body,
        chapter_one_tail,
        chapter_two_marker,
        chapter_two_body,
        appendix,
    ]
    for document in documents:
        book.add_item(document)

    book.toc = (
        epub.Link("intro.xhtml", "Introduction", "intro"),
        epub.Link("part-one-split-000.xhtml", "PART I RECOVERED PART", "part-one"),
        epub.Link("chapter-one-split-000.xhtml", "Chapter I First Chapter", "chapter-one"),
        epub.Link("chapter-two-split-000.xhtml", "Chapter II Second Chapter", "chapter-two"),
        epub.Link("appendix.xhtml", "Appendix", "appendix"),
    )
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + documents
    epub.write_epub(str(output_path), book)


def test_epub_short_story_collection_filters_structural_sections(tmp_path: Path) -> None:
    sample_epub = tmp_path / "short-story-collection.epub"
    create_short_story_collection_epub(sample_epub)

    chapters = EPUBParser(sample_epub).extract_chapters()

    assert [chapter.title for chapter in chapters] == ["First Story", "Second Story"]
    assert "Copyright" not in "\n".join(chapter.content for chapter in chapters)
    assert "About the Author" not in "\n".join(chapter.content for chapter in chapters)


def test_epub_short_story_collection_splits_single_story_document(tmp_path: Path) -> None:
    sample_epub = tmp_path / "single-document-stories.epub"
    create_single_document_story_collection_epub(sample_epub)

    chapters = EPUBParser(sample_epub).extract_chapters()

    assert [chapter.title for chapter in chapters] == ["First Story", "Second Story"]
    assert "Only the first story" in chapters[0].content
    assert "Only the second story" not in chapters[0].content
    assert "Only the second story" in chapters[1].content
    assert "Only the first story" not in chapters[1].content


def test_epub_parser_filters_publisher_social_backmatter(tmp_path: Path) -> None:
    sample_epub = tmp_path / "social-backmatter.epub"
    create_epub_with_social_backmatter(sample_epub)

    chapters = EPUBParser(sample_epub).extract_chapters()

    assert [chapter.title for chapter in chapters] == ["Actual Story"]
    chapter_text = "\n".join(chapter.content for chapter in chapters)
    assert "Follow Penguin" not in chapter_text
    assert "youtube.com/penguinbooks" not in chapter_text


def test_epub_parser_merges_readable_spine_documents_missing_from_sparse_toc(tmp_path: Path) -> None:
    sample_epub = tmp_path / "sparse-toc.epub"
    create_epub_with_sparse_toc(sample_epub)

    chapters = EPUBParser(sample_epub).extract_chapters()

    assert [chapter.title for chapter in chapters] == [
        "Introduction",
        "Chapter One",
        "Chapter Two",
        "Appendix",
    ]
    chapter_text = "\n".join(chapter.content for chapter in chapters)
    assert "First real chapter body text" in chapter_text
    assert "Second real chapter body text" in chapter_text
    assert "Copyright page" not in chapter_text
    assert "Biography text" not in chapter_text
    assert "readable non-spine document" not in chapter_text


def test_epub_sparse_toc_recovers_body_from_document_with_structural_fragment(tmp_path: Path) -> None:
    sample_epub = tmp_path / "fragmented-structural-toc.epub"
    create_epub_with_fragmented_structural_toc_document(sample_epub)

    chapters = EPUBParser(sample_epub).extract_chapters()

    assert [chapter.title for chapter in chapters] == ["Chapter One", "Appendix"]
    assert "First real chapter body text" in chapters[0].content
    assert "Readable front matter" not in chapters[0].content


def test_epub_parser_recovers_chapters_when_toc_targets_empty_split_markers(tmp_path: Path) -> None:
    sample_epub = tmp_path / "split-toc-markers.epub"
    create_epub_with_split_toc_markers(sample_epub)

    chapters = EPUBParser(sample_epub).extract_chapters()

    assert [chapter.title for chapter in chapters] == [
        "Introduction",
        "PART I RECOVERED PART",
        "Chapter I First Chapter",
        "Chapter II Second Chapter",
        "Appendix",
    ]
    assert "PART I RECOVERED PART" in chapters[1].content
    assert "Part One" in chapters[1].content
    assert "First chapter body text" in chapters[2].content
    assert "Short final line." in chapters[2].content
    assert "Second chapter body text" not in chapters[2].content
    assert "Second chapter body text" in chapters[3].content


def test_epub_single_index_document_with_real_toc_title_is_kept(tmp_path: Path) -> None:
    """A structural-looking file stem must not veto a real TOC chapter title."""
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("single-index-document")
    book.set_title("Single Index Document Fixture")
    book.set_language("en")
    book.add_author("Fixture Author")

    chapter = epub.EpubHtml(title="The Whole Story", file_name="index.xhtml", lang="en")
    chapter.content = """
    <html><body>
      <h1>The Whole Story</h1>
      <p>Chapter body text that is comfortably long enough to clear the
      minimum readable length threshold used by the section builder.</p>
      <p>More narrative body text keeps this well above the filter.</p>
    </body></html>
    """
    book.add_item(chapter)
    book.toc = (epub.Link("index.xhtml", "The Whole Story", "the-whole-story"),)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", chapter]

    sample_epub = tmp_path / "single-index.epub"
    epub.write_epub(str(sample_epub), book)

    chapters = EPUBParser(sample_epub).extract_chapters()

    assert [chapter.title for chapter in chapters] == ["The Whole Story"]
    assert "Chapter body text" in chapters[0].content


def test_epub_notes_document_with_narrative_toc_title_is_kept(tmp_path: Path) -> None:
    """notes.xhtml with a narrative TOC title is content, not back matter."""
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("narrative-notes-document")
    book.set_title("Narrative Notes Fixture")
    book.set_language("en")
    book.add_author("Fixture Author")

    chapter_one = epub.EpubHtml(title="Chapter One", file_name="chapter1.xhtml", lang="en")
    chapter_one.content = """
    <html><body>
      <h1>Chapter One</h1>
      <p>First chapter body text long enough to be treated as a readable
      section by the parser and its length threshold.</p>
      <p>Additional prose keeps the chapter above the minimum.</p>
    </body></html>
    """

    notes = epub.EpubHtml(title="Notes on a Winter Journey", file_name="notes.xhtml", lang="en")
    notes.content = """
    <html><body>
      <h1>Notes on a Winter Journey</h1>
      <p>This narrative chapter happens to live in notes.xhtml but is real
      story content that must survive section filtering.</p>
      <p>More narrative text keeps this section above the minimum length.</p>
    </body></html>
    """

    for document in [chapter_one, notes]:
        book.add_item(document)
    book.toc = (
        epub.Link("chapter1.xhtml", "Chapter One", "chapter-one"),
        epub.Link("notes.xhtml", "Notes on a Winter Journey", "winter-notes"),
    )
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", chapter_one, notes]

    sample_epub = tmp_path / "narrative-notes.epub"
    epub.write_epub(str(sample_epub), book)

    chapters = EPUBParser(sample_epub).extract_chapters()

    assert [chapter.title for chapter in chapters] == [
        "Chapter One",
        "Notes on a Winter Journey",
    ]
    assert "story content that must survive section filtering" in chapters[1].content


def test_epub_chapter_decode_honors_declared_encoding(tmp_path: Path) -> None:
    from modules.ingestion.epub_sections import _EPUBSectionBuilder

    class _Item:
        def __init__(self, content: bytes) -> None:
            self._content = content

        def get_content(self) -> bytes:
            return self._content

    shift_jis_markup = (
        '<?xml version="1.0" encoding="Shift_JIS"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<body><p>吾輩は猫である。名前はまだ無い。</p></body></html>"
    )
    decoded = _EPUBSectionBuilder._decode_item(_Item(shift_jis_markup.encode("shift_jis")))
    assert "吾輩は猫である。名前はまだ無い。" in decoded

    meta_charset_markup = (
        "<html><head><meta charset=\"gbk\"/></head>"
        "<body><p>你好，世界。这是一章。</p></body></html>"
    )
    decoded = _EPUBSectionBuilder._decode_item(_Item(meta_charset_markup.encode("gbk")))
    assert "你好，世界。这是一章。" in decoded

    utf16_markup = (
        '<?xml version="1.0" encoding="utf-16"?>\n'
        "<html><body><p>UTF-16 chapter body text.</p></body></html>"
    )
    decoded = _EPUBSectionBuilder._decode_item(_Item(utf16_markup.encode("utf-16")))
    assert "UTF-16 chapter body text." in decoded


def test_epub_parser():
    """Run all EPUB parser tests."""
    
    print("\n" + "="*50)
    print("EPUB PARSER TEST SUITE")
    print("="*50 + "\n")

    with tempfile.TemporaryDirectory(prefix="orpheus-epub-parser-") as temp_dir:
        sample_epub = Path(temp_dir) / "sample.epub"

        # Create sample EPUB in isolated temp output (never tracked fixtures).
        print("[1] Creating sample EPUB...")
        create_sample_epub(sample_epub)
        return _run_epub_parser_checks(sample_epub)


def _run_epub_parser_checks(sample_epub: Path) -> bool:
    
    # Test 1: File validation
    print("\n[2] Testing file validation...")
    try:
        parser = EPUBParser(sample_epub)
        print("✓ EPUBParser initialized successfully")
    except Exception as e:
        print(f"✗ Failed to initialize parser: {e}")
        return False
    
    # Test 2: Invalid file handling
    print("\n[3] Testing invalid file handling...")
    try:
        EPUBParser("/nonexistent/file.epub")
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
        print(f"  - Language: {doc.language}")
        print(f"  - Publisher: {doc.publisher}")
        print(f"  - Chapters: {len(doc.chapters)}")
    except Exception as e:
        print(f"✗ Failed to parse document: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test 4: Metadata extraction
    print("\n[5] Testing metadata extraction...")
    try:
        metadata = parser.get_metadata()
        assert metadata.get("title") == "Sample Audiobook EPUB", f"Title mismatch: {metadata.get('title')}"
        assert metadata.get("author") == "Test Author", f"Author mismatch: {metadata.get('author')}"
        print("✓ Metadata extracted correctly")
        for key, value in metadata.items():
            print(f"  - {key}: {value}")
    except Exception as e:
        print(f"✗ Metadata extraction failed: {e}")
        return False
    
    # Test 5: Chapter extraction
    print("\n[6] Testing chapter extraction...")
    try:
        chapters = parser.extract_chapters()
        assert len(chapters) >= 3, f"Expected at least 3 chapters, got {len(chapters)}"
        print(f"✓ Extracted {len(chapters)} chapters")
        for ch in chapters[:5]:  # Show first 5
            content_preview = ch.content[:50].replace("\n", " ")
            print(f"  - Chapter {ch.number}: {ch.title}")
            print(f"    Content preview: {content_preview}...")
    except Exception as e:
        print(f"✗ Chapter extraction failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test 6: Content cleaning
    print("\n[7] Testing content cleaning...")
    try:
        first_chapter = chapters[0]
        assert "<html>" not in first_chapter.content, "HTML tags should be stripped"
        assert "<p>" not in first_chapter.content, "HTML tags should be stripped"
        assert len(first_chapter.content) > 50, "Content should not be empty"
        print("✓ Content properly cleaned (no HTML tags)")
        print(f"  - Content length: {len(first_chapter.content)} chars")
    except Exception as e:
        print(f"✗ Content cleaning failed: {e}")
        return False
    
    # Test 7: HTML content preserved
    print("\n[8] Testing HTML content preservation...")
    try:
        first_chapter = chapters[0]
        assert len(first_chapter.html_content) > 0, "HTML content should be preserved"
        assert "<html>" in first_chapter.html_content or "<h1>" in first_chapter.html_content
        print("✓ Original HTML content preserved")
        print(f"  - HTML length: {len(first_chapter.html_content)} chars")
    except Exception as e:
        print(f"✗ HTML preservation failed: {e}")
        return False

    # Test 8: XHTML parsing should not emit XML-as-HTML warnings
    print("\n[9] Testing XHTML parser warning handling...")
    try:
        xhtml = """
        <?xml version="1.0" encoding="utf-8"?>
        <html xmlns="http://www.w3.org/1999/xhtml">
          <head><title>XHTML Chapter</title></head>
          <body><h1>XHTML Chapter</h1><p>Body text.</p></body>
        </html>
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            text = parser._soup_to_text(parser._parse_markup(xhtml))
            title = parser._extract_title_from_soup(parser._parse_markup(xhtml))

        assert "Body text." in text
        assert title == "XHTML Chapter"
        xml_warnings = [
            warning for warning in caught
            if issubclass(warning.category, XMLParsedAsHTMLWarning)
        ]
        assert not xml_warnings, "XHTML should not trigger XMLParsedAsHTMLWarning"
        print("✓ XHTML parsed without XMLParsedAsHTMLWarning")
    except Exception as e:
        print(f"✗ XHTML parser warning handling failed: {e}")
        return False
    
    # Cleanup
    print("\n" + "="*50)
    print("ALL TESTS PASSED ✓")
    print("="*50 + "\n")
    
    return True


def test_epub_extract_chapters_parses_each_document_item_once(tmp_path):
    sample_epub = tmp_path / "sample.epub"
    create_sample_epub(sample_epub)
    parser = EPUBParser(sample_epub)
    parser._load_book()

    parse_calls = 0
    original_parse_markup = parser._parse_markup

    def counted_parse_markup(markup):  # noqa: ANN001
        nonlocal parse_calls
        parse_calls += 1
        return original_parse_markup(markup)

    parser._parse_markup = counted_parse_markup  # type: ignore[method-assign]

    chapters = parser.extract_chapters()

    assert len(chapters) >= 3
    assert parse_calls == len(chapters) + 1  # includes the short nav document


def test_epub_extract_chapters_reapplies_options_after_cached_parse(tmp_path, monkeypatch):
    sample_epub = tmp_path / "option-cache.epub"
    sample_epub.write_bytes(b"fixture")
    parser = EPUBParser(sample_epub, validate=False)
    sections = [
        EPUBSection(
            number=1,
            title="Empty",
            content="   ",
            html_content="<p>   </p>",
            source_name="empty.xhtml",
        ),
        EPUBSection(
            number=2,
            title="Readable",
            content="one two three",
            html_content="<p>one two three</p>",
            source_name="readable.xhtml",
        )
    ]

    monkeypatch.setattr(parser, "_load_book", lambda: object())
    monkeypatch.setattr("modules.ingestion.epub_parser.build_epub_sections", lambda *args, **kwargs: sections)

    assert len(parser.extract_chapters(max_chapter_words=10)) == 1

    with pytest.raises(EmptyChapterError):
        parser.extract_chapters(skip_empty=False)

    with pytest.raises(ChapterTooLongError):
        parser.extract_chapters(max_chapter_words=2)


if __name__ == "__main__":
    success = test_epub_parser()
    sys.exit(0 if success else 1)
