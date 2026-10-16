"""EPUB extraction equivalence, input preservation, and recursion regressions."""

from __future__ import annotations

from copy import deepcopy
import sys

from bs4 import BeautifulSoup
from bs4.element import (
    CData,
    Comment,
    Declaration,
    Doctype,
    NavigableString,
    PreformattedString,
    ProcessingInstruction,
    RubyParenthesisString,
    RubyTextString,
    Script,
    Stylesheet,
    Tag,
    TemplateString,
    XMLProcessingInstruction,
)
from ebooklib import epub
from hypothesis import given, settings, strategies as st
import pytest

from modules.ingestion.epub_parser import EPUBParser
from modules.ingestion.epub_sections import parse_epub_markup, soup_to_epub_text
from modules.ingestion.text_layout import repair_text_layout


def _legacy_text(soup: Tag) -> str:
    """Frozen pre-change behavior, restricted to small trees in equivalence tests."""
    copied = deepcopy(soup)
    for element in copied(["script", "style", "nav", "header", "footer"]):
        element.decompose()
    for element in copied(["a", "span", "b", "strong", "i", "em", "small", "sup", "sub"]):
        element.unwrap()
    copied.smooth()
    text = repair_text_layout(copied.get_text(separator="\n", strip=True))
    return "\n\n".join(line.strip() for line in text.split("\n") if line.strip())


def _dom_state(root: Tag) -> tuple:
    """Snapshot identities and links iteratively; serializing a deep Tag recurses."""
    result = []
    pending = [root]
    while pending:
        node = pending.pop()
        links = tuple(id(getattr(node, name, None)) for name in (
            "parent", "previous_element", "next_element", "previous_sibling", "next_sibling",
        ))
        if isinstance(node, Tag):
            value = (node.name, node.namespace, node.prefix, repr(node.attrs),
                     repr(node.interesting_string_types), node.hidden,
                     tuple(id(child) for child in node.contents))
            pending.extend(reversed(node.contents))
        else:
            value = str(node)
        result.append((id(node), type(node), value, links))
    return tuple(result)


@pytest.mark.parametrize("parser", ["html.parser", "lxml", "xml"])
@pytest.mark.parametrize("markup", [
    "<p>The answer is <em>42</em> which is exact.</p>",
    "<p>word<b> part<i> within </i>end</b> tail.</p>",
    "<p>pre<span></span>fix <nav><a>discarded</a></nav> tail.</p>",
    "<p>first<br/>second<hr/>third<unknown/>fourth.</p>",
    "<p>one<!-- omitted -->two<![CDATA[ raw data ]]>three.</p>",
    "<p>before<span>first<div>block</div>last</span>after.</p>",
    "<head><title>Title stays</title><style>drop</style></head><body><h1>Heading</h1><p>Body.</p></body>",
    "<p>hyphen-<br/>ated word and non-<br/>fiction.</p>",
    "<p>before<header>remove<script>nested</script></header>after<footer>drop</footer></p>",
    "<template>unrendered text</template><p>Visible <small>small</small> text.</p>",
    "<p xml:lang='en'>Space&#160;<sup>2</sup> stays &amp; entities.</p>",
])
def test_extraction_matches_legacy_markup_and_preserves_dom(parser, markup):
    soup = BeautifulSoup(f"<root>{markup}</root>", parser)
    before = _dom_state(soup)
    expected = _legacy_text(soup)
    assert soup_to_epub_text(soup) == expected
    assert _dom_state(soup) == before


def test_eighty_navigation_links_do_not_require_copying_the_dom():
    links = "".join(f'<li><a href="ch{index}.xhtml">Chapter {index}</a></li>' for index in range(80))
    soup = parse_epub_markup(
        '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
        f'<nav><ol>{links}</ol></nav><p>Readable <em>body</em> remains.</p></body></html>'
    )
    before = _dom_state(soup)
    assert soup_to_epub_text(soup) == "Readable body remains."
    assert _dom_state(soup) == before


@pytest.mark.parametrize("tag_name", ["div", "span"])
def test_deep_retained_structure_does_not_depend_on_python_recursion(tag_name):
    soup = BeautifulSoup("", "html.parser")
    root = parent = soup.new_tag("root")
    soup.append(root)
    depth = sys.getrecursionlimit() + 20
    for _ in range(depth):
        child = soup.new_tag(tag_name)
        parent.append(child)
        parent = child
    parent.append(NavigableString("Deeply nested prose survives."))
    before = _dom_state(soup)
    assert soup_to_epub_text(soup) == "Deeply nested prose survives."
    assert _dom_state(soup) == before


