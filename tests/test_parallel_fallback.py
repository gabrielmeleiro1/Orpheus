"""
Test parallel fallback behavior in pipeline orchestrator.
"""

from __future__ import annotations

import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest


PROJECT_ROOT = Path(__file__).parent.parent
TESTS_DIR = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(TESTS_DIR))

from tests._factories_group2_pipeline import PermissiveMemoryPolicy as _PermissiveMemoryPolicy

from modules.pipeline.orchestrator import (
    ChapterResult,
    ConversionPipeline,
    PipelineConfig,
)
from modules.pipeline.parallel import ParallelPipelineOrchestrator, ParallelConfig
from modules.pipeline.pool_manager import SharedWorkerPoolManager
from modules.pipeline.worker_errors import (
    WorkerRuntimeLoadError,
    WorkerRuntimeMemoryError,
    WorkerRuntimeUnavailableError,
)
from worker_pool_test_helpers import (
    NeverCompletingChapterExecutor,
    PendingStartupProbeExecutor,
    ProgressCapture,
    RecordMemoryPolicy,
    assert_pending_futures_cancelled,
    make_failing_probe_executor,
    make_startup_status,
    reset_executor_class_state,
    wait_for_executor_chapter_submits,
)


def _cleaner_trace(text, cleaned_text):
    return SimpleNamespace(
        original_text=text,
        rule_based_text=text,
        cleaned_text=cleaned_text,
        mode="llm_success",
        llm_output=None,
        llm_error=None,
    )


def _clean_probe_chapter(tmp_path, cleaner, *, backend="api", progress_callback=None):
    cfg = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        cleaner_backend=backend,
        save_cleaning_debug=False,
    )
    pipeline = ConversionPipeline(config=cfg, progress_callback=progress_callback)
    pipeline.stages._chapter_processor.get_text_cleaner_for_chapter = lambda: cleaner  # type: ignore[method-assign]
    chapter = _Chapter(number=1, title="One", content="alpha")
    return pipeline.stages._chapter_processor.clean_chapter_content(
        chapter=chapter,
        chapter_idx=0,
        total_chapters=1,
        chunks=["## alpha", "## beta"],
        output_dir=tmp_path / "chapters",
    )


@dataclass
class _Chapter:
    number: int
    title: str
    content: str


class _SequentialProbePipeline(ConversionPipeline):
    def __init__(self, config: PipelineConfig):
        super().__init__(config=config)
        self.runtime_flags_seen: list[bool] = []
        self.stages.process_chapter = self.stage_process_chapter
        self.stages.process_chapters_sequential = self.stage_process_chapters_sequential
        self.stages._parallel_runner.process_chapter = self.stage_process_chapter
        self.stages._parallel_runner.process_chapters_sequential = (
            self.stage_process_chapters_sequential
        )

    def stage_process_chapters_sequential(
        self, chapters: list, output_dir: Path
    ) -> list[ChapterResult]:
        return [
            self.stage_process_chapter(
                chapter=chapter,
                chapter_idx=index,
                total_chapters=len(chapters),
                output_dir=output_dir,
            )
            for index, chapter in enumerate(chapters)
        ]

    def stage_process_chapter(
        self,
        chapter,
        chapter_idx: int,
        total_chapters: int,
        output_dir: Path,
    ) -> ChapterResult:
        _ = chapter_idx
        _ = total_chapters
        _ = output_dir
        self.runtime_flags_seen.append(self._run_state.parallel_execution_active)  # noqa: SLF001
        return ChapterResult(
            chapter_number=chapter.number,
            chapter_title=chapter.title,
            duration_ms=1000,
        )


class _SharedManagerProbePipeline(_SequentialProbePipeline):
    def __init__(self, config: PipelineConfig):
        if config.memory_policy is None:
            config.memory_policy = _PermissiveMemoryPolicy()
        super().__init__(config=config)


class _ExplodingSharedManager:
    def run_parallel(self, **kwargs):  # noqa: ANN003
        _ = kwargs
        raise RuntimeError("simulated shared-manager failure")


class _AllErrorSharedManager:
    def run_parallel(self, **kwargs):  # noqa: ANN003
        chapters = kwargs["chapters"]
        return [
            ChapterResult(
                chapter_number=chapter.number,
                chapter_title=chapter.title,
                error="simulated chapter failure",
            )
            for chapter in chapters
        ]


class _InfrastructureErrorSharedManager:
    def run_parallel(self, **kwargs):  # noqa: ANN003
        chapters = kwargs["chapters"]
        return [
            ChapterResult(
                chapter_number=chapter.number,
                chapter_title=chapter.title,
                error="bad value(s) in fds_to_keep",
            )
            for chapter in chapters
        ]


class _MemoryErrorSharedManager:
    def run_parallel(self, **kwargs):  # noqa: ANN003
        chapters = kwargs["chapters"]
        return [
            ChapterResult(
                chapter_number=chapter.number,
                chapter_title=chapter.title,
                error="MLX out of memory while allocating Metal buffer",
            )
            for chapter in chapters
        ]


