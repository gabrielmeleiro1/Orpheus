"""
Plain-text parser module.
"""

from __future__ import annotations

from pathlib import Path

from .models import Chapter, Document
from .text_encoding import read_source_text
from .text_layout import repair_text_layout

from modules.errors import CorruptedFileError


class TextParser:
    """Parse `.txt` files into one synthetic section."""

    def __init__(self, file_path: Path | str):
        self.file_path = Path(file_path)
        if not self.file_path.exists():
            raise FileNotFoundError(f"Text file not found: {self.file_path}")
        if self.file_path.suffix.lower() != ".txt":
            raise ValueError(f"Not a text file: {self.file_path}")

    def parse(self) -> Document:
        raw_text = self._read_text()
        title = self.file_path.stem
        content = repair_text_layout(raw_text).strip()
        if not content:
            raise CorruptedFileError(
                message="Text file contains no readable text",
                file_path=self.file_path,
            )
        return Document(
            title=title,
            author=None,
            chapters=[Chapter(number=1, title=title, content=content)],
            raw_markdown=raw_text,
        )

    def _read_text(self) -> str:
        return read_source_text(self.file_path)
