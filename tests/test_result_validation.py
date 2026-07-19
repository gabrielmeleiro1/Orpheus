"""Tests for pipeline result validation helpers."""

from __future__ import annotations

from pathlib import Path
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


def _completed_chapter(number: int, mp3_path: Path) -> SimpleNamespace:
    """Mirror ChapterResult, whose mp3_path is a Path (see pipeline/models.py)."""
    return SimpleNamespace(
        number=number,
        chapter_number=number,
        title=f"Chapter {number}",
        chapter_title=f"Chapter {number}",
        error=None,
        mp3_path=Path(mp3_path),
    )


def test_success_artifacts_reject_a_result_with_no_packaged_output(tmp_path) -> None:
    """The last gate between "pipeline finished" and "book is playable".

    If this stops firing, a conversion that produced no .m4b is reported as a
    success and written to the library as a playable book.
    """
    from modules.pipeline.result_validation import validate_conversion_success_artifacts

    chapter_audio = tmp_path / "01.mp3"
    chapter_audio.write_bytes(b"ID3")
    result = SimpleNamespace(
        chapters=[_completed_chapter(1, chapter_audio)],
        output_path=None,
    )

    validation = validate_conversion_success_artifacts(
        result,
        emit_chapter_mp3_artifacts=True,
        require_packaged_output=True,
    )

    assert validation.is_valid is False
    assert validation.packaged_output_note == "missing packaged output"


def test_success_artifacts_reject_a_packaged_output_path_that_does_not_exist(tmp_path) -> None:
    from modules.pipeline.result_validation import validate_conversion_success_artifacts

    chapter_audio = tmp_path / "01.mp3"
    chapter_audio.write_bytes(b"ID3")
    missing_book = tmp_path / "Book.m4b"
    result = SimpleNamespace(
        chapters=[_completed_chapter(1, chapter_audio)],
        output_path=str(missing_book),
    )

    validation = validate_conversion_success_artifacts(
        result,
        emit_chapter_mp3_artifacts=True,
        require_packaged_output=True,
    )

    assert validation.is_valid is False
    assert validation.missing_packaged_output == missing_book
    assert "missing packaged output" in (validation.packaged_output_note or "")


def test_success_artifacts_accept_a_complete_result(tmp_path) -> None:
    from modules.pipeline.result_validation import validate_conversion_success_artifacts

    chapter_audio = tmp_path / "01.mp3"
    chapter_audio.write_bytes(b"ID3")
    packaged = tmp_path / "Book.m4b"
    packaged.write_bytes(b"ftypM4A ")
    result = SimpleNamespace(
        chapters=[_completed_chapter(1, chapter_audio)],
        output_path=str(packaged),
    )

    validation = validate_conversion_success_artifacts(
        result,
        emit_chapter_mp3_artifacts=True,
        require_packaged_output=True,
    )

    assert validation.is_valid is True
    assert validation.chapters_completed == 1


def test_success_artifacts_reject_a_chapter_whose_audio_file_is_gone(tmp_path) -> None:
    from modules.pipeline.result_validation import validate_conversion_success_artifacts

    packaged = tmp_path / "Book.m4b"
    packaged.write_bytes(b"ftypM4A ")
    result = SimpleNamespace(
        chapters=[_completed_chapter(1, tmp_path / "never_written.mp3")],
        output_path=str(packaged),
    )

    validation = validate_conversion_success_artifacts(
        result,
        emit_chapter_mp3_artifacts=True,
        require_packaged_output=True,
    )

    assert validation.is_valid is False
    assert validation.chapter_notes
