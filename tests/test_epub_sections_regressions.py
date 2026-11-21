"""Regression coverage for epub sections."""

from ebooklib import epub
import pytest

from modules.ingestion.epub_sections import build_epub_sections


def test_epub_heading_fallback_preserves_unsplit_spine_documents():
    book = epub.EpubBook()
    pages = [
        ("opening", "Prelude", "<h1>Prelude</h1><p>" + "The opening begins here. " * 8 + "</p>"),
        (
            "stories",
            "Stories",
            "<h1>First</h1><p>" + "The first story unfolds. " * 8 + "</p>"
            "<h1>Second</h1><p>" + "The second story unfolds. " * 8 + "</p>",
        ),
        ("ending", "Afterword", "<h1>Afterword</h1><p>" + "The ending follows here. " * 8 + "</p>"),
    ]
    for item_id, title, markup in pages:
        page = epub.EpubHtml(uid=item_id, title=title, file_name=f"{item_id}.xhtml")
        page.content = markup
        book.add_item(page)
    book.spine = [(item_id, "yes") for item_id, _, _ in pages]
    book.toc = []

    sections = build_epub_sections(book)

    assert [section.title for section in sections] == ["Prelude", "First", "Second", "Afterword"]
    joined = "\n".join(section.content for section in sections)
    for sentence in (
        "The opening begins here.",
        "The first story unfolds.",
        "The second story unfolds.",
        "The ending follows here.",
    ):
        assert joined.count(sentence) == 8


@pytest.mark.parametrize("use_toc", [False, True])
@pytest.mark.parametrize("story_count, notes_position", [(1, 0), (1, 1), (2, 0), (2, 1), (2, 2)])
def test_epub_structural_fragment_is_a_boundary_between_narrative_sections(
    use_toc, story_count, notes_position,
):
    book = epub.EpubBook()
    narratives = [
        ("first", "First Story", "The first story unfolds. " * 8),
        ("second", "Second Story", "The second story unfolds. " * 8),
    ][:story_count]
    sections = list(narratives)
    sections.insert(notes_position, ("notes", "Notes", "These are explanatory footnotes. " * 8))
    page = epub.EpubHtml(uid="stories", title="Stories", file_name="stories.xhtml")
    page.content = "".join(
        f'<h1 id="{anchor}">{title}</h1><p>{content}</p>' for anchor, title, content in sections
    )
    book.add_item(page)
    book.spine = [("stories", "yes")]
    book.toc = (
        [epub.Link(f"stories.xhtml#{anchor}", title, anchor) for anchor, title, _ in sections]
        if use_toc else []
    )

    extracted = build_epub_sections(book)

    assert [section.title for section in extracted] == [title for _, title, _ in narratives]
    assert all("explanatory footnotes" not in section.content for section in extracted)
    for extracted_section, (_, title, content) in zip(extracted, narratives):
        assert extracted_section.content == title + "\n\n" + content.strip()


@pytest.mark.parametrize("exclusion_kind", ["source", "fragment"])
@pytest.mark.parametrize("story_count", [1, 2])
def test_epub_heading_fallback_respects_structural_only_toc(exclusion_kind, story_count):
    book = epub.EpubBook()
    legal = '<h1 id="legal">Publisher Statement</h1><p>' + "Copyright metadata. " * 8 + "</p>"
    stories = [
        ("First Story", "The first story unfolds. " * 8),
        ("Second Story", "The second story unfolds. " * 8),
    ][:story_count]
    body = "".join(f"<h1>{title}</h1><p>{text}</p>" for title, text in stories)
    if exclusion_kind == "source":
        legal_page = epub.EpubHtml(uid="legal", title="Publisher Statement", file_name="legal.xhtml")
        legal_page.content = legal
        book.add_item(legal_page)
        book.toc = [epub.Link("legal.xhtml", "Copyright", "legal")]
        book.spine = [("legal", "yes"), ("stories", "yes")]
    else:
        body = legal + body
        book.toc = [epub.Link("stories.xhtml#legal", "Copyright", "legal")]
        book.spine = [("stories", "yes")]
    page = epub.EpubHtml(uid="stories", title="Stories", file_name="stories.xhtml")
    page.content = body
    book.add_item(page)

    extracted = build_epub_sections(book)

    assert [section.title for section in extracted] == [title for title, _ in stories]
    assert all("Copyright metadata" not in section.content for section in extracted)
    for section, (_, content) in zip(extracted, stories):
        assert content.strip() in section.content


@pytest.mark.parametrize("tag", ["a", "span"])
def test_03_epub_fragment_anchors_survive_text_extraction(tag):
    book = epub.EpubBook()
    page = epub.EpubHtml(title="Stories", file_name="stories.xhtml")
    page.content = (
        f'<p><{tag} id="one"></{tag}>The first story begins. ' + "First story prose. " * 12 + "</p>"
        f'<p><{tag} id="two"></{tag}>The second story begins. ' + "Second story prose. " * 12 + "</p>"
    )
    book.add_item(page)
    book.spine = [page]
    book.toc = [epub.Link("stories.xhtml#one", "First", "one"), epub.Link("stories.xhtml#two", "Second", "two")]
    sections = build_epub_sections(book)
    assert [section.title for section in sections] == ["First", "Second"]
    assert "second story" not in sections[0].content
    assert "first story" not in sections[1].content