def test_heading_extraction_does_not_copy_ancestors_or_neighboring_navigation():
    soup = BeautifulSoup("<main><h1>Chapter <em>one</em></h1><nav></nav></main>", "html.parser")
    for index in range(100):
        item = soup.new_tag("a", href=f"#{index}")
        item.append(NavigableString(f"Navigation {index}"))
        soup.nav.append(item)
    before = _dom_state(soup)
    assert soup_to_epub_text(soup.h1) == "Chapter one"
    assert _dom_state(soup) == before


@pytest.mark.parametrize("name", ["nav", "header", "footer"])
def test_removed_tag_passed_as_root_keeps_its_own_text(name):
    soup = BeautifulSoup(f"<{name}>Root <em>content</em><nav>nested discarded</nav></{name}>", "html.parser")
    root = soup.find(name)
    before = _dom_state(soup)
    assert soup_to_epub_text(root) == _legacy_text(root) == "Root content"
    assert _dom_state(soup) == before


@pytest.mark.parametrize("accepted", [(NavigableString, CData), None, Comment, (TemplateString,)])
def test_string_class_and_preformatted_barriers_match_smoothing(accepted):
    soup = BeautifulSoup("<root></root>", "html.parser")
    soup.interesting_string_types = accepted
    for node in (
        NavigableString("before "), TemplateString("joined"), Comment(" hidden "),
        NavigableString(" after "), CData(" raw "), NavigableString(" final "),
        ProcessingInstruction("ignored"), Script("script subclass"), NavigableString(""),
    ):
        soup.root.append(node)
    before = _dom_state(soup)
    assert soup_to_epub_text(soup) == _legacy_text(soup)
    assert _dom_state(soup) == before


@pytest.mark.parametrize("kind, expected", [
    ("inline", "leftright"),
    ("removed", "leftright"),
    ("retained", "left\n\nright"),
    ("comment", "left\n\nright"),
    ("cdata", "left\n\nright"),
    ("preformatted", "left\n\nright"),
    ("instruction", "left\n\nright"),
    ("ordinary", "leftright"),
    ("whitespace", "left right"),
])
def test_empty_nodes_keep_distinct_string_run_boundaries(kind, expected):
    soup = BeautifulSoup("<root></root>", "html.parser")
    root = soup.root
    root.append(NavigableString("left"))
    if kind in {"inline", "removed", "retained"}:
        middle = soup.new_tag({"inline": "span", "removed": "nav", "retained": "br"}[kind])
        if kind == "removed":
            nested = soup.new_tag("p")
            nested.append(NavigableString("discarded"))
            middle.append(nested)
    else:
        string_type = {
            "comment": Comment, "cdata": CData, "preformatted": PreformattedString,
            "instruction": ProcessingInstruction, "ordinary": NavigableString,
            "whitespace": NavigableString,
        }[kind]
        middle = string_type(" " if kind == "whitespace" else "")
    root.append(middle)
    root.append(NavigableString("right"))
    before = _dom_state(soup)
    assert soup_to_epub_text(root) == _legacy_text(root) == expected
    assert _dom_state(soup) == before


@pytest.mark.parametrize("append_empty, accepted, expected", [
    (False, TemplateString, "payload"),
    (True, TemplateString, ""),
    (False, (NavigableString, CData), ""),
    (True, (NavigableString, CData), "payload"),
])
def test_empty_ordinary_string_can_change_subtype_filtering(append_empty, accepted, expected):
    soup = BeautifulSoup("<root></root>", "html.parser")
    root = soup.root
    root.interesting_string_types = accepted
    root.append(TemplateString("payload"))
    if append_empty:
        root.append(NavigableString(""))
    before = _dom_state(soup)
    assert soup_to_epub_text(root) == _legacy_text(root) == expected
    assert _dom_state(soup) == before


@pytest.mark.parametrize("name, string_type", [
    ("script", Script), ("style", Stylesheet), ("template", TemplateString),
    ("rt", RubyTextString), ("rp", RubyParenthesisString),
])
def test_special_container_root_uses_its_own_string_filter(name, string_type):
    soup = BeautifulSoup(f"<{name}>kept</{name}>", "html.parser")
    root = soup.find(name)
    assert type(root.contents[0]) is string_type
    before = _dom_state(soup)
    assert soup_to_epub_text(root) == _legacy_text(root) == "kept"
    assert soup_to_epub_text(soup) == _legacy_text(soup) == ""
    assert _dom_state(soup) == before