class _MixedResultSharedManager:
    def run_parallel(self, **kwargs):  # noqa: ANN003
        chapters = kwargs["chapters"]
        return [
            ChapterResult(
                chapter_number=chapters[0].number,
                chapter_title=chapters[0].title,
                duration_ms=1,
            ),
            ChapterResult(
                chapter_number=chapters[1].number,
                chapter_title=chapters[1].title,
                error="chapter content invalid",
            ),
        ]


class _ErrorOnlyManager:
    """Shared-manager stub that reports only chapter-shaped errors."""

    def __init__(self, max_workers: int, available_vram_gb: float):  # noqa: ARG002
        self.max_workers = max_workers
        self.available_vram_gb = available_vram_gb

    def run_parallel(self, **kwargs):  # noqa: ANN003
        chapters = kwargs["chapters"]
        return [
            ChapterResult(
                chapter_number=chapter.number,
                chapter_title=chapter.title,
                error="simulated worker failure",
            )
            for chapter in chapters
        ]

    def shutdown(self):
        return None


class _AdaptiveGuardProbePipeline(ConversionPipeline):
    def __init__(self, config: PipelineConfig):
        super().__init__(config=config)
        self.used_parallel = False
        self.used_sequential = False
        self.stages.ingest = self.stage_ingest
        self.stages.save_intermediate_source = self.stage_save_intermediate_source
        self.stages.warmup_models = self.stage_warmup_models
        self.stages.process_chapters_parallel = self.stage_process_chapters_parallel
        self.stages.process_chapters_sequential = self.stage_process_chapters_sequential
        self.stages.package = self.stage_package

    @staticmethod
    def _complete_results(chapters: list, output_dir: Path) -> list[ChapterResult]:
        output_dir.mkdir(parents=True, exist_ok=True)
        results: list[ChapterResult] = []
        for chapter in chapters:
            mp3_path = output_dir / f"chapter_{chapter.number:02d}.mp3"
            mp3_path.write_bytes(b"mp3")
            results.append(
                ChapterResult(
                    chapter_number=chapter.number,
                    chapter_title=chapter.title,
                    mp3_path=mp3_path,
                    duration_ms=1,
                )
            )
        return results

    def stage_ingest(self, source_path: Path):  # noqa: ARG002
        chapters = [
            _Chapter(number=1, title="One", content="tiny"),
            _Chapter(number=2, title="Two", content="tiny"),
            _Chapter(number=3, title="Three", content="tiny"),
        ]
        return SimpleNamespace(
            title="Tiny Book",
            author="Author",
            raw_markdown="# Tiny Book",
            chapters=chapters,
        )

    def stage_save_intermediate_source(self, document, output_dir: Path):  # noqa: ARG002
        return None

    def stage_warmup_models(self) -> None:
        return None

    def stage_process_chapters_parallel(
        self, chapters: list, output_dir: Path
    ) -> list[ChapterResult]:
        self.used_parallel = True
        return self._complete_results(chapters, output_dir)

    def stage_process_chapters_sequential(
        self, chapters: list, output_dir: Path
    ) -> list[ChapterResult]:
        self.used_sequential = True
        return self._complete_results(chapters, output_dir)

    def stage_package(
        self,
        chapter_results: list[ChapterResult],  # noqa: ARG002
        title: str,  # noqa: ARG002
        author: str | None,  # noqa: ARG002
        output_dir: Path,
    ) -> Path:
        output_path = output_dir / "tiny.m4b"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake")
        return output_path


def _test_parallel_path_forwards_total_chapters_to_worker_partial() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=2,
            memory_policy=_PermissiveMemoryPolicy(),
        )

        pipeline = ConversionPipeline(config=cfg)
        pipeline.stages.process_chapters_sequential = (
            lambda chapters, output_dir: (_ for _ in ()).throw(
                AssertionError("parallel path unexpectedly fell back to sequential mode")
            )
        )
        chapters = [
            _Chapter(number=1, title="One", content="alpha"),
            _Chapter(number=2, title="Two", content="beta"),
        ]

        def fake_process_parallel(  # noqa: ANN001, ARG001
            self,
            *,
            chapters,
            process_func,
            progress_callback,
            cancel_requested=None,
        ):
            _ = cancel_requested
            assert process_func.keywords["total_chapters"] == 2
            assert len(chapters) == 2
            progress_callback(1, 2, "fake progress")
            return [
                ChapterResult(chapter_number=1, chapter_title="One", duration_ms=1000),
                ChapterResult(chapter_number=2, chapter_title="Two", duration_ms=1000),
            ]

        with patch(
            "modules.pipeline.parallel.ParallelPipelineOrchestrator.process_chapters_parallel",
            new=fake_process_parallel,
        ):
            results = pipeline.stages.process_chapters_parallel(chapters, cfg.output_dir)

        assert [result.chapter_number for result in results] == [1, 2]

    print("✓ parallel path forwards total chapters to worker partial")


