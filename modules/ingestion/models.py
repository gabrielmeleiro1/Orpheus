"""Shared ingestion document contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Chapter:
    """Common chapter representation returned by ingestion parsers."""

    number: int
    title: str
    content: str


@dataclass
class Document:
    """Common document representation returned by ingestion parsers."""

    title: str
    author: Optional[str] = None
    chapters: list[Chapter] = field(default_factory=list)
    raw_markdown: str = ""
