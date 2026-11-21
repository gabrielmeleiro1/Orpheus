"""
Markdown parser module.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import re

from .models import Chapter, Document
from .text_encoding import read_source_text
from .text_layout import repair_text_layout

from modules.errors import CorruptedFileError


# Callers strip the title. Leaving surrounding whitespace in the group keeps
# matching linear: a lazy title followed by ``\s*$`` rescans every trailing
# whitespace run once per character of title.
HEADING_RE = re.compile(r"^(#{1,6})\s(.+)$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


_LINE_RE = re.compile(r"[^\n\r\v\f\x1c-\x1e\x85\u2028\u2029]*(?:\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]|$)")


def _line_matches(text: str) -> Iterator[re.Match[str]]:
    # Match str.splitlines(keepends=True), without retaining all lines.
    for match in _LINE_RE.finditer(text):
        if match.start() != match.end():
            yield match


def strip_leading_front_matter(raw_text: str) -> str:
    """Remove only a closed YAML front-matter block on the first line."""
    lines = _line_matches(raw_text)
    first = next(lines, None)
    if first is None or first.group().strip() != "---":
        return raw_text
    for match in lines:
        if match.group().strip() in {"---", "..."}:
            return raw_text[match.end():]
    return raw_text


def iter_markdown_lines(raw_text: str) -> Iterator[tuple[str, str | None]]:
    """Yield each line and its heading, recognizing backtick and tilde fences."""
    fence_char: str | None = None
    fence_length = 0
    for line_match in _line_matches(raw_text):
        line = line_match.group().rstrip("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
        title = None
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence_char is None:
                fence_char = marker[0]
                fence_length = len(marker)
            elif (
                marker[0] == fence_char
                and len(marker) >= fence_length
                and not fence_match.group(2).strip()
            ):
                fence_char = None
                fence_length = 0
        elif fence_char is None:
            match = HEADING_RE.match(line)
            if match:
                title = match.group(2).strip()
        yield line, title


_BYLINE_RE = re.compile(r"^[*_]{0,3}\s*by\s+(?P<author>[^*_\n]+?)\s*[*_]{0,3}$", re.IGNORECASE)


def _strip_emphasis(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`]", "", text)).strip()


def detect_title_block(raw_text: str) -> tuple[str, str | None] | None:
    """Return (title, author) when the document opens with a title block.

    A title block is a level-1 heading whose body holds nothing but an optional
    byline ("*by Author*"), followed by the first real section. It is book
    metadata, not a chapter to narrate. Without a byline, the next heading must
    be a lower level ("# Book" then "## Chapter"), so a book of top-level
    chapters keeps its first one.
    """
    title: str | None = None
    author: str | None = None
    for line, heading in iter_markdown_lines(raw_text):
        stripped = line.strip()
        if heading is not None:
            level = len(HEADING_RE.match(line).group(1))
            if title is None:
                if level != 1:
                    return None
                title = _strip_emphasis(heading)
                continue
            if author is None and level == 1:
                return None
            return title, author
        if not stripped:
            continue
        if title is None:
            # Body text before the first heading: the document does not open
            # with a title block, so that heading is a chapter.
            return None
        byline = _BYLINE_RE.match(stripped)
        if byline is None or author is not None:
            return None
        author = _strip_emphasis(byline.group("author")) or None
    return None


def markdown_headings(raw_text: str) -> list[tuple[int, str]]:
    """Return heading lines outside fenced code blocks."""
    return [
        (number, title)
        for number, (_, title) in enumerate(iter_markdown_lines(raw_text))
        if title is not None
    ]


class MarkdownParser:
    """Parse `.md` files into heading-based sections when available."""

    def __init__(self, file_path: Path | str):
        self.file_path = Path(file_path)
        if not self.file_path.exists():
            raise FileNotFoundError(f"Markdown file not found: {self.file_path}")
        if self.file_path.suffix.lower() != ".md":
            raise ValueError(f"Not a markdown file: {self.file_path}")

    def parse(self) -> Document:
        raw_text = read_source_text(self.file_path)
        content_text = strip_leading_front_matter(raw_text)
        sections = self._split_sections(content_text)
        title = sections[0].title if sections else self.file_path.stem
        author = None
        title_block = detect_title_block(content_text)
        if (
            title_block is not None
            and len(sections) > 1
            and (not sections[0].content.strip() or _BYLINE_RE.match(sections[0].content.strip()))
        ):
            # The title block is metadata; narrating it would add a chapter
            # that only reads the title and byline.
            title, author = title_block
            sections = [
                Chapter(number=index, title=section.title, content=section.content)
                for index, section in enumerate(sections[1:], start=1)
            ]
        chapters = sections or [
            Chapter(number=1, title=self.file_path.stem, content=content_text.strip())
        ]
        for chapter in chapters:
            chapter.content = repair_text_layout(chapter.content).strip()
        if not any(chapter.content.strip() for chapter in chapters):
            raise CorruptedFileError(
                message=(
                    "The file is empty." if self.file_path.stat().st_size == 0
                    else "Markdown file contains no readable text"
                ),
                file_path=self.file_path,
            )
        return Document(
            title=title,
            author=author,
            chapters=chapters,
            raw_markdown=raw_text,
        )

    def _split_sections(self, raw_text: str) -> list[Chapter]:
        lines = raw_text.splitlines()
        preamble: list[str] = []
        sections: list[tuple[str, list[str]]] = []
        current_title: str | None = None
        current_lines: list[str] = []

        heading_lines = dict(markdown_headings(raw_text))
        for line_number, line in enumerate(lines):
            title = heading_lines.get(line_number)
            if title is not None:
                if current_title is not None:
                    sections.append((current_title, current_lines))
                else:
                    preamble = current_lines
                current_title = title
                current_lines = []
                continue
            current_lines.append(line)

        if current_title is not None:
            sections.append((current_title, current_lines))

        if not sections:
            return []

        if preamble:
            sections[0][1][:0] = preamble

        chapters: list[Chapter] = []
        for index, (title, content_lines) in enumerate(sections, start=1):
            content = "\n".join(content_lines).strip()
            chapters.append(Chapter(number=index, title=title, content=content))
        return chapters