def test_parallel_progress_forwards_worker_performance_snapshot() -> None:
    progress_payloads = []

    def _pipeline_progress(
        stage,  # noqa: ANN001
        chapter_idx: int,
        total_chapters: int,
        chunk_idx: int,
        total_chunks: int,
        message: str,
        eta: float | None,
        performance: dict | None = None,
    ) -> None:
        _ = stage, chapter_idx, total_chapters, chunk_idx, total_chunks, message, eta
        if performance:
            progress_payloads.append(performance)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=2,
        )
        pipeline = _SharedManagerProbePipeline(config=cfg)
        pipeline.progress_callback = _pipeline_progress
        chapters = [
            _Chapter(number=1, title="One", content="alpha"),
            _Chapter(number=2, title="Two", content="beta"),
        ]

        def fake_process_parallel(  # noqa: ANN001, ARG001
            self,
            *,
            chapters,
            process_func,
            progress_callback,
            cancel_requested=None,
        ):
            _ = cancel_requested
            progress_callback(
                1,
                len(chapters),
                "Queued chapter 1/2",
                {
                    "pipeline_mode": "parallel",
                    "active_workers": 2,
                    "max_workers": 2,
                    "queued_chapters": 0,
                    "running_chapters": 2,
                    "completed_chapters": 0,
                    "failed_chapters": 0,
                    "active_chapter_titles": ["One", "Two"],
                },
            )
            return [
                ChapterResult(chapter_number=1, chapter_title="One", duration_ms=1000),
                ChapterResult(chapter_number=2, chapter_title="Two", duration_ms=1000),
            ]

        with patch(
            "modules.pipeline.parallel.ParallelPipelineOrchestrator.process_chapters_parallel",
            new=fake_process_parallel,
        ):
            pipeline.stages.process_chapters_parallel(chapters, cfg.output_dir)

    assert any(
        payload.get("active_workers") == 2
        and payload.get("active_chapter_titles") == ["One", "Two"]
        for payload in progress_payloads
    )


def _test_shared_manager_exception_retries_sequential_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=2,
            shared_worker_pool_manager=_ExplodingSharedManager(),
        )
        pipeline = _SharedManagerProbePipeline(config=cfg)
        chapters = [
            _Chapter(number=1, title="One", content="alpha"),
            _Chapter(number=2, title="Two", content="beta"),
        ]

        results = pipeline.stages.process_chapters_parallel(chapters, cfg.output_dir)

        assert len(results) == 2
        assert all(result.error is None for result in results)
        assert pipeline.runtime_flags_seen == [False, False]

    print("✓ shared manager exception retries sequential mode")


def _test_shared_manager_all_errors_retry_sequential_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=2,
            shared_worker_pool_manager=_AllErrorSharedManager(),
        )
        pipeline = _SharedManagerProbePipeline(config=cfg)
        chapters = [
            _Chapter(number=1, title="One", content="alpha"),
            _Chapter(number=2, title="Two", content="beta"),
        ]

        results = pipeline.stages.process_chapters_parallel(chapters, cfg.output_dir)

        assert len(results) == 2
        assert all(result.error is None for result in results)
        assert pipeline.runtime_flags_seen == [False, False]

    print("✓ shared manager all-error results retry sequential mode")


def test_parallel_cancellation_interrupts_long_running_pool_wait() -> None:
    reset_executor_class_state(NeverCompletingChapterExecutor)
    fallback_progress: list[dict] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=2,
            memory_policy=_PermissiveMemoryPolicy(),
            shared_worker_pool_manager=SharedWorkerPoolManager(max_workers=2),
        )
        pipeline = _SharedManagerProbePipeline(config=cfg)
        pipeline._notify_progress = lambda **kwargs: fallback_progress.append(kwargs)  # noqa: SLF001
        chapters = [
            _Chapter(number=1, title="One", content="alpha"),
            _Chapter(number=2, title="Two", content="beta"),
            _Chapter(number=3, title="Three", content="gamma"),
        ]
        results: list[ChapterResult] = []

        def _run_pipeline() -> None:
            with patch(
                "modules.pipeline.pool_manager.ProcessPoolExecutor",
                NeverCompletingChapterExecutor,
            ):
                results.extend(
                    pipeline.stages.process_chapters_parallel(chapters, cfg.output_dir)
                )

        thread = threading.Thread(target=_run_pipeline)
        thread.start()

        executor = wait_for_executor_chapter_submits(NeverCompletingChapterExecutor)

        pipeline.cancel()
        thread.join(timeout=2)
        cfg.shared_worker_pool_manager.shutdown(timeout_seconds=0.0)

    assert not thread.is_alive()
    assert results == []
    assert pipeline.runtime_flags_seen == []
    assert fallback_progress == []
    assert_pending_futures_cancelled(executor)


def test_cancelled_parallel_results_do_not_retry_sequential_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=2,
            shared_worker_pool_manager=_AllErrorSharedManager(),
        )
        pipeline = _SharedManagerProbePipeline(config=cfg)
        chapters = [
            _Chapter(number=1, title="One", content="alpha"),
            _Chapter(number=2, title="Two", content="beta"),
        ]
        fallback_progress = []
        pipeline._notify_progress = lambda **kwargs: fallback_progress.append(kwargs)  # noqa: SLF001
        pipeline.cancel()

        results = pipeline.stages.process_chapters_parallel(chapters, cfg.output_dir)

        assert results == []
        assert pipeline.runtime_flags_seen == []
        assert fallback_progress == []


