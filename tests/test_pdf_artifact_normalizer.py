"""Fixture-backed tests for PDF artifact normalization policies."""

from __future__ import annotations

import json
from pathlib import Path


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "pdf_artifact_normalization_samples.json"


def test_numbered_running_headers_match_across_pages_without_matching_chapter_numbers():
    from modules.ingestion.pdf_artifact_normalizer import RepeatedPageBoundaryPolicy

    pages = [
        {"text": "The lighthouse keeper 12\nChapter 1\nThe lamp was cold."},
        {"text": "13 The lighthouse keeper\nChapter 2\nHe struck a match."},
    ]
    normalized = RepeatedPageBoundaryPolicy().normalize_pages(pages)

    assert [page["text"] for page in normalized] == [
        "Chapter 1\nThe lamp was cold.",
        "Chapter 2\nHe struck a match.",
    ]
    assert "keeper 12" in pages[0]["text"]


def test_single_pdf_page_repairs_layout_before_paragraph_reflow():
    from modules.ingestion.pdf_artifact_normalizer import normalize_pdf_artifacts

    pages = [{"text": "THE LIGHTHOUSE KEEPER 12\nThe keep-\ner wa-\nited."}]
    assert normalize_pdf_artifacts(pages)[0]["text"] == "THE LIGHTHOUSE KEEPER 12\nThe keeper waited."


def test_pdf_artifact_normalization_fixture_samples() -> None:
    from modules.ingestion.pdf_artifact_normalizer import (
        normalize_pdf_artifacts,
    )

    samples = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    for sample in samples:
        normalized = normalize_pdf_artifacts(sample["pages"])

        assert [page["text"] for page in normalized] == sample["expected_texts"], sample["name"]


def test_default_policies_preserve_numbered_section_headings() -> None:
    from modules.ingestion.pdf_artifact_normalizer import normalize_pdf_artifacts

    pages = [
        {
            "text": (
                "1 The problem of evil\n"
                "Body paragraph that must survive default normalization intact.\n\n"
                "Discussion continues well after the numbered heading above."
            )
        }
    ]

    normalized = normalize_pdf_artifacts(pages)

    assert "1 The problem of evil" in normalized[0]["text"]
    assert "Body paragraph that must survive" in normalized[0]["text"]


def test_repair_line_break_hyphenation_is_fast_on_pathological_page() -> None:
    # A hyphen-dense page must not trigger quadratic backtracking.
    import time

    from modules.ingestion.pdf_artifact_normalizer import repair_line_break_hyphenation

    text = "word-" * 8000  # trailing hyphens, no line break to satisfy the match

    start = time.perf_counter()
    repair_line_break_hyphenation(text)
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"hyphenation repair took {elapsed:.3f}s"


def test_repair_line_break_hyphenation_still_joins_known_compounds() -> None:
    from modules.ingestion.pdf_artifact_normalizer import repair_line_break_hyphenation

    # Single-segment known compound split across a line break.
    assert repair_line_break_hyphenation("photo-\nreconnaissance") == "photo-reconnaissance"
    # Multi-segment known compound.
    assert repair_line_break_hyphenation("coming-into-\nbeing") == "coming-into-being"
    # Capitalized prefix is preserved on repair.
    assert repair_line_break_hyphenation("Auto-\npilot") == "Auto-pilot"


def test_line_break_hyphen_repair_does_not_glue_distinct_words() -> None:
    from modules.ingestion.pdf_artifact_normalizer import repair_line_break_hyphenation

    assert "intojust" not in repair_line_break_hyphenation("They get into-\njust as before.")
