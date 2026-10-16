"""Regression coverage for markdown parser."""

import time

from modules.ingestion.markdown_parser import MarkdownParser
from modules.pipeline.chapter_narration import chapter_body


def test_06_markdown_opening_sentence_is_not_a_heading(tmp_path):
    path = tmp_path / "enough.md"
    path.write_text("# Enough\n\nEnough.\nI refused to take another step.", encoding="utf-8")
    assert chapter_body(MarkdownParser(path).parse().chapters[0]).startswith("Enough.")


def test_heading_with_long_trailing_whitespace_parses_in_linear_time(tmp_path):
    path = tmp_path / "padded.md"
    path.write_text("# Padded  title" + " " * 200_000 + "end\n\nBody text.", encoding="utf-8")
    started = time.perf_counter()
    chapters = MarkdownParser(path).parse().chapters
    assert time.perf_counter() - started < 2.0
    assert chapters[0].title.startswith("Padded  title")
    assert chapters[0].title.endswith("end")


def test_heading_titles_are_stripped(tmp_path):
    path = tmp_path / "spaced.md"
    path.write_text("#   Spaced Title   \n\nBody text.", encoding="utf-8")
    assert MarkdownParser(path).parse().chapters[0].title == "Spaced Title"