def test_all_worker_failure_emits_fallback_performance_snapshot() -> None:
    progress_payloads = []

    def _progress_callback(
        stage,  # noqa: ANN001
        chapter_idx: int,
        total_chapters: int,
        chunk_idx: int,
        total_chunks: int,
        message: str,
        eta: float | None,
        performance: dict | None = None,
    ) -> None:
        _ = stage, chapter_idx, total_chapters, chunk_idx, total_chunks, message, eta
        if performance:
            progress_payloads.append(performance)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=2,
            shared_worker_pool_manager=_AllErrorSharedManager(),
        )
        pipeline = _SharedManagerProbePipeline(config=cfg)
        pipeline.progress_callback = _progress_callback
        chapters = [
            _Chapter(number=1, title="One", content="alpha"),
            _Chapter(number=2, title="Two", content="beta"),
        ]

        pipeline.stages.process_chapters_parallel(chapters, cfg.output_dir)

    assert any(
        payload.get("fallback_reason") == "all_chapter_workers_failed"
        for payload in progress_payloads
    )


def _startup_probe_chapters() -> list[_Chapter]:
    return [
        _Chapter(number=1, title="One", content="alpha"),
        _Chapter(number=2, title="Two", content="beta"),
    ]


def _make_startup_probe_pipeline(
    failure_status,
    *,
    memory_policy=None,
    config_kwargs: dict | None = None,
    progress: ProgressCapture | None = None,
):
    config_kwargs = dict(config_kwargs or {})
    tmp_path_ctx = tempfile.TemporaryDirectory()
    tmp_path = Path(tmp_path_ctx.name)
    cfg = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        enable_parallel=True,
        max_parallel_chapters=2,
        memory_policy=memory_policy or _PermissiveMemoryPolicy(),
        shared_worker_pool_manager=SharedWorkerPoolManager(max_workers=2),
        **config_kwargs,
    )
    pipeline = _SharedManagerProbePipeline(config=cfg)
    if progress is not None:
        pipeline.progress_callback = progress.callback
    chapters = _startup_probe_chapters()
    failing_executor = make_failing_probe_executor(failure_status)
    return tmp_path_ctx, cfg, pipeline, chapters, failing_executor


def _run_parallel_with_failing_startup_probe(
    failure_status,
    *,
    memory_policy=None,
    config_kwargs: dict | None = None,
    progress: ProgressCapture | None = None,
):
    tmp_path_ctx, cfg, pipeline, chapters, failing_executor = _make_startup_probe_pipeline(
        failure_status,
        memory_policy=memory_policy,
        config_kwargs=config_kwargs,
        progress=progress,
    )
    with patch("modules.pipeline.pool_manager.ProcessPoolExecutor", failing_executor):
        results = pipeline.stages.process_chapters_parallel(chapters, cfg.output_dir)

    cfg.shared_worker_pool_manager.shutdown()
    tmp_path_ctx.cleanup()
    return pipeline, results


@pytest.mark.parametrize(
    ("failure_status", "expected_exception", "match"),
    [
        pytest.param(
            make_startup_status(
                error_type="WorkerRuntimeUnavailableError",
                message="cuda driver unavailable",
                provider="cuda",
                device="cuda:0",
            ),
            WorkerRuntimeUnavailableError,
            "cuda/cuda:0",
            id="unavailable",
        ),
        pytest.param(
            make_startup_status(
                error_type="WorkerRuntimeLoadError",
                message="kokoro runtime load failed",
                provider="mlx",
                device=None,
            ),
            WorkerRuntimeLoadError,
            "mlx",
            id="load",
        ),
        pytest.param(
            make_startup_status(
                error_type="WorkerRuntimeMemoryError",
                message="CUDA out of memory during startup",
                provider="cuda",
                device="cuda:0",
            ),
            WorkerRuntimeMemoryError,
            "cuda/cuda:0",
            id="memory_manual_mode",
        ),
    ],
)
def test_worker_startup_fail_fast_via_real_probes(
    failure_status,
    expected_exception,
    match: str,
) -> None:
    progress = ProgressCapture()
    config_kwargs = (
        {"parallel_worker_mode": "manual"}
        if failure_status.error_type == "WorkerRuntimeMemoryError"
        else {}
    )
    tmp_path_ctx, cfg, pipeline, chapters, failing_executor = _make_startup_probe_pipeline(
        failure_status,
        config_kwargs=config_kwargs,
        progress=progress,
    )

    with patch("modules.pipeline.pool_manager.ProcessPoolExecutor", failing_executor):
        with pytest.raises(expected_exception, match=match):
            pipeline.stages.process_chapters_parallel(chapters, cfg.output_dir)

    cfg.shared_worker_pool_manager.shutdown()
    tmp_path_ctx.cleanup()

    assert pipeline.runtime_flags_seen == []
    progress.assert_fail_fast_startup_observability(
        provider=failure_status.provider,
        device=failure_status.device,
        error_type=failure_status.error_type or "",
    )


