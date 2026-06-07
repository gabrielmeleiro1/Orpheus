"""
Test App Events Module
======================
Unit tests for typed app event contracts.
"""

import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from modules.app.events import (
    EventType,
    JobState,
    make_log_event,
    make_progress_event,
    make_state_event,
)


def test_events() -> bool:
    print("\n" + "=" * 50)
    print("APP EVENTS TEST SUITE")
    print("=" * 50 + "\n")

    # Progress event clamp and fields
    progress = make_progress_event(
        stage="synthesizing",
        progress=1.7,
        message="chunk done",
        stage_progress=0.42,
        eta=9.5,
    )
    assert progress.event_type == EventType.PROGRESS
    assert progress.progress == 1.0
    assert progress.overall_progress == 1.0
    assert progress.stage_progress == 0.42
    assert progress.stage == "synthesizing"
    assert progress.message == "chunk done"
    assert progress.eta == 9.5
    assert progress.chapter_current == 0
    assert progress.chapter_total == 0
    assert progress.chunk_current == 0
    assert progress.chunk_total == 0
    print("✓ progress event normalization")

    # Progress event subtask counters clamp for UI display
    detailed_progress = make_progress_event(
        stage="cleaning",
        progress=0.4,
        message="cleaning chunk",
        chapter_current=3,
        chapter_total=2,
        chunk_current=-1,
        chunk_total=8,
    )
    assert detailed_progress.chapter_current == 2
    assert detailed_progress.chapter_total == 2
    assert detailed_progress.chunk_current == 0
    assert detailed_progress.chunk_total == 8
    print("✓ progress event subtask counter normalization")

    # Progress event performance payload normalization
    perf_progress = make_progress_event(
        stage="synthesizing",
        progress=0.5,
        message="workers active",
        performance={
            "pipeline_mode": "parallel",
            "active_workers": 3,
            "max_workers": 2,
            "queued_chapters": -1,
            "running_chapters": 4,
            "completed_chapters": 1,
            "failed_chapters": 0,
            "active_chapter_titles": ["One", "", "Two"],
            "cleaner_chunks_per_second": 12.3456,
            "tts_chars_per_second": 99.9,
            "encoder_chapters_per_second": 0.25,
            "elapsed_seconds": 123.456,
            "eta_seconds": 77.7,
            "fallback_reason": "all_chapter_workers_failed",
            "ignored": "value",
        },
    )
    assert perf_progress.performance == {
        "pipeline_mode": "parallel",
        "active_workers": 2,
        "max_workers": 2,
        "queued_chapters": 0,
        "running_chapters": 4,
        "completed_chapters": 1,
        "failed_chapters": 0,
        "active_chapter_titles": ["One", "Two"],
        "cleaner_chunks_per_second": 12.35,
        "tts_chars_per_second": 99.9,
        "encoder_chapters_per_second": 0.25,
        "elapsed_seconds": 123.46,
        "eta_seconds": 77.7,
        "fallback_reason": "all_chapter_workers_failed",
    }
    print("✓ progress event performance payload normalization")

    # Log event level normalization
    log = make_log_event("model loaded", "INFO")
    assert log.event_type == EventType.LOG
    assert log.level == "info"
    assert log.message == "model loaded"
    print("✓ log event normalization")

    # State event creation
    state = make_state_event(JobState.RUNNING, "conv_123", "started")
    assert state.event_type == EventType.STATE
    assert state.state == JobState.RUNNING
    assert state.job_id == "conv_123"
    assert state.message == "started"
    print("✓ state event creation")

    print("\n" + "=" * 50)
    print("ALL APP EVENT TESTS PASSED ✓")
    print("=" * 50 + "\n")
    return True


if __name__ == "__main__":
    success = test_events()
    sys.exit(0 if success else 1)
