"""Regression coverage for ingestion."""

import pytest

from modules.ingestion.epub_sections import parse_epub_markup, soup_to_epub_text
from modules.ingestion.markdown_parser import MarkdownParser
from modules.ingestion.text_parser import TextParser
from modules.tts.cleaner_rules import rule_based_clean


@pytest.mark.parametrize("text", ["The answer is\n42\nwhich is exact.", "42"])
@pytest.mark.parametrize("kind", ["txt", "md", "epub", "cleaner"])
def test_01_standalone_numbers_are_content(tmp_path, text, kind):
    if kind in {"txt", "md"}:
        path = tmp_path / f"numbers.{kind}"
        path.write_text(text, encoding="utf-8")
        parser = TextParser if kind == "txt" else MarkdownParser
        result = parser(path).parse().chapters[0].content
    elif kind == "epub":
        result = soup_to_epub_text(parse_epub_markup("<p>" + text + "</p>"))
    else:
        result = rule_based_clean(text)
    assert "42" in result