def test_worker_startup_memory_error_degrades_via_real_probes() -> None:
    progress = ProgressCapture()
    memory_policy = RecordMemoryPolicy()
    failure_status = make_startup_status(
        error_type="WorkerRuntimeMemoryError",
        message="MLX out of memory while allocating Metal buffer",
        provider="mlx",
        device=None,
    )

    pipeline, results = _run_parallel_with_failing_startup_probe(
        failure_status,
        memory_policy=memory_policy,
        config_kwargs={"tts_engine": "other", "cleaner_backend": "api"},
        progress=progress,
    )

    assert memory_policy.errors_recorded == 1
    assert pipeline._selected_parallel_chapters == 1  # noqa: SLF001
    assert len(results) == 2
    assert all(result.error is None for result in results)
    assert pipeline.runtime_flags_seen == [False, False]
    assert any(
        payload.get("fallback_reason") == "memory_pressure"
        for payload in progress.payloads
    )


def test_worker_startup_infrastructure_error_retries_via_real_probes() -> None:
    progress = ProgressCapture()
    failure_status = make_startup_status(
        error_type="WorkerInfrastructureError",
        message="bad value(s) in fds_to_keep",
        provider="mlx",
        device=None,
        phase="worker_init",
    )

    pipeline, results = _run_parallel_with_failing_startup_probe(
        failure_status,
        progress=progress,
    )

    assert len(results) == 2
    assert all(result.error is None for result in results)
    assert pipeline.runtime_flags_seen == [False, False]
    assert any(
        payload.get("fallback_reason") == "worker_infrastructure_error"
        for payload in progress.payloads
    )


def test_worker_startup_timeout_retries_sequential_via_real_pool_boundary() -> None:
    reset_executor_class_state(PendingStartupProbeExecutor)
    progress = ProgressCapture()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        manager = SharedWorkerPoolManager(
            max_workers=2,
            shutdown_timeout_seconds=0.0,
            startup_probe_timeout_seconds=0.01,
        )
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=2,
            shared_worker_pool_manager=manager,
        )
        pipeline = _SharedManagerProbePipeline(config=cfg)
        pipeline.progress_callback = progress.callback
        chapters = _startup_probe_chapters()

        with patch(
            "modules.pipeline.pool_manager.ProcessPoolExecutor",
            PendingStartupProbeExecutor,
        ):
            results = pipeline.stages.process_chapters_parallel(
                chapters,
                cfg.output_dir,
            )

        manager.shutdown()

    assert len(results) == 2
    assert all(result.error is None for result in results)
    assert pipeline.runtime_flags_seen == [False, False]
    assert any(
        payload.get("fallback_reason") == "worker_infrastructure_error"
        for payload in progress.payloads
    )


def test_memory_error_degrades_to_sequential() -> None:
    memory_policy = RecordMemoryPolicy()
    progress = ProgressCapture()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=2,
            shared_worker_pool_manager=_MemoryErrorSharedManager(),
            memory_policy=memory_policy,
            tts_engine="other",
            cleaner_backend="api",
        )
        pipeline = _SharedManagerProbePipeline(config=cfg)
        pipeline.progress_callback = progress.callback
        chapters = [
            _Chapter(number=1, title="One", content="alpha"),
            _Chapter(number=2, title="Two", content="beta"),
        ]

        results = pipeline.stages.process_chapters_parallel(chapters, cfg.output_dir)

    assert memory_policy.errors_recorded == 1
    assert cfg.max_parallel_chapters == 2
    assert cfg.enable_parallel is True
    assert pipeline._parallel_config is not None  # noqa: SLF001
    assert pipeline._parallel_config.max_workers == 2  # noqa: SLF001
    assert pipeline._selected_parallel_chapters == 1  # noqa: SLF001
    assert len(results) == 2
    assert all(result.error is None for result in results)
    assert pipeline.runtime_flags_seen == [False, False]
    assert any(
        payload.get("fallback_reason") == "memory_pressure"
        for payload in progress.payloads
    )


def test_memory_fallback_stops_shared_pool_before_sequential_models(tmp_path, monkeypatch):
    events = []

    class Manager(_MemoryErrorSharedManager):
        def shutdown(self):
            events.append("pool stopped")
            return True

    config = PipelineConfig(
        output_dir=tmp_path / "output", temp_dir=tmp_path / "temp",
        enable_parallel=True, max_parallel_chapters=2,
        shared_worker_pool_manager=Manager(), memory_policy=RecordMemoryPolicy(),
        tts_engine="other", cleaner_backend="api",
    )
    pipeline = _SharedManagerProbePipeline(config)
    runner = pipeline.stages._parallel_runner

    def sequential(chapters, output_dir):
        assert events == ["pool stopped"]
        events.append("sequential models loaded")
        return []

    monkeypatch.setattr(runner, "process_chapters_sequential", sequential)
    runner.process_chapters(
        [_Chapter(1, "One", "alpha"), _Chapter(2, "Two", "beta")], config.output_dir,
    )

    assert events == ["pool stopped", "sequential models loaded"]


