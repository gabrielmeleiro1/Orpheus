from __future__ import annotations

import json
from pathlib import Path

import pytest

from modules.ingestion.markdown_parser import MarkdownParser
from modules.text.document_normalizer import normalize_extracted_document

POLICY_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "policy"


@pytest.mark.parametrize(
    "case",
    json.loads((POLICY_FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))["cases"],
    ids=lambda case: case["id"],
)
def test_policy_fixtures_match_parse_skip_expectations(case: dict) -> None:
    source = POLICY_FIXTURE_ROOT / case["source"]
    document = normalize_extracted_document(
        MarkdownParser(source).parse(),
        source_type=source.suffix,
        skip_non_prose_sections=True,
    )
    skipped = document.skipped_non_prose_sections

    if case.get("must_not_skip"):
        assert not skipped
        assert document.chapters
    if case.get("expected_skip"):
        assert skipped
        assert case["expected_reason"] in {entry["reason"] for entry in skipped}
    if "expected_skip_reasons" in case:
        assert set(case["expected_skip_reasons"]) <= {entry["reason"] for entry in skipped}
    if "expected_chapters" in case:
        assert len(document.chapters) == case["expected_chapters"]
