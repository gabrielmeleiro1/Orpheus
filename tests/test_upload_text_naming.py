"""Pasted-text uploads keep the whole title in their file name."""

from __future__ import annotations

import pytest

from modules.app.services.library_service import LibraryService
from modules.errors import sanitize_filename


def _library_service(tmp_path) -> LibraryService:
    return LibraryService(repository=object(), output_dir=tmp_path / "output", temp_dir=tmp_path / "temp")


@pytest.mark.parametrize("title,expected", [
    ("Mr. Smith Goes to Washington", "Mr. Smith Goes to Washington.md"),
    ("Vol. 2", "Vol. 2.md"),
    ("1.5 Release Notes", "1.5 Release Notes.md"),
    ("notes.txt", "notes.txt.md"),
])
def test_pasted_title_with_dots_is_not_cut_at_the_first_dot(tmp_path, title, expected) -> None:
    service = _library_service(tmp_path)

    result = service.upload_text_source(text="Some pasted words.", title=title)

    assert result["original_name"] == expected
    assert result["source_handle"].endswith("_" + expected)


@pytest.mark.parametrize("name", [
    "Mr. " + "x" * 300,
    "Mr. " + "é" * 190,
    "A." + "b" * 250 + ".txt",
])
def test_sanitize_filename_honours_its_byte_budget_for_dotted_names(name) -> None:
    sanitized = sanitize_filename(name)

    assert 0 < len(sanitized.encode("utf-8")) <= 200
    assert sanitized.startswith(name[:2])


@pytest.mark.parametrize("name", ["a" * 199 + " b" * 5, "a" * 199 + ". b" * 5])
def test_sanitize_filename_truncation_never_leaves_a_trailing_dot_or_space(name) -> None:
    # Windows drops trailing dots and spaces, so the name on disk would differ.
    sanitized = sanitize_filename(name)

    assert sanitized == sanitized.rstrip(" .")
    assert len(sanitized.encode("utf-8")) <= 200


def test_sanitize_filename_keeps_a_real_extension_when_truncating() -> None:
    sanitized = sanitize_filename("x" * 300 + ".epub")

    assert sanitized.endswith(".epub")
    assert len(sanitized.encode("utf-8")) == 200