def test_single_worker_dispatch_stops_shared_pool_before_warmup(tmp_path, monkeypatch):
    from modules.pipeline.chapter_dispatch import dispatch_chapters

    events = []

    class Manager:
        def shutdown(self):
            events.append("pool stopped")
            return True

    pipeline = ConversionPipeline(PipelineConfig(
        output_dir=tmp_path / "output", temp_dir=tmp_path / "temp",
        enable_parallel=False, shared_worker_pool_manager=Manager(),
    ))
    monkeypatch.setattr(pipeline.stages, "warmup_models", lambda: events.append("warmup"))
    monkeypatch.setattr(pipeline.stages, "process_chapters_sequential", lambda *_args: [])

    dispatch_chapters(pipeline.dispatch_context, [_Chapter(1, "One", "alpha")], tmp_path)

    assert events == ["pool stopped", "warmup"]


def test_parallel_fallback_helper_emits_snapshot_and_runs_sequential(tmp_path) -> None:
    logs: list[tuple[str, str]] = []
    progress_payloads: list[dict] = []
    cfg = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        enable_parallel=True,
        max_parallel_chapters=2,
        memory_policy=_PermissiveMemoryPolicy(),
        tts_engine="other",
        cleaner_backend="api",
    )
    pipeline = ConversionPipeline(
        config=cfg,
        verbose_callback=lambda message, level: logs.append((message, level)),
        progress_callback=lambda stage, chapter_idx, total_chapters, chunk_idx, total_chunks, message, eta, performance=None: (
            progress_payloads.append(performance or {})
        ),
    )
    chapters = [
        _Chapter(number=1, title="One", content="alpha"),
        _Chapter(number=2, title="Two", content="beta"),
    ]
    sequential_results = [
        ChapterResult(chapter_number=1, chapter_title="One", duration_ms=1),
        ChapterResult(chapter_number=2, chapter_title="Two", duration_ms=1),
    ]
    calls: list[tuple[list, Path]] = []

    def _sequential(chapters_arg: list, output_dir_arg: Path) -> list[ChapterResult]:
        calls.append((chapters_arg, output_dir_arg))
        return sequential_results

    pipeline.stages._parallel_runner.process_chapters_sequential = _sequential  # type: ignore[method-assign]
    pipeline._run_state.parallel_execution_active = True  # noqa: SLF001

    results = pipeline.stages._parallel_runner._parallel_fallback(
        chapters,
        tmp_path / "chapters",
        total_chapters=2,
        reason="no_parallel_results",
        message="Parallel fallback: no chapter results returned",
        log_message="[PARALLEL] No chapter results returned; retrying sequential mode",
        failed_count=2,
    )

    assert results == sequential_results
    assert calls == [(chapters, tmp_path / "chapters")]
    assert pipeline._run_state.parallel_execution_active is False  # noqa: SLF001
    assert logs[-1] == (
        "[PARALLEL] No chapter results returned; retrying sequential mode",
        "warning",
    )
    expected_performance = {
        "pipeline_mode": "sequential",
        "fallback_reason": "no_parallel_results",
        "failed_chapters": 2,
    }
    assert {
        key: progress_payloads[-1].get(key) for key in expected_performance
    } == expected_performance


def test_parallel_fallback_helper_honors_cancellation(tmp_path) -> None:
    cfg = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        enable_parallel=True,
        max_parallel_chapters=2,
        memory_policy=_PermissiveMemoryPolicy(),
    )
    pipeline = ConversionPipeline(config=cfg)
    pipeline._run_state.parallel_execution_active = True  # noqa: SLF001
    pipeline.cancel()

    def _sequential(chapters_arg: list, output_dir_arg: Path) -> list[ChapterResult]:
        _ = (chapters_arg, output_dir_arg)
        raise AssertionError("cancelled fallback must not retry sequential mode")

    pipeline.stages._parallel_runner.process_chapters_sequential = _sequential  # type: ignore[method-assign]

    results = pipeline.stages._parallel_runner._parallel_fallback(
        [_Chapter(number=1, title="One", content="alpha")],
        tmp_path / "chapters",
        total_chapters=1,
        reason="parallel_exception",
        message="Parallel fallback: cancelled",
        log_message="[PARALLEL] cancelled",
        failed_count=1,
    )

    assert results == []
    assert pipeline._run_state.parallel_execution_active is True  # noqa: SLF001


def test_parallel_fallback_helper_forwards_safe_mode_reason(tmp_path) -> None:
    progress_payloads: list[dict] = []
    cfg = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        enable_parallel=True,
        max_parallel_chapters=2,
        memory_policy=_PermissiveMemoryPolicy(),
    )
    pipeline = ConversionPipeline(
        config=cfg,
        progress_callback=lambda stage, chapter_idx, total_chapters, chunk_idx, total_chunks, message, eta, performance=None: (
            progress_payloads.append(performance or {})
        ),
    )
    pipeline.stages._parallel_runner.process_chapters_sequential = (  # type: ignore[method-assign]
        lambda chapters_arg, output_dir_arg: []
    )

    pipeline.stages._parallel_runner._parallel_fallback(
        [_Chapter(number=1, title="One", content="alpha")],
        tmp_path / "chapters",
        total_chapters=1,
        reason="memory_pressure",
        message="Parallel fallback: memory pressure detected",
        log_message="[PARALLEL] Memory pressure detected",
        failed_count=1,
        safe_mode_reason="memory pressure error",
    )

    assert progress_payloads[-1].get("safe_mode_reason") == "memory pressure error"