def _single_page_book(markup, toc, *, title="Stories"):
    book = epub.EpubBook()
    page = epub.EpubHtml(uid="page", title=title, file_name="page.xhtml")
    page.content = markup
    book.add_item(page)
    book.spine = [("page", "yes")]
    book.toc = toc
    return book


def _assert_each_sentence_once(sections, sentences):
    joined = "\n".join(section.content for section in sections)
    for sentence in sentences:
        assert joined.count(sentence) == 8, sentence


@pytest.mark.parametrize("wrap_sections", [False, True])
def test_epub_nested_toc_keeps_parent_chapter_opening_before_fragments(wrap_sections):
    fragments = (
        '<h2 id="s1">Section A</h2><p>' + "Section A carries on. " * 8 + "</p>"
        '<h2 id="s2">Section B</h2><p>' + "Section B wraps up. " * 8 + "</p>"
    )
    if wrap_sections:
        fragments = f"<div>{fragments}</div>"
    markup = "<h1>Chapter 1</h1><p>" + "The chapter opening sets the scene. " * 8 + "</p>" + fragments
    book = _single_page_book(markup, [
        epub.Link("page.xhtml", "Chapter 1", "ch1"),
        epub.Link("page.xhtml#s1", "Section A", "s1"),
        epub.Link("page.xhtml#s2", "Section B", "s2"),
    ])

    sections = build_epub_sections(book)

    assert [section.title for section in sections] == ["Chapter 1", "Section A", "Section B"]
    _assert_each_sentence_once(sections, [
        "The chapter opening sets the scene.", "Section A carries on.", "Section B wraps up.",
    ])


def test_epub_toc_title_split_keeps_intro_before_first_matched_heading():
    markup = (
        "<p>" + "The part introduction explains things. " * 8 + "</p>"
        "<h2>Alpha</h2><p>" + "Alpha story prose. " * 8 + "</p>"
        "<h2>Beta</h2><p>" + "Beta story prose. " * 8 + "</p>"
    )
    book = _single_page_book(markup, [
        epub.Link("page.xhtml", "Part Intro", "intro"),
        epub.Link("page.xhtml", "Alpha", "alpha"),
        epub.Link("page.xhtml", "Beta", "beta"),
    ])

    sections = build_epub_sections(book)

    assert [section.title for section in sections] == ["Part Intro", "Alpha", "Beta"]
    _assert_each_sentence_once(sections, [
        "The part introduction explains things.", "Alpha story prose.", "Beta story prose.",
    ])


def test_epub_heading_fallback_does_not_narrate_an_untitled_preamble():
    # Nothing names the text before the first heading; in practice it is a
    # title page, dedication or blurb far more often than prose.
    markup = (
        "<p>" + "A preamble precedes the stories. " * 8 + "</p>"
        "<h1>First</h1><p>" + "The first story unfolds. " * 8 + "</p>"
        "<h1>Second</h1><p>" + "The second story unfolds. " * 8 + "</p>"
    )
    book = _single_page_book(markup, [])

    sections = build_epub_sections(book)

    assert [section.title for section in sections] == ["First", "Second"]
    assert sections[0].content.startswith("First")
    assert "A preamble precedes" not in "\n".join(section.content for section in sections)
    _assert_each_sentence_once(sections, ["The first story unfolds.", "The second story unfolds."])


def test_epub_toc_listing_a_document_twice_narrates_it_once():
    book = epub.EpubBook()
    pages = [
        ("copyright", "Copyright", "<h1>Copyright</h1><p>" + "All rights reserved. " * 8 + "</p>"),
        ("ch1", "Chapter 1", "<h1>Chapter 1</h1><p>" + "The first chapter unfolds. " * 8 + "</p>"),
        ("ch2", "Chapter 2", "<h1>Chapter 2</h1><p>" + "The second chapter unfolds. " * 8 + "</p>"),
    ]
    for item_id, title, markup in pages:
        page = epub.EpubHtml(uid=item_id, title=title, file_name=f"{item_id}.xhtml")
        page.content = markup
        book.add_item(page)
    book.spine = [(item_id, "yes") for item_id, _, _ in pages]
    book.toc = [
        epub.Link("ch1.xhtml", "Begin Reading", "begin"),
        epub.Link("copyright.xhtml", "Copyright", "copyright"),
        epub.Link("ch1.xhtml", "Chapter 1", "ch1"),
        epub.Link("ch2.xhtml", "Chapter 2", "ch2"),
    ]

    sections = build_epub_sections(book)

    assert len(sections) == 2
    _assert_each_sentence_once(sections, ["The first chapter unfolds.", "The second chapter unfolds."])


