"""Fixture-backed tests for PDF artifact normalization policies."""

from __future__ import annotations

import json
from pathlib import Path


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "pdf_artifact_normalization_samples.json"


def test_pdf_artifact_normalization_fixture_samples() -> None:
    from modules.ingestion.pdf_artifact_normalizer import normalize_pdf_artifacts

    samples = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    for sample in samples:
        normalized = normalize_pdf_artifacts(sample["pages"])

        assert [page["text"] for page in normalized] == sample["expected_texts"], sample["name"]


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
