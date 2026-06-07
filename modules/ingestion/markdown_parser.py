"""
Markdown parser module.
"""

from __future__ import annotations

from pathlib import Path
import re

from .models import Chapter, Document
from .text_encoding import read_source_text


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

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
        sections = self._split_sections(raw_text)
        title = sections[0].title if sections else self.file_path.stem
        return Document(
            title=title,
            author=None,
            chapters=sections or [Chapter(number=1, title=self.file_path.stem, content=raw_text.strip())],
            raw_markdown=raw_text,
        )

    def _split_sections(self, raw_text: str) -> list[Chapter]:
        lines = raw_text.splitlines()
        preamble: list[str] = []
        sections: list[tuple[str, list[str]]] = []
        current_title: str | None = None
        current_lines: list[str] = []

        for line in lines:
            match = HEADING_RE.match(line)
            if match:
                if current_title is not None:
                    sections.append((current_title, current_lines))
                else:
                    preamble = current_lines
                current_title = match.group(2).strip()
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
