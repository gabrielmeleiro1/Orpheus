from __future__ import annotations

import pytest

from modules.app.services.progress_math import (
    build_progress_snapshot,
    compute_overall_progress,
    compute_stage_progress,
    current_counter,
    normalize_progress_stage,
)


@pytest.mark.parametrize(
    ("raw_stage", "expected"),
    [
        ("INGESTING", "ingesting"),
        ("document chunking", "chunking"),
        ("text cleanup", "cleaning"),
        ("synth batch", "synthesizing"),
        ("encoding", "encoding"),
        ("packaging", "packaging"),
        ("complete", "complete"),
        ("custom", "custom"),
    ],
)
def test_normalize_progress_stage_maps_pipeline_labels(raw_stage: str, expected: str) -> None:
    assert normalize_progress_stage(raw_stage) == expected


def test_compute_stage_progress_uses_stage_specific_counters() -> None:
    assert compute_stage_progress(
        stage_key="ingesting",
        chapter_idx=0,
        total_chapters=2,
        chunk_idx=9,
        total_chunks=9,
    ) == 0.5
    assert compute_stage_progress(
        stage_key="cleaning",
        chapter_idx=9,
        total_chapters=9,
        chunk_idx=2,
        total_chunks=8,
    ) == 0.375
    assert compute_stage_progress(
        stage_key="packaging",
        chapter_idx=9,
        total_chapters=9,
        chunk_idx=9,
        total_chunks=9,
    ) == 0.15


def test_compute_overall_progress_weights_chapter_stage_work() -> None:
    progress = compute_overall_progress(
        stage_key="cleaning",
        stage_progress=0.375,
        chapter_idx=1,
        total_chapters=4,
    )

    assert progress == pytest.approx(0.3140625)


def test_build_progress_snapshot_includes_normalized_progress_and_counters() -> None:
    snapshot = build_progress_snapshot(
        stage_value="synthesizing",
        chapter_idx=1,
        total_chapters=4,
        chunk_idx=2,
        total_chunks=8,
    )

    assert snapshot.stage_key == "synthesizing"
    assert snapshot.stage_progress == 0.375
    assert snapshot.overall_progress == pytest.approx(0.3890625)
    assert snapshot.chapter_current == 2
    assert snapshot.chapter_total == 4
    assert snapshot.chunk_current == 3
    assert snapshot.chunk_total == 8


def test_current_counter_is_bounded_for_empty_and_out_of_range_inputs() -> None:
    assert current_counter(0, 0) == 0
    assert current_counter(-10, 3) == 0
    assert current_counter(99, 3) == 3
