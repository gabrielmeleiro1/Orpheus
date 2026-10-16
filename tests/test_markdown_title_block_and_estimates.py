"""Markdown title blocks are metadata, emphasis never reaches speech, and preflight estimates length."""

from __future__ import annotations

import pytest

from modules.ingestion.markdown_parser import MarkdownParser
from modules.ingestion.preflight import preflight_source
from modules.tts.cleaner_rules import rule_based_clean

_BOOK = (
    "# Café Ümlaut\n\n*by Zoë Brontë-Ng*\n\n"
    "## Morning Service\n\nThe vicar hid the tide table at a *quarter* past nine.\n\n"
    "## Closing Time\n\nThomas hid a ship's bell in the **grey** afternoon.\n"
)


def test_title_and_byline_block_is_metadata_not_a_chapter(tmp_path):
    source = tmp_path / "book.md"
    source.write_text(_BOOK, encoding="utf-8")

    document = MarkdownParser(source).parse()
    preflight = preflight_source(source)

    assert document.title == "Café Ümlaut"
    assert document.author == "Zoë Brontë-Ng"
    assert [chapter.title for chapter in document.chapters] == ["Morning Service", "Closing Time"]
    assert [chapter.number for chapter in document.chapters] == [1, 2]
    assert preflight["title"] == "Café Ümlaut"
    assert preflight["author"] == "Zoë Brontë-Ng"
    assert preflight["chapter_count"] == 2
    assert preflight["chapter_titles"] == ["Morning Service", "Closing Time"]
    assert "*" not in preflight["sample"] and "by Zoë" not in preflight["sample"]


@pytest.mark.parametrize(
    "text",
    [
        # Top-level chapters: the first heading is a chapter, not a title.
        "# One\n\nFirst chapter prose.\n\n# Two\n\nSecond chapter prose.\n",
        # Prose under the title is narrated, not dropped.
        "# Book\n\nAn opening paragraph worth reading aloud.\n\n## Chapter\n\nMore prose.\n",
    ],
)
def test_headings_with_prose_or_peer_chapters_stay_chapters(tmp_path, text):
    source = tmp_path / "book.md"
    source.write_text(text, encoding="utf-8")

    document = MarkdownParser(source).parse()

    assert len(document.chapters) == 2
    assert document.author is None


def test_emphasis_markers_never_reach_speech_text():
    spoken = rule_based_clean("*by Zoë Brontë-Ng* said **very** _quietly_ and ***loudly***.")

    assert not set(spoken) & {"*", "_"}
    assert "very" in spoken and "quietly" in spoken and "loudly" in spoken


def test_preview_sample_drops_emphasis_but_keeps_identifiers_and_fences(tmp_path):
    source = tmp_path / "notes.md"
    source.write_text(
        "# Notes\n\nShe said **very** _quietly_ that snake_case_name stays.\n\n```py\nx = 2 * 3 * 4\n```\n",
        encoding="utf-8",
    )

    sample = preflight_source(source)["sample"]

    assert sample == "She said very quietly that snake_case_name stays. ```py x = 2 * 3 * 4 ```"


def test_preflight_estimates_audio_length_for_oversized_pastes(tmp_path):
    source = tmp_path / "paste.txt"
    source.write_text("word " * 200_000, encoding="utf-8")

    estimate = preflight_source(source)["estimated_audio_seconds"]

    # 999,999 spoken characters at 18.5 chars/second, the rate the desktop
    # queue mirrors for its own estimates (conversionPanelHelpers.ts).
    assert estimate == 54_054