def test_parallel_fallback_helper_wraps_sequential_fallback_failure(
    tmp_path,
) -> None:
    cfg = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        enable_parallel=True,
        max_parallel_chapters=2,
        memory_policy=_PermissiveMemoryPolicy(),
    )
    pipeline = ConversionPipeline(config=cfg)
    worker_results = [
        ChapterResult(chapter_number=1, chapter_title="One", error="worker one"),
        ChapterResult(chapter_number=2, chapter_title="Two", error="worker two"),
        ChapterResult(chapter_number=3, chapter_title="Three", error="worker three"),
        ChapterResult(chapter_number=4, chapter_title="Four", error="worker four"),
    ]

    def _sequential(chapters_arg: list, output_dir_arg: Path) -> list[ChapterResult]:
        _ = (chapters_arg, output_dir_arg)
        raise ValueError("fallback boom")

    pipeline.stages._parallel_runner.process_chapters_sequential = _sequential  # type: ignore[method-assign]

    with pytest.raises(RuntimeError) as exc_info:
        pipeline.stages._parallel_runner._parallel_fallback(
            [_Chapter(number=1, title="One", content="alpha")],
            tmp_path / "chapters",
            total_chapters=1,
            reason="all_chapter_workers_failed",
            message="Parallel fallback: all chapter workers failed",
            log_message="[PARALLEL] All chapter workers failed",
            failed_count=4,
            wrap_reason="all chapter workers failed",
            worker_results=worker_results,
        )

    assert str(exc_info.value) == (
        "Sequential fallback failed after all chapter workers failed: "
        "fallback boom. Worker errors: worker one; worker two; worker three; "
        "... (+1 more)"
    )


def _test_shared_manager_infrastructure_errors_retry_sequential_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=2,
            shared_worker_pool_manager=_InfrastructureErrorSharedManager(),
        )
        pipeline = _SharedManagerProbePipeline(config=cfg)
        chapters = [
            _Chapter(number=1, title="One", content="alpha"),
            _Chapter(number=2, title="Two", content="beta"),
        ]

        results = pipeline.stages.process_chapters_parallel(chapters, cfg.output_dir)

        assert len(results) == 2
        assert all(result.error is None for result in results)
        assert pipeline.runtime_flags_seen == [False, False]

    print("✓ shared manager infrastructure errors retry sequential mode")


def _test_shared_manager_mixed_results_retry_failed_chapters() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=2,
            shared_worker_pool_manager=_MixedResultSharedManager(),
        )
        pipeline = _SharedManagerProbePipeline(config=cfg)
        chapters = [
            _Chapter(number=1, title="One", content="alpha"),
            _Chapter(number=2, title="Two", content="beta"),
        ]

        results = pipeline.stages.process_chapters_parallel(chapters, cfg.output_dir)

        assert len(results) == 2
        assert results[0].error is None
        # Failed chapter was retried sequentially and succeeded
        assert results[1].error is None
        assert results[1].chapter_number == 2
        assert pipeline.runtime_flags_seen == [False]

    print("✓ shared manager mixed results retry failed chapters sequentially")


def _test_parallel_orchestrator_does_not_hang_on_error_only_workers() -> None:
    config = ParallelConfig(max_workers=2, use_processes=False)
    orchestrator = ParallelPipelineOrchestrator(config)
    chapters = [
        _Chapter(number=1, title="One", content="alpha"),
        _Chapter(number=2, title="Two", content="beta"),
    ]

    with patch("modules.pipeline.parallel.SharedWorkerPoolManager", _ErrorOnlyManager):
        results = orchestrator.process_chapters_parallel(
            chapters=chapters,
            process_func=lambda ch, idx, _fm: {"id": idx},  # noqa: ARG005
        )

    assert len(results) == 2
    assert all(isinstance(result, ChapterResult) for result in results)
    assert all(result.error == "simulated worker failure" for result in results)

    print("✓ parallel orchestrator exits when workers only return errors")


def _test_api_backend_prefers_batch_cleaning() -> None:
    class _FakeCleaner:
        def __init__(self):
            self.config = SimpleNamespace(backend="api")
            self.batch_calls = 0
            self.single_calls = 0

        async def clean_batch_async(self, texts):
            self.batch_calls += 1
            return [text.replace("## ", "") for text in texts]

        def clean_with_trace(self, text):
            self.single_calls += 1
            return _cleaner_trace(text, text)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cleaner = _FakeCleaner()
        cleaned, cancelled = _clean_probe_chapter(
            tmp_path, cleaner
        )

        assert not cancelled
        assert cleaned == ["alpha", "beta"]
        assert cleaner.batch_calls == 1
        assert cleaner.single_calls == 0

    print("✓ api cleaner path uses batch cleaning")


