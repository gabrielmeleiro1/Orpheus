"""
Markdown parser module.
"""

from __future__ import annotations

from pathlib import Path
import re

from .models import Chapter, Document
from .text_encoding import read_source_text

from modules.errors import CorruptedFileError


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


def strip_leading_front_matter(raw_text: str) -> str:
    """Remove a closed YAML front-matter block at the start of a document.

    Only a delimiter on the first line can open the block, and an unclosed
    block is left untouched so malformed documents are not silently truncated.
    Later thematic ``---`` separators remain ordinary Markdown content.
    """
    lines = raw_text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return raw_text

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() in {"---", "..."}:
            return "".join(lines[index + 1 :])
    return raw_text


def markdown_headings(raw_text: str) -> list[tuple[int, str]]:
    """Return heading lines outside fenced code blocks.

    Both backtick and tilde fences are recognized. A closing fence must use
    the same marker character and be at least as long as its opener.
    """
    headings: list[tuple[int, str]] = []
    fence_char: str | None = None
    fence_length = 0

    for line_number, line in enumerate(raw_text.splitlines()):
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            marker_char = marker[0]
            marker_length = len(marker)
            suffix = fence_match.group(2)
            if fence_char is None:
                fence_char = marker_char
                fence_length = marker_length
            elif (
                marker_char == fence_char
                and marker_length >= fence_length
                and not suffix.strip()
            ):
                fence_char = None
                fence_length = 0
            continue

        if fence_char is None:
            match = HEADING_RE.match(line)
            if match:
                headings.append((line_number, match.group(2).strip()))

    return headings


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
        chapters = sections or [
            Chapter(number=1, title=self.file_path.stem, content=content_text.strip())
        ]
        if not any(chapter.content.strip() for chapter in chapters):
            raise CorruptedFileError(
                message="Markdown file contains no readable text",
                file_path=self.file_path,
            )
        return Document(
            title=title,
            author=None,
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
