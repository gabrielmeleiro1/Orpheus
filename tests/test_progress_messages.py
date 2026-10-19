"""Progress messages name the book and the chapter that chunk counters belong to."""

from __future__ import annotations

from pathlib import Path

import pytest

from modules.pipeline.orchestrator import _display_source_name
from modules.pipeline.progress import chapter_progress_message


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("1794851209_3954793f.epub", "the EPUB source"),
        ("1794836799_c9c997be_Parallel Pool Probe.md", "Parallel Pool Probe.md"),
        ("The Lantern at Harrow Point.pdf", "The Lantern at Harrow Point.pdf"),
    ],
)
def test_upload_handles_never_reach_the_progress_text(name, expected):
    assert _display_source_name(Path(name)) == expected


def test_chunk_progress_carries_its_chapter():
    assert chapter_progress_message(2, 8, "cleaning chunk 1/4") == "Chapter 3/8: cleaning chunk 1/4"
    assert chapter_progress_message(0, 0, "encoding") == "Encoding"


def test_conversion_progress_messages_have_book_and_chapter_context(conversion):
    source, _engine, make_pipeline = conversion
    pipeline = make_pipeline()
    messages: list[str] = []
    pipeline.progress_callback = lambda stage, *args: messages.append(args[4])

    result = pipeline.convert(source, title="Tiny Book")

    assert result.success, result.error
    assert messages[0] == "Reading Tiny Book..."
    chunk_messages = [message for message in messages if "chunk" in message]
    assert chunk_messages
    assert all(message.startswith(("Chapter 1/2: ", "Chapter 2/2: ")) for message in chunk_messages)