def _test_custom_backend_prefers_trace_batch_cleaning() -> None:
    class _FakeCleaner:
        def __init__(self):
            self.config = SimpleNamespace(backend="custom")
            self.batch_calls = 0
            self.single_calls = 0

        async def clean_batch_with_trace_async(self, texts):
            self.batch_calls += 1
            return [
                _cleaner_trace(text, text.replace('## ', ''))
                for text in texts
            ]

        def clean_with_trace(self, text):
            self.single_calls += 1
            return _cleaner_trace(text, text)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cleaner = _FakeCleaner()
        cleaned, cancelled = _clean_probe_chapter(
            tmp_path, cleaner
        )

        assert not cancelled
        assert cleaned == ["alpha", "beta"]
        assert cleaner.batch_calls == 1
        assert cleaner.single_calls == 0

    print("✓ custom cleaner path uses trace batching")


def test_mlx_cleaning_uses_per_chunk_trace_cleaning_for_live_progress() -> None:
    class _FakeCleaner:
        def __init__(self):
            self.config = SimpleNamespace(backend="mlx")
            self.batch_calls = 0
            self.single_calls = 0

        async def clean_batch_with_trace_async(self, texts):
            self.batch_calls += 1
            _ = texts
            raise AssertionError("MLX cleaning should not batch local model calls")

        def clean_with_trace(self, text):
            self.single_calls += 1
            return _cleaner_trace(text, text.replace('## ', ''))

    progress_events = []

    def _capture_progress(stage, chapter_idx, total_chapters, chunk_idx, total_chunks, message, eta):
        _ = (eta,)
        progress_events.append(
            {
                "stage": str(getattr(stage, "value", stage)),
                "chapter_idx": chapter_idx,
                "total_chapters": total_chapters,
                "chunk_idx": chunk_idx,
                "total_chunks": total_chunks,
                "message": message,
            }
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cleaner = _FakeCleaner()
        cleaned, cancelled = _clean_probe_chapter(
            tmp_path, cleaner, backend="mlx", progress_callback=_capture_progress
        )

    assert not cancelled
    assert cleaned == ["alpha", "beta"]
    assert cleaner.batch_calls == 0
    assert cleaner.single_calls == 2
    assert [event["message"] for event in progress_events] == [
        "Chapter 1/1: cleaning chunk 1/2",
        "Chapter 1/1: cleaning chunk 2/2",
    ]
    assert {event["stage"] for event in progress_events} == {"cleaning"}


def _test_batch_cleaning_falls_back_to_per_chunk_when_shape_mismatch() -> None:
    class _FakeCleaner:
        def __init__(self):
            self.config = SimpleNamespace(backend="custom")
            self.batch_calls = 0
            self.single_calls = 0

        async def clean_batch_with_trace_async(self, texts):
            self.batch_calls += 1
            _ = texts
            return []  # force shape mismatch and fallback

        def clean_with_trace(self, text):
            self.single_calls += 1
            return _cleaner_trace(text, text.replace('## ', ''))

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cleaner = _FakeCleaner()
        cleaned, cancelled = _clean_probe_chapter(
            tmp_path, cleaner
        )

        assert not cancelled
        assert cleaned == ["alpha", "beta"]
        assert cleaner.batch_calls == 1
        assert cleaner.single_calls == 2

    print("✓ cleaner batch mismatch falls back to per-chunk mode")


def _test_adaptive_parallel_guard_for_tiny_chapters() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=4,
        )
        pipeline = _AdaptiveGuardProbePipeline(config=cfg)
        source = tmp_path / "tiny.pdf"
        source.write_text("x", encoding="utf-8")

        result = pipeline.convert(source)
        assert result.success
        assert pipeline.used_sequential is True
        assert pipeline.used_parallel is False

    print("✓ adaptive guard keeps tiny chapters sequential")


def test_parallel_fallback() -> bool:
    print("\n" + "=" * 50)
    print("PARALLEL FALLBACK TEST SUITE")
    print("=" * 50 + "\n")

    _test_parallel_path_forwards_total_chapters_to_worker_partial()
    _test_shared_manager_exception_retries_sequential_mode()
    _test_shared_manager_all_errors_retry_sequential_mode()
    _test_shared_manager_infrastructure_errors_retry_sequential_mode()
    _test_shared_manager_mixed_results_retry_failed_chapters()
    _test_parallel_orchestrator_does_not_hang_on_error_only_workers()
    _test_api_backend_prefers_batch_cleaning()
    _test_custom_backend_prefers_trace_batch_cleaning()
    _test_batch_cleaning_falls_back_to_per_chunk_when_shape_mismatch()
    _test_adaptive_parallel_guard_for_tiny_chapters()

    print("\n" + "=" * 50)
    print("ALL PARALLEL FALLBACK TESTS PASSED ✓")
    print("=" * 50 + "\n")
    return True


if __name__ == "__main__":
    success = test_parallel_fallback()
    sys.exit(0 if success else 1)
