"""Progress weighting helpers for conversion events."""

from __future__ import annotations

from dataclasses import dataclass

STAGE_WEIGHTS = {
    "ingesting": 0.05,
    "chunking": 0.10,
    "cleaning": 0.15,
    "synthesizing": 0.55,
    "encoding": 0.10,
    "packaging": 0.05,
}
CHAPTER_STAGE_ORDER = ("chunking", "cleaning", "synthesizing", "encoding")
CHAPTER_STAGE_WEIGHT_TOTAL = 0.90


@dataclass(frozen=True)
class ProgressSnapshot:
    stage_key: str
    stage_progress: float
    overall_progress: float
    chapter_current: int
    chapter_total: int
    chunk_current: int
    chunk_total: int


def clamp_progress(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def normalize_progress_stage(stage_value: str) -> str:
    normalized = str(stage_value or "").strip().lower()
    if "ingest" in normalized:
        return "ingesting"
    if "chunk" in normalized:
        return "chunking"
    if "clean" in normalized:
        return "cleaning"
    if "synth" in normalized:
        return "synthesizing"
    if "encod" in normalized:
        return "encoding"
    if "pack" in normalized:
        return "packaging"
    if normalized == "complete":
        return "complete"
    return normalized


def current_index_progress(current_idx: int, total_items: int) -> float:
    if total_items <= 0:
        return 0.0
    completed_items = current_idx + 1
    return clamp_progress(completed_items / total_items)


def current_counter(current_idx: int, total_items: int) -> int:
    if total_items <= 0:
        return 0
    return min(total_items, max(0, current_idx + 1))


def compute_stage_progress(
    *,
    stage_key: str,
    chapter_idx: int,
    total_chapters: int,
    chunk_idx: int,
    total_chunks: int,
) -> float:
    if stage_key == "complete":
        return 1.0
    if stage_key == "ingesting":
        return current_index_progress(chapter_idx, total_chapters)
    if stage_key in {"cleaning", "synthesizing"}:
        return current_index_progress(chunk_idx, total_chunks)
    if stage_key in {"chunking", "encoding"}:
        return current_index_progress(chapter_idx, total_chapters)
    if stage_key == "packaging":
        return 0.15
    return 0.0


def compute_overall_progress(
    *,
    stage_key: str,
    stage_progress: float,
    chapter_idx: int,
    total_chapters: int,
) -> float:
    stage_progress = clamp_progress(stage_progress)
    if stage_key == "complete":
        return 1.0
    if stage_key == "packaging":
        base = 1.0 - STAGE_WEIGHTS["packaging"]
        return clamp_progress(base + STAGE_WEIGHTS["packaging"] * stage_progress)
    if stage_key == "ingesting":
        return clamp_progress(STAGE_WEIGHTS["ingesting"] * stage_progress)
    if stage_key not in CHAPTER_STAGE_ORDER:
        return 0.0
    if total_chapters <= 0:
        return STAGE_WEIGHTS["ingesting"]

    chapter_number = min(total_chapters, max(1, chapter_idx + 1))
    prior_chapter_work = max(0, chapter_number - 1) * CHAPTER_STAGE_WEIGHT_TOTAL
    current_stage_index = CHAPTER_STAGE_ORDER.index(stage_key)
    current_stage_offset = sum(
        STAGE_WEIGHTS[name] for name in CHAPTER_STAGE_ORDER[:current_stage_index]
    )
    current_stage_work = STAGE_WEIGHTS[stage_key] * stage_progress
    chapter_work = (prior_chapter_work + current_stage_offset + current_stage_work) / total_chapters
    return clamp_progress(STAGE_WEIGHTS["ingesting"] + chapter_work)


def build_progress_snapshot(
    *,
    stage_value: str,
    chapter_idx: int,
    total_chapters: int,
    chunk_idx: int,
    total_chunks: int,
) -> ProgressSnapshot:
    stage_key = normalize_progress_stage(stage_value)
    stage_progress = compute_stage_progress(
        stage_key=stage_key,
        chapter_idx=chapter_idx,
        total_chapters=total_chapters,
        chunk_idx=chunk_idx,
        total_chunks=total_chunks,
    )
    overall_progress = compute_overall_progress(
        stage_key=stage_key,
        stage_progress=stage_progress,
        chapter_idx=chapter_idx,
        total_chapters=total_chapters,
    )
    return ProgressSnapshot(
        stage_key=stage_key,
        stage_progress=stage_progress,
        overall_progress=overall_progress,
        chapter_current=current_counter(chapter_idx, total_chapters),
        chapter_total=max(0, int(total_chapters)),
        chunk_current=current_counter(chunk_idx, total_chunks),
        chunk_total=max(0, int(total_chunks)),
    )
