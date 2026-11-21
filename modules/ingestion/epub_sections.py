"""EPUB reading-order and section detection helpers."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from itertools import takewhile
from dataclasses import dataclass
from pathlib import PurePosixPath
import codecs
import re
from typing import Any, Callable, Iterable, Optional
from urllib.parse import unquote

import ebooklib
from bs4 import BeautifulSoup
from bs4.element import NavigableString, PageElement, PreformattedString, Tag

from .text_layout import repair_text_layout


MIN_SECTION_CHARS = 50

_STRUCTURAL_TITLE_RE = re.compile(
    r"^(?:"
    r"cover|"
    r"back\s+cover|"
    r"front\s+cover|"
    r"title\s+page|"
    r"half\s+title(?:\s+page)?|"
    r"table\s+of\s+contents|"
    r"contents|"
    r"copyright(?:\s+page)?|"
    r"dedication|"
    r"epigraph|"
    r"about\s+the\s+author|"
    r"about\s+the\s+publisher|"
    r"follow\s+(?:the\s+)?penguin|"
    r"also\s+by(?:\s+.+)?|"
    r"praise(?:\s+for\s+.+)?|"
    r"permissions?|"
    r"credits?|"
    r"index|"
    r"bibliography|"
    r"notes?|"
    r"footnotes?"
    r")$",
    re.IGNORECASE,
)
_GENERIC_CONTAINER_TITLE_RE = re.compile(
    r"^(?:stories|short\s+stories|collected\s+stories|selected\s+stories|parts?)$",
    re.IGNORECASE,
)
_PART_DIVIDER_TITLE_RE = re.compile(
    r"^part\s+(?:[ivxlcdm]+|\d+)\b",
    re.IGNORECASE,
)
_XML_PROLOG_ENCODING_RE = re.compile(
    rb"<\?xml[^>]*?\bencoding=[\"']([A-Za-z0-9._-]+)[\"']"
)
_META_CHARSET_RE = re.compile(
    rb"<meta[^>]*?\bcharset\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)


def _declared_markup_encoding(content: bytes) -> Optional[str]:
    """Sniff the encoding declared in an XML prolog or meta charset tag."""
    head = content[:1024]
    match = _XML_PROLOG_ENCODING_RE.search(head) or _META_CHARSET_RE.search(head)
    if match is None:
        return None
    return match.group(1).decode("ascii")


@dataclass
class EPUBSection:
    """A readable EPUB section suitable for one audiobook chapter."""

    number: int
    title: str
    content: str
    html_content: str
    source_name: str


@dataclass
class _DocumentPart:
    """Parsed XHTML document from the EPUB spine."""

    item_id: str
    source_name: str
    title: Optional[str]
    html_content: str
    text_content: str
    soup: BeautifulSoup
    order: int
    in_spine: bool


@dataclass(frozen=True)
class _TOCEntry:
    title: str
    href: str
    source_name: str
    fragment: Optional[str]
    order: int


def parse_epub_markup(markup: str) -> BeautifulSoup:
    """Parse EPUB XHTML/XML without triggering XML-as-HTML warnings."""
    if is_xhtml_document(markup):
        try:
            return BeautifulSoup(markup, "xml")
        except Exception:
            return BeautifulSoup(markup, "lxml")
    return BeautifulSoup(markup, "lxml")


def is_xhtml_document(markup: str) -> bool:
    """Heuristic for XHTML/XML chapter documents embedded in EPUB files."""
    head = (markup or "").lstrip()[:1024].lower()
    if head.startswith("<?xml"):
        return True
    return "<html" in head and "xmlns=" in head


def extract_title_from_soup(soup: BeautifulSoup) -> Optional[str]:
    """Extract the first useful document title from parsed markup."""
    for tag in ["h1", "h2", "h3"]:
        heading = soup.find(tag)
        if heading:
            title = heading.get_text(strip=True)
            if title:
                return title

    title = soup.find("title")
    if title:
        text = title.get_text(strip=True)
        if text:
            return text

    return None


_EPUB_REMOVED_TAGS = frozenset({"script", "style", "nav", "header", "footer"})
_EPUB_INLINE_TAGS = frozenset({"a", "span", "b", "strong", "i", "em", "small", "sup", "sub"})


def _epub_text_runs(soup: Tag) -> list[str]:
    """Model removal, inline unwrapping and BeautifulSoup smoothing without edits.

    Unwrapped inline elements share their parent's adjacent-string run. Every
    retained tag, including an empty tag, remains a boundary. Preformatted
    strings (comments, CDATA and processing instructions) also remain boundaries,
    even when get_text would omit their contents. Stripping happens after a run
    is joined, so whitespace surrounding emphasis is not lost.
    """
    # get_text() applies the requested root's filter to all descendants. In the
    # pinned BeautifulSoup version, None includes every NavigableString subtype.
    accepted_types = soup.interesting_string_types
    runs: list[str] = []
    pieces: list[str] = []
    single_type: type[NavigableString] = NavigableString

    def emit(text: str, string_type: type[NavigableString]) -> None:
        if accepted_types is not None:
            if isinstance(accepted_types, type):
                if string_type is not accepted_types:
                    return
            elif string_type not in accepted_types:
                return
        stripped = text.strip()
        if stripped:
            runs.append(stripped)

    def flush() -> None:
        if pieces:
            # smooth() replaces two or more adjacent non-preformatted strings
            # with a plain NavigableString, even when they began as subclasses.
            emit("".join(pieces), single_type if len(pieces) == 1 else NavigableString)
            pieces.clear()

    # The root itself is never removed/unwrapped: BeautifulSoup.find_all searches
    # its descendants, and callers also pass individual heading Tags here.
    frames: list[tuple[Iterator[PageElement], bool]] = [(iter(soup.children), True)]
    while frames:
        children, boundary = frames[-1]
        child = next(children, None)
        if child is None:
            frames.pop()
            if boundary:
                flush()
            continue
        if isinstance(child, Tag):
            if child.name in _EPUB_REMOVED_TAGS:
                continue
            retained = child.name not in _EPUB_INLINE_TAGS
            if retained:
                flush()
            frames.append((iter(child.children), retained))
        elif isinstance(child, NavigableString):
            if isinstance(child, PreformattedString):
                flush()
                emit(str(child), type(child))
            else:
                if not pieces:
                    single_type = type(child)
                # Empty ordinary strings still participate in smooth()'s
                # promotion of adjacent subclasses to plain NavigableString.
                pieces.append(str(child))
        else:
            flush()
    return runs


def soup_to_epub_text(soup: BeautifulSoup) -> str:
    """Convert parsed EPUB markup to clean block-separated text without editing it."""
    text = "\n".join(_epub_text_runs(soup))
    text = repair_text_layout(text)
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return "\n\n".join(lines)


def build_epub_sections(
    book: Any,
    *,
    parse_markup: Callable[[str], BeautifulSoup] = parse_epub_markup,
    soup_to_text: Callable[[BeautifulSoup], str] = soup_to_epub_text,
    extract_title: Callable[[BeautifulSoup], Optional[str]] = extract_title_from_soup,
    min_section_chars: int = MIN_SECTION_CHARS,
) -> list[EPUBSection]:
    """Build readable audiobook sections from EPUB spine, TOC, and headings."""
    builder = _EPUBSectionBuilder(
        book=book,
        parse_markup=parse_markup,
        soup_to_text=soup_to_text,
        extract_title=extract_title,
        min_section_chars=min_section_chars,
    )
    return builder.build()


class _EPUBSectionBuilder:
    """Shared EPUB section-detection implementation."""

    def __init__(
        self,
        *,
        book: Any,
        parse_markup: Callable[[str], BeautifulSoup],
        soup_to_text: Callable[[BeautifulSoup], str],
        extract_title: Callable[[BeautifulSoup], Optional[str]],
        min_section_chars: int,
    ) -> None:
        self.book = book
        self.parse_markup = parse_markup
        self.soup_to_text = soup_to_text
        self.extract_title = extract_title
        self.min_section_chars = min_section_chars

    def build(self) -> list[EPUBSection]:
        documents = self._read_spine_documents()
        if not documents:
            return []

        excluded_sources, excluded_fragments = self._structural_toc_exclusions(documents)
        sections = self._sections_from_toc(documents)
        if sections:
            sections = self._merge_unclaimed_spine_documents(
                sections,
                documents,
                excluded_sources,
                excluded_fragments,
            )
            return self._renumber(sections)

        # A structural-only TOC still supplies exclusions. Recover its mixed
        # documents through the fragment-aware merge instead of treating those
        # headings as fresh narrative chapters.
        fallback_documents = [
            document for document in documents
            if document.source_name not in excluded_sources
            and document.source_name not in excluded_fragments
        ]
        sections = self._sections_from_document_headings(fallback_documents)
        if not sections:
            sections = self._fallback_document_sections(fallback_documents)
        # Splitting one XHTML document must not discard the other spine
        # documents that already represent complete chapters.
        sections = self._merge_unclaimed_spine_documents(
            sections, documents, excluded_sources, excluded_fragments,
        )
        return self._renumber(sections)

    def _read_spine_documents(self) -> list[_DocumentPart]:
        ordered_items: list[tuple[Any, bool]] = []
        seen_keys: set[str] = set()

        for spine_entry in getattr(self.book, "spine", []) or []:
            item_id = spine_entry[0] if isinstance(spine_entry, tuple) else spine_entry
            item = self.book.get_item_with_id(item_id)
            if item is None:
                continue
            key = self._item_key(item)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            ordered_items.append((item, True))

        for item in self.book.get_items():
            if item.get_type() != ebooklib.ITEM_DOCUMENT:
                continue
            key = self._item_key(item)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            ordered_items.append((item, False))

        documents: list[_DocumentPart] = []
        for order, (item, in_spine) in enumerate(ordered_items):
            if item.get_type() != ebooklib.ITEM_DOCUMENT:
                continue
            html_content = self._decode_item(item)
            soup = self.parse_markup(html_content)
            title = self.extract_title(soup) or str(getattr(item, "title", "") or "").strip() or None
            text_content = self.soup_to_text(soup)
            documents.append(
                _DocumentPart(
                    item_id=str(getattr(item, "id", "") or ""),
                    source_name=_normalize_href(item.get_name()),
                    title=title,
                    html_content=html_content,
                    text_content=text_content,
                    soup=soup,
                    order=order,
                    in_spine=in_spine,
                )
            )
        return documents

    def _sections_from_toc(self, documents: list[_DocumentPart]) -> list[EPUBSection]:
        entries = self._read_toc_entries()
        if not entries:
            return []

        resolver = _DocumentResolver(documents)
        resolved_entries: list[tuple[_TOCEntry, _DocumentPart]] = []
        for entry in entries:
            document = resolver.resolve(entry.source_name)
            if document is None:
                continue
            resolved_entries.append((entry, document))

        # A document listed again later (a "Begin Reading" entry ahead of the
        # front matter) is split once, by all its entries, at its first position.
        entries_by_document: dict[str, list[_TOCEntry]] = {}
        ordered: list[EPUBSection | _DocumentPart] = []
        for index, (entry, document) in enumerate(resolved_entries):
            if not self._has_readable_content(document):
                if _is_structural_title(entry.title, entry.source_name):
                    continue
                recovered = self._section_from_unreadable_toc_marker(
                    entry,
                    document,
                    self._next_toc_document_after(resolved_entries, index),
                    documents,
                )
                if recovered is not None:
                    ordered.append(recovered)
                continue

            # Structural entries still delimit neighboring narrative sections.
            # Keep them through splitting; _filter_sections removes their bodies.
            if document.source_name not in entries_by_document:
                entries_by_document[document.source_name] = []
                ordered.append(document)
            entries_by_document[document.source_name].append(entry)

        sections: list[EPUBSection] = []
        for item in ordered:
            if isinstance(item, EPUBSection):
                sections.append(item)
            else:
                sections.extend(
                    self._sections_from_toc_document(item, entries_by_document[item.source_name])
                )

        return self._filter_sections(sections)

    def _sections_from_toc_document(
        self,
        document: _DocumentPart,
        entries_for_document: list[_TOCEntry],
    ) -> list[EPUBSection]:
        fragment_entries = [entry for entry in entries_for_document if entry.fragment]

        if len(fragment_entries) >= 2:
            # Fragment-less entries ahead of the fragments (the parent chapter
            # of a nested TOC) name the document's opening.
            parent_entries = list(takewhile(lambda entry: not entry.fragment, entries_for_document))
            split_sections = self._split_document_by_toc_fragments(
                document, fragment_entries, _document_entry_title(document, parent_entries),
            )
            if split_sections:
                return split_sections

        if len(entries_for_document) >= 2:
            split_sections = self._split_document_by_toc_titles(document, entries_for_document)
            if split_sections:
                return split_sections

        title = _document_entry_title(document, entries_for_document) or entries_for_document[0].title
        return [self._section_from_document(document, title=title)]

    @staticmethod
    def _next_toc_document_after(
        resolved_entries: list[tuple[_TOCEntry, _DocumentPart]],
        index: int,
    ) -> Optional[_DocumentPart]:
        current_document = resolved_entries[index][1]
        for _, candidate in resolved_entries[index + 1:]:
            if candidate.order > current_document.order:
                return candidate
        return None

    def _section_from_unreadable_toc_marker(
        self,
        entry: _TOCEntry,
        document: _DocumentPart,
        next_document: Optional[_DocumentPart],
        documents: list[_DocumentPart],
    ) -> Optional[EPUBSection]:
        is_part_divider = _is_part_divider_title(entry.title)
        span_documents: list[_DocumentPart] = []
        has_readable_body = False
        for candidate in documents:
            if candidate.order <= document.order:
                continue
            if next_document is not None and candidate.order >= next_document.order:
                break
            if _is_structural_title(candidate.title, candidate.source_name):
                continue
            if is_part_divider:
                if not candidate.text_content.strip():
                    continue
            elif self._has_readable_content(candidate):
                has_readable_body = True
            elif not has_readable_body or not candidate.text_content.strip():
                continue
            span_documents.append(candidate)

        if not span_documents:
            return None

        text_content = "\n\n".join(
            part.text_content.strip()
            for part in span_documents
            if part.text_content.strip()
        )
        if is_part_divider:
            title_text = entry.title.strip()
            if title_text and title_text not in text_content:
                text_content = f"{title_text}\n\n{text_content}".strip()
        html_content = "\n".join(part.html_content for part in span_documents)
        return EPUBSection(
            number=0,
            title=entry.title,
            content=text_content,
            html_content=html_content,
            source_name=document.source_name,
        )

    def _read_toc_entries(self) -> list[_TOCEntry]:
        entries: list[_TOCEntry] = []
        toc = _normalize_toc(getattr(self.book, "toc", None))
        for order, node in enumerate(_flatten_toc_nodes(toc)):
            title = str(getattr(node, "title", "") or "").strip()
            href = str(getattr(node, "href", "") or "").strip()

            if not href and hasattr(node, "get_name"):
                href = str(node.get_name() or "").strip()
            if not title and hasattr(node, "get_name"):
                title = _title_from_href(str(node.get_name() or ""))
            if not title or not href:
                continue

            source_name, fragment = _split_href(href)
            entries.append(
                _TOCEntry(
                    title=title,
                    href=href,
                    source_name=source_name,
                    fragment=fragment,
                    order=order,
                )
            )
        return entries

    def _structural_toc_exclusions(
        self,
        documents: list[_DocumentPart],
    ) -> tuple[set[str], dict[str, set[str]]]:
        resolver = _DocumentResolver(documents)
        sources: set[str] = set()
        fragments: dict[str, set[str]] = defaultdict(set)
        for entry in self._read_toc_entries():
            if not _is_structural_title(entry.title, entry.source_name):
                continue
            document = resolver.resolve(entry.source_name)
            if document is None:
                continue
            if entry.fragment:
                fragments[document.source_name].add(entry.fragment)
            else:
                sources.add(document.source_name)
        return sources, fragments

    def _split_document_by_toc_fragments(
        self,
        document: _DocumentPart,
        entries: list[_TOCEntry],
        opening_title: Optional[str],
    ) -> list[EPUBSection]:
        markers: list[tuple[str, Tag]] = []
        for entry in entries:
            marker = _find_fragment_marker(document.soup, entry.fragment)
            if marker is None:
                return []
            markers.append((entry.title, marker))
        return self._split_document_at_markers(document, markers, opening_title=opening_title)

    def _split_document_by_toc_titles(
        self,
        document: _DocumentPart,
        entries: list[_TOCEntry],
    ) -> list[EPUBSection]:
        markers: list[tuple[str, Tag]] = []
        opening_title: Optional[str] = None
        used: set[int] = set()
        headings = _find_split_headings(document.soup)
        for entry in entries:
            target = _normalize_title(entry.title)
            for heading in headings:
                if id(heading) in used:
                    continue
                if _normalize_title(heading.get_text(" ", strip=True)) == target:
                    used.add(id(heading))
                    markers.append((entry.title, heading))
                    break
            else:
                if not markers and opening_title is None:
                    opening_title = entry.title
        if len(markers) < 2:
            return []
        return self._split_document_at_markers(document, markers, opening_title=opening_title)

    def _sections_from_document_headings(self, documents: list[_DocumentPart]) -> list[EPUBSection]:
        sections: list[EPUBSection] = []
        for document in documents:
            if not self._has_readable_content(document):
                continue

            headings = _find_split_headings(document.soup)
            if len(headings) < 2:
                continue
            if not _is_generic_container_title(document.title):
                heading_level = headings[0].name
                if heading_level != "h1":
                    continue

            markers = [
                (heading.get_text(" ", strip=True), heading)
                for heading in headings
                if heading.get_text(" ", strip=True)
            ]
            if len(markers) < 2:
                continue
            sections.extend(self._split_document_at_markers(document, markers))
        return self._filter_sections(sections)

    def _fallback_document_sections(self, documents: list[_DocumentPart]) -> list[EPUBSection]:
        sections: list[EPUBSection] = []
        for document in documents:
            if not self._has_readable_content(document):
                continue
            if _is_structural_title(document.title, document.source_name):
                continue
            sections.append(self._section_from_document(document))
        return sections

    def _sections_from_fragment_excluded_document(
        self,
        document: _DocumentPart,
        excluded_fragments: set[str],
    ) -> list[EPUBSection]:
        # TOC labels can identify structural material whose visible heading is
        # descriptive (e.g. "Publisher Statement"). Add those boundaries first
        # so the TOC classification wins when both markers occupy one block.
        markers: list[tuple[str, Tag]] = []
        for entry in self._read_toc_entries():
            if entry.fragment not in excluded_fragments:
                continue
            if not _is_structural_title(entry.title, entry.source_name):
                continue
            marker = _find_fragment_marker(document.soup, entry.fragment)
            if marker is not None:
                markers.append((entry.title, marker))
        markers.extend(
            (heading.get_text(" ", strip=True), heading)
            for heading in _find_split_headings(document.soup)
            if heading.get_text(" ", strip=True)
        )
        if not any(
            not _is_structural_title(title, document.source_name)
            for title, _ in markers
        ):
            return []
        return self._filter_sections(self._split_document_at_markers(document, markers))

    def _merge_unclaimed_spine_documents(
        self,
        toc_sections: list[EPUBSection],
        documents: list[_DocumentPart],
        excluded_sources: set[str],
        excluded_fragments: dict[str, set[str]],
    ) -> list[EPUBSection]:
        claimed_sources = {section.source_name for section in toc_sections}
        claimed_section_markup = [
            section.html_content for section in toc_sections if section.html_content
        ]

        sections = list(toc_sections)
        for document in documents:
            if not document.in_spine:
                continue
            if document.source_name in excluded_sources:
                continue
            if document.source_name in claimed_sources:
                continue
            if document.html_content and any(
                document.html_content in section_markup
                for section_markup in claimed_section_markup
            ):
                continue
            if document.source_name in excluded_fragments:
                sections.extend(self._sections_from_fragment_excluded_document(
                    document, excluded_fragments[document.source_name],
                ))
                continue
            if not self._has_readable_content(document):
                continue
            if _is_structural_title(document.title, document.source_name):
                continue
            sections.append(self._section_from_document(document))

        document_order = {document.source_name: document.order for document in documents}
        return sorted(
            sections,
            key=lambda section: document_order.get(section.source_name, len(documents)),
        )

    def _split_document_at_markers(
        self,
        document: _DocumentPart,
        markers: list[tuple[str, Tag]],
        *,
        opening_title: Optional[str] = None,
    ) -> list[EPUBSection]:
        root = _common_content_parent([marker for _, marker in markers], document.soup)
        # A named opening may start outside the element wrapping the markers.
        opening = _body_content_before(root, document.soup) if opening_title else []
        children = [*opening, *root.children]
        child_indexes = {id(child): index for index, child in enumerate(children)}
        marker_indexes: list[tuple[str, int]] = []
        for title, marker in markers:
            child = _direct_child_under(marker, root)
            index = child_indexes.get(id(child))
            if index is None:
                return []
            marker_indexes.append((title, index))

        deduped_marker_indexes: list[tuple[str, int]] = []
        seen_indexes: set[int] = set()
        for title, index in sorted(marker_indexes, key=lambda item: item[1]):
            if index in seen_indexes:
                continue
            seen_indexes.add(index)
            deduped_marker_indexes.append((title, index))

        # Content before the first marker is the document's opening. Only a
        # TOC entry naming it (a nested TOC's parent chapter) makes it a
        # section, which _filter_sections drops when too short or structural.
        # An untitled opening is title pages, dedications, blurbs and licence
        # headers far more often than prose, so it is not narrated.
        if opening_title and deduped_marker_indexes[0][1] > 0:
            deduped_marker_indexes = [(opening_title, 0), *deduped_marker_indexes]

        sections: list[EPUBSection] = []
        for position, (title, start_index) in enumerate(deduped_marker_indexes):
            next_position = position + 1
            end_index = (
                deduped_marker_indexes[next_position][1]
                if next_position < len(deduped_marker_indexes)
                else len(children)
            )
            html_content = "".join(str(child) for child in children[start_index:end_index])
            text_content = self._text_from_fragment(html_content)
            sections.append(
                EPUBSection(
                    number=0,
                    title=title,
                    content=text_content,
                    html_content=html_content,
                    source_name=document.source_name,
                )
            )
        return self._filter_sections(sections)

    def _section_from_document(self, document: _DocumentPart, *, title: Optional[str] = None) -> EPUBSection:
        section_title = (title or document.title or _title_from_href(document.source_name) or "Chapter").strip()
        return EPUBSection(
            number=0,
            title=section_title,
            content=document.text_content,
            html_content=document.html_content,
            source_name=document.source_name,
        )

    def _text_from_fragment(self, html_content: str) -> str:
        return self.soup_to_text(BeautifulSoup(html_content, "lxml"))

    def _has_readable_content(self, document: _DocumentPart) -> bool:
        return len(document.text_content.strip()) >= self.min_section_chars

    def _filter_sections(self, sections: Iterable[EPUBSection]) -> list[EPUBSection]:
        filtered: list[EPUBSection] = []
        for section in sections:
            if (
                len(section.content.strip()) < self.min_section_chars
                and not _is_part_divider_title(section.title)
            ):
                continue
            if _is_structural_title(section.title, section.source_name):
                continue
            filtered.append(section)
        return filtered

    @staticmethod
    def _decode_item(item: Any) -> str:
        content = item.get_content()
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            pass
        if content.startswith(codecs.BOM_UTF16_LE) or content.startswith(codecs.BOM_UTF16_BE):
            try:
                return content.decode("utf-16")
            except UnicodeDecodeError:
                pass
        declared = _declared_markup_encoding(content)
        if declared:
            try:
                return content.decode(declared)
            except (UnicodeDecodeError, LookupError):
                pass
        return content.decode("latin-1")

    @staticmethod
    def _item_key(item: Any) -> str:
        return str(getattr(item, "id", "") or item.get_name() or id(item))

    @staticmethod
    def _renumber(sections: list[EPUBSection]) -> list[EPUBSection]:
        for index, section in enumerate(sections, start=1):
            section.number = index
        return sections


class _DocumentResolver:
    """Resolve TOC hrefs to parsed spine documents."""

    def __init__(self, documents: list[_DocumentPart]) -> None:
        self.by_name = {document.source_name: document for document in documents}
        self.by_basename: dict[str, _DocumentPart] = {}
        basename_counts: dict[str, int] = defaultdict(int)
        for document in documents:
            basename_counts[PurePosixPath(document.source_name).name] += 1
        for document in documents:
            basename = PurePosixPath(document.source_name).name
            if basename_counts[basename] == 1:
                self.by_basename[basename] = document

    def resolve(self, source_name: str) -> Optional[_DocumentPart]:
        normalized = _normalize_href(source_name)
        if normalized in self.by_name:
            return self.by_name[normalized]

        basename = PurePosixPath(normalized).name
        if basename in self.by_basename:
            return self.by_basename[basename]

        suffix_matches = [
            document
            for name, document in self.by_name.items()
            if name.endswith(f"/{normalized}") or normalized.endswith(f"/{name}")
        ]
        if len(suffix_matches) == 1:
            return suffix_matches[0]
        return None


def _normalize_toc(toc: Any) -> list[Any]:
    """Normalize ``book.toc`` to a list of nodes.

    ebooklib usually exposes a list/tuple, but a nav-only spine EPUB can yield a
    bare, non-iterable ``Link``. Wrapping a lone node keeps ``_flatten_toc_nodes``
    from raising ``TypeError: 'Link' object is not iterable`` out of the parser.
    """
    if toc is None:
        return []
    if isinstance(toc, (list, tuple)):
        return list(toc)
    return [toc]


def _flatten_toc_nodes(nodes: Iterable[Any]) -> Iterable[Any]:
    for node in nodes:
        if isinstance(node, tuple) and len(node) == 2:
            item, children = node
            yield item
            yield from _flatten_toc_nodes(children)
        else:
            yield node


def _split_href(href: str) -> tuple[str, Optional[str]]:
    source, separator, fragment = href.partition("#")
    normalized_source = _normalize_href(source)
    return normalized_source, unquote(fragment) if separator and fragment else None


def _normalize_href(href: str) -> str:
    href = unquote((href or "").strip()).split("?", 1)[0]
    href = href.replace("\\", "/").lstrip("/")
    if not href:
        return ""
    return PurePosixPath(href).as_posix().removeprefix("./")


def _title_from_href(href: str) -> str:
    name = PurePosixPath(_normalize_href(href)).stem
    return re.sub(r"[_-]+", " ", name).strip().title()


def _normalize_title(title: Optional[str]) -> str:
    normalized = re.sub(r"\s+", " ", title or "").strip().lower()
    normalized = normalized.strip(" .:-–—")
    return normalized


def _is_structural_title(title: Optional[str], source_name: str = "") -> bool:
    normalized = _normalize_title(title)
    if normalized:
        # A real title decides on its own; a structural-looking file stem
        # (index, notes, ...) must not veto a narrative TOC/heading title.
        return bool(
            _STRUCTURAL_TITLE_RE.match(normalized)
            or normalized.startswith("copyright ")
        )

    stem = PurePosixPath(_normalize_href(source_name)).stem.lower()
    stem = re.sub(r"[_-]+", " ", stem)
    return bool(stem and _STRUCTURAL_TITLE_RE.match(stem))


def _document_entry_title(document: _DocumentPart, entries: list[_TOCEntry]) -> Optional[str]:
    """Title from a document's fragment-less TOC entries: the one naming its
    heading, else the last one (an earlier one is usually a "Begin Reading"
    alias)."""
    whole_document_entries = [entry for entry in entries if not entry.fragment]
    if not whole_document_entries:
        return None
    heading = _normalize_title(document.title)
    return next(
        (entry.title for entry in whole_document_entries if _normalize_title(entry.title) == heading),
        whole_document_entries[-1].title,
    )


def _is_generic_container_title(title: Optional[str]) -> bool:
    normalized = _normalize_title(title)
    return bool(normalized and _GENERIC_CONTAINER_TITLE_RE.match(normalized))


def _is_part_divider_title(title: Optional[str]) -> bool:
    normalized = _normalize_title(title)
    return bool(normalized and _PART_DIVIDER_TITLE_RE.match(normalized))


def _find_fragment_marker(soup: BeautifulSoup, fragment: Optional[str]) -> Optional[Tag]:
    if not fragment:
        return None
    marker = soup.find(id=fragment)
    if isinstance(marker, Tag):
        return marker
    marker = soup.find(attrs={"name": fragment})
    if isinstance(marker, Tag):
        return marker
    return None


def _find_split_headings(soup: BeautifulSoup) -> list[Tag]:
    for tag_name in ["h1", "h2"]:
        headings = [
            heading
            for heading in soup.find_all(tag_name)
            if isinstance(heading, Tag) and heading.get_text(" ", strip=True)
        ]
        if len(headings) >= 2:
            return headings
    return []


def _common_content_parent(markers: list[Tag], soup: BeautifulSoup) -> Tag:
    if not markers:
        body = soup.find("body")
        return body if isinstance(body, Tag) else soup

    current: Optional[PageElement] = markers[0]
    while current is not None:
        if isinstance(current, Tag) and all(_contains(current, marker) for marker in markers):
            body = current.find("body", recursive=False)
            return body if isinstance(body, Tag) else current
        current = current.parent

    body = soup.find("body")
    return body if isinstance(body, Tag) else soup


def _body_content_before(root: Tag, soup: BeautifulSoup) -> list[PageElement]:
    """Return the body content before a nested root, in document order."""
    body = soup.find("body")
    before: list[PageElement] = []
    if not isinstance(body, Tag) or not _contains(body, root):
        return before
    node: Tag = root
    while node is not body and node.parent is not None:
        before[:0] = reversed(list(node.previous_siblings))
        node = node.parent
    return before


def _contains(parent: Tag, child: Tag) -> bool:
    current: Optional[PageElement] = child
    while current is not None:
        if current is parent:
            return True
        current = current.parent
    return False


def _direct_child_under(element: Tag, root: Tag) -> PageElement:
    current: PageElement = element
    while current.parent is not None and current.parent is not root:
        current = current.parent
    return current
