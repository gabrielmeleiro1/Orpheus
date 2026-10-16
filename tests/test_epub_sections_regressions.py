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