def test_descendant_filters_do_not_override_the_requested_root_filter():
    soup = BeautifulSoup("<root><p>visible<!-- hidden --></p></root>", "html.parser")
    soup.p.interesting_string_types = Comment
    before = _dom_state(soup)
    assert soup_to_epub_text(soup.root) == _legacy_text(soup.root) == "visible"
    assert soup_to_epub_text(soup.p) == _legacy_text(soup.p) == "hidden"
    assert _dom_state(soup) == before


def test_xml_names_match_by_local_name_without_case_folding():
    soup = BeautifulSoup(
        '<root xmlns:x="urn:test"><p>left<x:em>middle</x:em>right'
        '<x:nav><p>discarded</p></x:nav>tail<NAV>kept</NAV>end</p></root>',
        "xml",
    )
    before = _dom_state(soup)
    expected = "leftmiddlerighttail\n\nkept\n\nend"
    assert soup_to_epub_text(soup) == _legacy_text(soup) == expected
    assert _dom_state(soup) == before


@pytest.mark.parametrize("tag_root", [False, True])
def test_empty_document_or_tag_root_yields_no_text(tag_root):
    soup = BeautifulSoup("<root></root>" if tag_root else "", "html.parser")
    root = soup.root if tag_root else soup
    before = _dom_state(soup)
    assert soup_to_epub_text(root) == _legacy_text(root) == ""
    assert _dom_state(soup) == before


def test_complete_epub_with_eighty_navigation_entries_is_readable(tmp_path):
    book = epub.EpubBook()
    book.set_identifier("epub-text-recursion-regression")
    book.set_title("Navigation regression")
    book.set_language("en")
    chapters = []
    for index in range(80):
        chapter = epub.EpubHtml(title=f"Chapter {index + 1}", file_name=f"chapter-{index + 1}.xhtml", lang="en")
        chapter.content = (
            f"<h1>Chapter {index + 1}</h1><p>The traveler reached the station. "
            "She read the timetable carefully and waited for the morning train.</p>"
        )
        book.add_item(chapter)
        chapters.append(chapter)
    book.toc = chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *chapters]
    path = tmp_path / "eighty-chapters.epub"
    epub.write_epub(str(path), book)
    result = EPUBParser(path).parse()
    assert len(result.chapters) == 80
    assert [chapter.title for chapter in result.chapters] == [f"Chapter {index + 1}" for index in range(80)]
    assert all("morning train" in chapter.content for chapter in result.chapters)


_STRING_TYPES = (
    NavigableString, Comment, CData, TemplateString, Script, Stylesheet,
    RubyTextString, RubyParenthesisString, PreformattedString,
    ProcessingInstruction, XMLProcessingInstruction, Declaration, Doctype,
)
_text = st.text(alphabet="abc ABC012\n\t\u00a0-.,!?", max_size=16)
_leaf = st.tuples(st.sampled_from(_STRING_TYPES), _text)
_node = _leaf
for _ in range(3):
    _node = st.one_of(_leaf, st.tuples(
        st.sampled_from(("p", "div", "br", "em", "span", "a", "nav", "style", "footer", "template")),
        st.lists(_node, max_size=2),
    ))


def _append_node(soup, parent, node):
    kind, value = node
    if isinstance(kind, str):
        child = soup.new_tag(kind)
        parent.append(child)
        for item in value:
            _append_node(soup, child, item)
    else:
        parent.append(kind(value))


@pytest.mark.property
@given(nodes=st.lists(_node, max_size=2), accepted=st.sampled_from([
    (NavigableString, CData), None, Comment, TemplateString, (),
]), tag_root=st.booleans())
@settings(max_examples=200, deadline=None, database=None)
def test_small_trees_match_legacy_text_exactly_without_mutation(nodes, accepted, tag_root):
    soup = BeautifulSoup("<root></root>", "html.parser")
    root = soup.root if tag_root else soup
    root.interesting_string_types = accepted
    for node in nodes:
        _append_node(soup, soup.root, node)
    before = _dom_state(soup)
    assert soup_to_epub_text(root) == _legacy_text(root)
    assert _dom_state(soup) == before