_FRONT_MATTER = (
    "<h1>The Lighthouse Keeper</h1><p>A Novel</p><p>by Jane Doe</p>"
    "<p>Copyright © 2021 by Jane Doe. All rights reserved. No part of this publication may be "
    "reproduced without the prior written permission of the publisher. ISBN 978-0-00-000000-0.</p>"
    "<h2>Contents</h2><p>Chapter 1</p><p>Chapter 2</p>"
)


def _two_chapters(tag="h2", ids=True):
    def chapter(n, sentence):
        anchor = f' id="ch{n}"' if ids else ""
        return f"<{tag}{anchor}>Chapter {n}</{tag}><p>" + sentence * 8 + "</p>"

    return chapter(1, "The keeper climbed the stairs at dusk. ") + chapter(2, "Storms rolled in from the west. ")


@pytest.mark.parametrize(
    ("markup", "toc"),
    [
        pytest.param(
            _FRONT_MATTER + _two_chapters(),
            [epub.Link("page.xhtml#ch1", "Chapter 1", "a"), epub.Link("page.xhtml#ch2", "Chapter 2", "b")],
            id="fragment-toc",
        ),
        pytest.param(
            _FRONT_MATTER + _two_chapters(ids=False),
            [epub.Link("page.xhtml", "Chapter 1", "a"), epub.Link("page.xhtml", "Chapter 2", "b")],
            id="title-toc",
        ),
        pytest.param(
            _FRONT_MATTER.replace("h1", "p").replace("h2", "p") + _two_chapters(tag="h1"),
            [],
            id="heading-fallback",
        ),
    ],
)
def test_epub_single_file_front_matter_before_the_first_chapter_is_not_narrated(markup, toc):
    sections = build_epub_sections(_single_page_book(markup, toc))

    assert [section.title for section in sections] == ["Chapter 1", "Chapter 2"]
    assert sections[0].content.startswith("Chapter 1")
    joined = "\n".join(section.content for section in sections)
    for front_matter in ("Lighthouse Keeper", "Copyright", "Contents"):
        assert front_matter not in joined
    _assert_each_sentence_once(sections, ["The keeper climbed the stairs at dusk.", "Storms rolled in from the west."])


def test_epub_title_page_without_copyright_or_contents_is_not_narrated():
    markup = "<p>The Lighthouse Keeper</p><p>A Novel</p><p>by Jane Doe, with drawings by the author</p>"
    sections = build_epub_sections(_single_page_book(markup + _two_chapters(tag="h1"), []))

    assert sections[0].content.startswith("Chapter 1")


def test_epub_navigation_after_the_chapter_wrapper_is_not_narrated():
    markup = (
        '<div class="chapter">' + _two_chapters(tag="h1") + "</div>"
        '<div class="nav"><p>Previous | Next | Back to the start of the book</p></div>'
    )

    sections = build_epub_sections(_single_page_book(markup, []))

    assert [section.title for section in sections] == ["Chapter 1", "Chapter 2"]
    assert "Previous" not in sections[-1].content
    _assert_each_sentence_once(sections, ["The keeper climbed the stairs at dusk.", "Storms rolled in from the west."])


def test_epub_toc_begin_reading_alias_does_not_rename_the_chapter():
    book = epub.EpubBook()
    pages = [
        ("copyright", "Copyright", "<h1>Copyright</h1><p>" + "All rights reserved. " * 8 + "</p>"),
        ("ch1", "Chapter 1", "<h1>Chapter 1</h1><p>" + "The first chapter unfolds. " * 8 + "</p>"),
        ("ch2", "Chapter 2", "<h1>Chapter 2</h1><p>" + "The second chapter unfolds. " * 8 + "</p>"),
    ]
    for item_id, title, markup in pages:
        page = epub.EpubHtml(uid=item_id, title=title, file_name=f"{item_id}.xhtml")
        page.content = markup
        book.add_item(page)
    book.spine = [(item_id, "yes") for item_id, _, _ in pages]
    book.toc = [
        epub.Link("ch1.xhtml", "Begin Reading", "begin"),
        epub.Link("copyright.xhtml", "Copyright", "copyright"),
        epub.Link("ch1.xhtml", "Chapter 1", "ch1"),
        epub.Link("ch2.xhtml", "Chapter 2", "ch2"),
    ]

    sections = build_epub_sections(book)

    assert [section.title for section in sections] == ["Chapter 1", "Chapter 2"]


def test_epub_nested_toc_opening_is_named_by_the_parent_chapter_not_an_alias():
    markup = (
        "<h1>Chapter 1</h1><p>" + "The chapter opening sets the scene. " * 8 + "</p>"
        '<h2 id="s1">Section A</h2><p>' + "Section A carries on. " * 8 + "</p>"
        '<h2 id="s2">Section B</h2><p>' + "Section B wraps up. " * 8 + "</p>"
    )
    book = _single_page_book(markup, [
        epub.Link("page.xhtml", "Begin Reading", "begin"),
        epub.Link("page.xhtml", "Chapter 1", "ch1"),
        epub.Link("page.xhtml#s1", "Section A", "s1"),
        epub.Link("page.xhtml#s2", "Section B", "s2"),
    ])

    sections = build_epub_sections(book)

    assert [section.title for section in sections] == ["Chapter 1", "Section A", "Section B"]
