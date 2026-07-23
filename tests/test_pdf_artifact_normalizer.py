"""Fixture-backed tests for PDF artifact normalization policies."""

from __future__ import annotations

import json
from pathlib import Path


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "pdf_artifact_normalization_samples.json"


def test_pdf_artifact_normalization_fixture_samples() -> None:
    from modules.ingestion.pdf_artifact_normalizer import (
        KNOWN_CORPUS_PDF_ARTIFACT_POLICIES,
        normalize_pdf_artifacts,
    )

    samples = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    for sample in samples:
        normalized = normalize_pdf_artifacts(sample["pages"])

        assert [page["text"] for page in normalized] == sample["expected_texts"], sample["name"]

        corpus_expected = sample.get("known_corpus_expected_texts")
        if corpus_expected is not None:
            corpus_normalized = normalize_pdf_artifacts(
                sample["pages"], KNOWN_CORPUS_PDF_ARTIFACT_POLICIES
            )
            assert [page["text"] for page in corpus_normalized] == corpus_expected, sample["name"]


def test_line_artifact_policy_does_not_own_corpus_specific_footnote_blocks() -> None:
    from modules.ingestion.pdf_artifact_normalizer import (
        KnownCorpusInlineFootnotePolicy,
        PDFLineArtifactPolicy,
    )

    pages = [
        {
            "text": (
                "Body before.\n"
                "I The problem line is a corpus-specific extracted footnote.\n"
                "Wrapped note line.\n\n"
                "Body after."
            )
        }
    ]

    assert "I The problem line" in PDFLineArtifactPolicy().normalize_pages(pages)[0]["text"]
    assert "I The problem line" not in KnownCorpusInlineFootnotePolicy().normalize_pages(pages)[0]["text"]


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


def test_known_corpus_policies_opt_in_strips_inline_footnote_blocks() -> None:
    from modules.ingestion.pdf_artifact_normalizer import (
        KNOWN_CORPUS_PDF_ARTIFACT_POLICIES,
        normalize_pdf_artifacts,
    )

    pages = [
        {
            "text": (
                "Body before.\n"
                "I The problem line is a corpus-specific extracted footnote.\n"
                "Wrapped note line.\n\n"
                "Body after."
            )
        }
    ]

    normalized = normalize_pdf_artifacts(pages, KNOWN_CORPUS_PDF_ARTIFACT_POLICIES)

    assert "The problem line" not in normalized[0]["text"]
    assert "Body before." in normalized[0]["text"]
    assert "Body after." in normalized[0]["text"]


def test_repair_line_break_hyphenation_is_fast_on_pathological_page() -> None:
    # Finding 3: a hyphen-dense page must not trigger quadratic backtracking.
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


def test_default_line_repairs_do_not_apply_corpus_specific_rewrites() -> None:
    from modules.ingestion.pdf_artifact_normalizer import repair_line_break_hyphenation

    text = (
        "They get into-\n"
        "just as auto-sug-\n"
        "gestion and proto-leg-\n"
        "end appear."
    )

    repaired = repair_line_break_hyphenation(text)

    assert "into—just" not in repaired
    assert "auto-suggestion" not in repaired
    assert "proto-legend" not in repaired
    # The generic joiner must not silently glue distinct words together either.
    assert "intojust" not in repaired

    corpus_repaired = repair_line_break_hyphenation(text, known_corpus_repairs=True)

    assert "into—just" in corpus_repaired
    assert "auto-suggestion" in corpus_repaired
    assert "proto-legend" in corpus_repaired
