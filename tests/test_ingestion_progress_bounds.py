"""Ingestion reports a many-chapter source without flooding the event stream."""

from __future__ import annotations

from types import SimpleNamespace

from modules.pipeline.stages.ingestion import ingest_source


def _runtime():
    events: list[dict] = []
    logs: list[str] = []
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            source_type=None,
            pdf_extraction_backend="fitz",
            pre_chunk_document_cleanup=True,
            skip_non_prose_sections=True,
        ),
        log_verbose=lambda message, level="info": logs.append(message),
        notify_progress=lambda **payload: events.append(payload),
    )
    return runtime, events, logs


def test_thousands_of_chapters_emit_bounded_progress_and_logs(tmp_path):
    source = tmp_path / "many.md"
    source.write_text(
        "".join(f"# Heading {index}\n\nThe ferry left at dawn with {index} crates.\n\n" for index in range(5_000)),
        encoding="utf-8",
    )
    runtime, events, logs = _runtime()

    document = ingest_source(runtime, source)

    assert len(document.chapters) == 5_000
    assert len(events) <= 101
    assert events[-1]["chapter_idx"] == 4_999
    assert len(logs) < 260


def test_small_books_still_report_every_chapter(tmp_path):
    source = tmp_path / "small.md"
    source.write_text(
        "".join(f"# Part {index}\n\nThe ferry left at dawn with {index} crates.\n\n" for index in range(12)),
        encoding="utf-8",
    )
    runtime, events, _ = _runtime()

    ingest_source(runtime, source)

    assert [event["chapter_idx"] for event in events] == list(range(12))
