"""Tests for pipeline result validation helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _chapter(number: int, title: str, *, chapter_number: int | None = None) -> SimpleNamespace:
    attrs = {"number": number, "title": title}
    if chapter_number is not None:
        attrs["chapter_number"] = chapter_number
    return SimpleNamespace(**attrs)


def test_validate_chapter_sets_repair_accepts_matching_sets() -> None:
    from modules.pipeline.result_validation import validate_chapter_sets

    source = [_chapter(1, "One"), _chapter(2, "Two")]
    stored = [
        _chapter(1, "One", chapter_number=1),
        _chapter(2, "Two", chapter_number=2),
    ]

    assert validate_chapter_sets(source, stored, context="repair") == []


def test_validate_chapter_sets_repair_reports_duplicate_source_rows() -> None:
    from modules.pipeline.result_validation import validate_chapter_sets

    source = [_chapter(1, "One"), _chapter(1, "One")]
    stored = [_chapter(1, "One", chapter_number=1)]

    errors = validate_chapter_sets(source, stored, context="repair")

    assert errors == ["duplicate source chapter rows: 1"]


def test_validate_chapter_sets_repair_reports_duplicate_stored_rows() -> None:
    from modules.pipeline.result_validation import validate_chapter_sets

    source = [_chapter(1, "One")]
    stored = [
        _chapter(1, "One", chapter_number=1),
        _chapter(1, "One", chapter_number=1),
    ]

    errors = validate_chapter_sets(source, stored, context="repair")

    assert errors == ["duplicate stored chapter rows: 1"]


def test_validate_chapter_sets_repair_reports_missing_stored_rows() -> None:
    from modules.pipeline.result_validation import validate_chapter_sets

    source = [_chapter(1, "One"), _chapter(2, "Two")]
    stored = [_chapter(1, "One", chapter_number=1)]

    errors = validate_chapter_sets(source, stored, context="repair")

    assert errors == ["missing stored chapter rows for source chapters: 2"]


def test_validate_chapter_sets_repair_reports_missing_source_rows() -> None:
    from modules.pipeline.result_validation import validate_chapter_sets

    source = [_chapter(1, "One")]
    stored = [
        _chapter(1, "One", chapter_number=1),
        _chapter(2, "Two", chapter_number=2),
    ]

    errors = validate_chapter_sets(source, stored, context="repair")

    assert errors == ["missing source chapter rows for stored chapters: 2"]


def test_validate_chapter_sets_repair_reports_title_mismatch() -> None:
    from modules.pipeline.result_validation import validate_chapter_sets

    source = [_chapter(1, "One")]
    stored = [_chapter(1, "Different", chapter_number=1)]

    errors = validate_chapter_sets(source, stored, context="repair")

    assert errors == ["title mismatch for chapter 1: expected 'One', got 'Different'"]


def test_validate_chapter_sets_repair_ignores_blank_or_matching_titles() -> None:
    from modules.pipeline.result_validation import validate_chapter_sets

    source = [_chapter(1, ""), _chapter(2, "Two")]
    stored = [
        _chapter(1, "Stored", chapter_number=1),
        _chapter(2, "  two  ", chapter_number=2),
    ]

    assert validate_chapter_sets(source, stored, context="repair") == []


def test_validate_chapter_sets_rejects_unknown_context() -> None:
    from modules.pipeline.result_validation import validate_chapter_sets

    with pytest.raises(ValueError, match="unsupported chapter-set context"):
        validate_chapter_sets([], [], context="convert")
