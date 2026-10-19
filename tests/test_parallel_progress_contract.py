"""Parallel chapter runs report stages and overall progress like sequential runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from modules.app.services.progress_math import build_progress_snapshot
from modules.pipeline.models import ChapterResult, PipelineConfig
from modules.pipeline.orchestrator import ConversionPipeline
from modules.pipeline.pool_manager import SharedWorkerPoolManager


@dataclass
class _Chapter:
    number: int
    title: str
    content: str


def _finish_chapter(chapter, idx, _manager):
    return ChapterResult(chapter_number=chapter.number, chapter_title=chapter.title, duration_ms=1000)


def _run_parallel(tmp_path: Path, total: int) -> tuple[list, list[str]]:
    events: list = []
    messages: list[str] = []

    def on_progress(stage, chapter_idx, total_chapters, chunk_idx, total_chunks, message, eta=None, performance=None):
        messages.append(message)
        events.append(build_progress_snapshot(
            stage_value=stage.value, chapter_idx=chapter_idx, total_chapters=total_chapters,
            chunk_idx=chunk_idx, total_chunks=total_chunks,
        ))

    config = PipelineConfig(
        output_dir=tmp_path / "output", temp_dir=tmp_path / "temp",
        enable_parallel=True, max_parallel_chapters=2,
    )
    pipeline = ConversionPipeline(config, progress_callback=on_progress)
    chapters = [_Chapter(number=i + 1, title=f"C{i + 1}", content="text") for i in range(total)]

    def thread_pool_run(self, *, chapters, process_func, progress_callback, cancel_requested=None):
        # The real pool manager (thread-backed) drives the callback sequence.
        manager = SharedWorkerPoolManager(max_workers=2)
        manager._use_processes = False
        try:
            return manager.run_parallel(
                chapters=chapters, process_func=_finish_chapter,
                progress_callback=progress_callback, config_fingerprint=("contract",),
                cancel_requested=cancel_requested,
            )
        finally:
            manager.shutdown(timeout_seconds=5.0)

    with patch(
        "modules.pipeline.parallel.ParallelPipelineOrchestrator.process_chapters_parallel",
        new=thread_pool_run,
    ):
        results = pipeline.stages.process_chapters_parallel(chapters, config.output_dir)
    assert len(results) == total
    return events, messages


def test_parallel_progress_follows_the_sequential_chapter_contract(tmp_path):
    total = 6
    events, messages = _run_parallel(tmp_path, total)

    # Worker startup (model loading in every worker) is announced on its own,
    # before any chapter stage is reported.
    assert messages[0].startswith("Starting ") and "(loading models in each)" in messages[0]
    events = events[1:]

    stages = [event.stage_key for event in events]
    assert set(stages) == {"synthesizing", "encoding"}
    overall = [event.overall_progress for event in events]
    assert overall == sorted(overall), overall

    # After k finished chapters the bar sits exactly where a sequential run is
    # once chapter k has been encoded.
    finished = [event for event in events if event.stage_key == "encoding"]
    assert [event.chapter_current for event in finished] == list(range(1, total + 1))
    for done, event in enumerate(finished, start=1):
        sequential = build_progress_snapshot(
            stage_value="encoding", chapter_idx=done - 1, total_chapters=total,
            chunk_idx=0, total_chunks=0,
        )
        assert event.overall_progress == sequential.overall_progress
    assert finished[0].overall_progress > 0.05 + 0.9 * 0.5 / total
    assert finished[-1].overall_progress >= 0.9

    # Bounded: at most two progress events per chapter, queue notices excluded.
    assert len(events) <= 2 * total + 1
