"""Pure helpers for validating chapter conversion results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .models import ChapterResult


class ArtifactPolicy(Enum):
    """Artifact contract checked at pipeline packaging boundaries."""

    PACKAGING_INPUTS = "packaging_inputs"
    CHAPTER_MP3_ARTIFACTS = "chapter_mp3_artifacts"
    CHAPTER_ERRORS_ONLY = "chapter_errors_only"


@dataclass(frozen=True)
class ConversionArtifactValidation:
    """Artifact validation result for a successful pipeline result."""

    chapter_notes: list[str]
    missing_packaged_output: Optional[Path] = None
    packaged_output_note: Optional[str] = None
    chapters_completed: int = 0

    @property
    def is_valid(self) -> bool:
        """Whether the successful result satisfies artifact requirements."""
        return (
            not self.chapter_notes
            and self.missing_packaged_output is None
            and self.packaged_output_note is None
        )


def format_failure_notes(notes: list[str], *, limit: int = 3) -> str:
    """Join failure notes for display, truncating with an overflow suffix."""
    if not notes:
        return ""
    summary = "; ".join(notes[:limit])
    overflow = len(notes) - limit
    if overflow > 0:
        summary += f"; ... (+{overflow} more)"
    return summary


def _path_exists(path: Any) -> bool:
    exists = getattr(path, "exists", None)
    if not callable(exists) or not bool(exists()):
        return False
    is_file = getattr(path, "is_file", None)
    if callable(is_file):
        return bool(is_file())
    return True


def _path_missing_if_checkable(path: Any) -> bool:
    exists = getattr(path, "exists", None)
    if not callable(exists):
        return False
    if not bool(exists()):
        return True
    is_file = getattr(path, "is_file", None)
    return callable(is_file) and not bool(is_file())


def select_packaging_audio_input(result: Any) -> Optional[Path]:
    """Return the preferred existing chapter audio path for packaging."""
    aac_path = getattr(result, "aac_path", None)
    wav_path = getattr(result, "wav_path", None)
    mp3_path = getattr(result, "mp3_path", None)
    if _path_exists(aac_path):
        return aac_path
    if _path_exists(wav_path):
        return wav_path
    if _path_exists(mp3_path):
        return mp3_path
    return None


def select_packaging_audio_inputs(chapter_results: list[Any]) -> list[Path]:
    """Return homogeneous preferred chapter audio inputs for packaging."""
    if chapter_results and all(
        _path_exists(getattr(result, "aac_path", None)) for result in chapter_results
    ):
        return [getattr(result, "aac_path") for result in chapter_results]

    if chapter_results and all(
        _path_exists(getattr(result, "wav_path", None)) for result in chapter_results
    ):
        return [getattr(result, "wav_path") for result in chapter_results]

    if chapter_results and all(
        _path_exists(getattr(result, "mp3_path", None)) for result in chapter_results
    ):
        return [getattr(result, "mp3_path") for result in chapter_results]

    return []


def _chapter_note(result: Any, reason: str) -> str:
    return f"chapter {result.chapter_number} ({result.chapter_title}): {reason}"


def _packaging_missing_audio_reason(result: Any) -> str:
    wav_path = getattr(result, "wav_path", None)
    mp3_path = getattr(result, "mp3_path", None)
    if wav_path is not None:
        return f"missing WAV file at {wav_path}"
    if mp3_path is not None:
        return f"missing MP3 file at {mp3_path}"
    return "missing chapter audio path"


def _preflight_missing_audio_reason(result: Any) -> Optional[str]:
    mp3_path = getattr(result, "mp3_path", None)
    if mp3_path is None:
        return "missing chapter audio output"
    if _path_missing_if_checkable(mp3_path):
        return f"missing MP3 file at {mp3_path}"
    return None


def _repair_missing_audio_reason(result: Any) -> str:
    wav_path = getattr(result, "wav_path", None)
    mp3_path = getattr(result, "mp3_path", None)
    if wav_path is not None and _path_missing_if_checkable(wav_path):
        return f"missing WAV file at {wav_path}"
    if mp3_path is not None and _path_missing_if_checkable(mp3_path):
        return f"missing MP3 file at {mp3_path}"
    return "missing chapter audio output"


def summarize_packaging_input_failures(chapter_results: list[Any]) -> list[str]:
    """Return notes for chapters that are not valid package inputs."""
    notes: list[str] = []
    for result in chapter_results:
        if getattr(result, "error", None):
            notes.append(_chapter_note(result, str(result.error)))
            continue
        if select_packaging_audio_input(result) is not None:
            continue
        notes.append(_chapter_note(result, _packaging_missing_audio_reason(result)))
    return notes


def summarize_repair_input_failures(chapter_results: list[Any]) -> list[str]:
    """Return notes for repaired chapters that cannot be repackaged."""
    notes: list[str] = []
    for result in chapter_results:
        if getattr(result, "error", None):
            notes.append(_chapter_note(result, str(result.error)))
            continue
        if select_packaging_audio_input(result) is not None:
            continue
        notes.append(_chapter_note(result, _repair_missing_audio_reason(result)))
    return notes


def summarize_chapter_mp3_artifact_failures(chapter_results: list[Any]) -> list[str]:
    """Return notes for chapters missing required MP3 artifacts."""
    notes: list[str] = []
    for result in chapter_results:
        reason: Optional[str] = None
        mp3_path = getattr(result, "mp3_path", None)
        if getattr(result, "error", None):
            reason = str(result.error)
        elif mp3_path is None:
            reason = "missing MP3 output"
        elif _path_missing_if_checkable(mp3_path):
            reason = f"missing MP3 file at {mp3_path}"

        if reason is not None:
            notes.append(_chapter_note(result, reason))
    return notes


def summarize_chapter_errors(chapter_results: list[Any]) -> list[str]:
    """Return notes for chapters that report an error."""
    return [
        _chapter_note(result, str(result.error))
        for result in chapter_results
        if getattr(result, "error", None)
    ]


def summarize_incomplete_chapters(
    chapter_results: list[ChapterResult],
) -> list[str]:
    """Return notes for chapters that failed or lack usable package audio."""
    return summarize_packaging_input_failures(chapter_results)


def validate_conversion_success_artifacts(
    pipeline_result: Any,
    *,
    emit_chapter_mp3_artifacts: bool,
    require_packaged_output: bool = False,
) -> ConversionArtifactValidation:
    """Validate artifacts for a pipeline result that reported success."""
    chapters = list(getattr(pipeline_result, "chapters", []))
    output_path = getattr(pipeline_result, "output_path", None)
    has_packaged_output = output_path is not None
    require_chapter_mp3 = (
        bool(emit_chapter_mp3_artifacts)
        or not has_packaged_output
    )

    chapter_notes = (
        summarize_chapter_mp3_artifact_failures(chapters)
        if require_chapter_mp3
        else summarize_chapter_errors(chapters)
    )
    chapters_completed = sum(
        1
        for chapter in chapters
        if not getattr(chapter, "error", None)
        and (
            not require_chapter_mp3
            or (
                getattr(chapter, "mp3_path", None) is not None
                and not _path_missing_if_checkable(getattr(chapter, "mp3_path", None))
            )
        )
    )

    missing_packaged_output = None
    packaged_output_note = None
    if output_path is None and require_packaged_output:
        packaged_output_note = "missing packaged output"
    elif output_path is not None and not Path(output_path).is_file():
        missing_packaged_output = Path(output_path)
        packaged_output_note = f"missing packaged output at {missing_packaged_output}"

    return ConversionArtifactValidation(
        chapter_notes=chapter_notes,
        missing_packaged_output=missing_packaged_output,
        packaged_output_note=packaged_output_note,
        chapters_completed=chapters_completed,
    )


_CHAPTER_SET_CONTEXT_LABELS: dict[str, tuple[str, str]] = {
    "repair": ("source", "stored"),
}


def _normalize_chapter_title(title: Any) -> str:
    return " ".join(str(title or "").split()).casefold()


def _repair_expected_number(chapter: Any, index: int) -> int:
    return int(getattr(chapter, "number", index + 1))


def _repair_actual_number(chapter: Any, _index: int) -> int:
    return int(getattr(chapter, "chapter_number", getattr(chapter, "number", 0)))


def validate_chapter_sets(
    expected: list[Any],
    actual: list[Any],
    *,
    context: str,
) -> list[str]:
    """
    Validate that two chapter collections align by number and title.

    For ``context="repair"``, ``expected`` is the ingested source document and
    ``actual`` is the persisted library chapter set. Returns human-readable
    mismatch notes; an empty list means the sets are structurally compatible.
    """
    if context not in _CHAPTER_SET_CONTEXT_LABELS:
        raise ValueError(f"unsupported chapter-set context: {context!r}")

    expected_label, actual_label = _CHAPTER_SET_CONTEXT_LABELS[context]
    if context == "repair":
        expected_number = _repair_expected_number
        actual_number = _repair_actual_number
    else:
        raise ValueError(f"unsupported chapter-set context: {context!r}")

    expected_by_number: dict[int, Any] = {}
    duplicate_expected_numbers: list[int] = []
    for index, chapter in enumerate(expected):
        chapter_number = expected_number(chapter, index)
        if chapter_number in expected_by_number:
            duplicate_expected_numbers.append(chapter_number)
        expected_by_number[chapter_number] = chapter

    actual_numbers: set[int] = set()
    duplicate_actual_numbers: list[int] = []
    for index, chapter in enumerate(actual):
        chapter_number = actual_number(chapter, index)
        if chapter_number in actual_numbers:
            duplicate_actual_numbers.append(chapter_number)
        actual_numbers.add(chapter_number)

    expected_numbers = set(expected_by_number)
    errors: list[str] = []
    if duplicate_expected_numbers:
        duplicate_numbers = ", ".join(
            str(number) for number in sorted(set(duplicate_expected_numbers))
        )
        errors.append(f"duplicate {expected_label} chapter rows: {duplicate_numbers}")
    if duplicate_actual_numbers:
        duplicate_numbers = ", ".join(
            str(number) for number in sorted(set(duplicate_actual_numbers))
        )
        errors.append(f"duplicate {actual_label} chapter rows: {duplicate_numbers}")

    missing_actual_numbers = sorted(expected_numbers - actual_numbers)
    if missing_actual_numbers:
        missing_numbers = ", ".join(str(number) for number in missing_actual_numbers)
        errors.append(
            f"missing {actual_label} chapter rows for {expected_label} chapters: {missing_numbers}"
        )

    missing_expected_numbers = sorted(actual_numbers - expected_numbers)
    if missing_expected_numbers:
        missing_numbers = ", ".join(str(number) for number in missing_expected_numbers)
        errors.append(
            f"missing {expected_label} chapter rows for {actual_label} chapters: {missing_numbers}"
        )

    for index, chapter in enumerate(actual):
        chapter_number = actual_number(chapter, index)
        expected_chapter = expected_by_number.get(chapter_number)
        if expected_chapter is None:
            continue
        actual_title = _normalize_chapter_title(getattr(chapter, "title", ""))
        expected_title = _normalize_chapter_title(getattr(expected_chapter, "title", ""))
        if not actual_title or not expected_title or actual_title == expected_title:
            continue
        errors.append(
            "title mismatch for chapter "
            f"{chapter_number}: expected {getattr(expected_chapter, 'title', '')!r}, "
            f"got {getattr(chapter, 'title', '')!r}"
        )

    return errors


def validate_chapter_result_structure(
    expected_chapters: list[Any],
    chapter_results: list[ChapterResult],
) -> tuple[list[ChapterResult], list[str]]:
    """
    Ensure dispatched results represent exactly the ingested chapter set.

    Results may arrive out of order from parallel workers; when the chapter
    numbers and titles match, return them in document order for packaging.
    """
    expected_by_number: dict[Any, Any] = {}
    expected_order: dict[Any, int] = {}
    expected_duplicate_numbers: list[Any] = []
    for idx, chapter in enumerate(expected_chapters):
        number = getattr(chapter, "number", idx + 1)
        if number in expected_by_number:
            expected_duplicate_numbers.append(number)
            continue
        expected_by_number[number] = chapter
        expected_order[number] = idx

    results_by_number: dict[Any, list[ChapterResult]] = {}
    for result in chapter_results:
        results_by_number.setdefault(result.chapter_number, []).append(result)

    notes: list[str] = []
    for number in expected_duplicate_numbers:
        notes.append(f"duplicate ingested chapter number {number}")

    for number, results in results_by_number.items():
        if len(results) > 1:
            notes.append(f"duplicate chapter {number}")

    for number, chapter in expected_by_number.items():
        if number not in results_by_number:
            title = str(getattr(chapter, "title", "")).strip()
            label = f"chapter {number} ({title})" if title else f"chapter {number}"
            notes.append(f"missing {label}")

    for number in results_by_number:
        if number not in expected_by_number:
            notes.append(f"unexpected chapter {number}")

    for number, chapter in expected_by_number.items():
        results = results_by_number.get(number, [])
        if len(results) != 1:
            continue
        expected_title = str(getattr(chapter, "title", "")).strip()
        actual_title = str(results[0].chapter_title).strip()
        if expected_title and actual_title and expected_title != actual_title:
            notes.append(
                f"title mismatch for chapter {number}: "
                f"expected {expected_title!r}, got {actual_title!r}"
            )

    if notes:
        return chapter_results, notes

    ordered_results = sorted(
        chapter_results,
        key=lambda result: expected_order[result.chapter_number],
    )
    return ordered_results, []
