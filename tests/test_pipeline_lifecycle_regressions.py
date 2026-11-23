"""Regression tests for pipeline lifecycle, worker pool and repair behaviour."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from modules.pipeline.book_workspace import prepare_book_workspace
from modules.pipeline.chapter_processor import ChapterProcessor
from modules.pipeline.chapter_runners import process_chapter_worker
from modules.pipeline.model_session import ModelSession
from modules.pipeline.models import (
    ChapterResult,
    PipelineConfig,
    PipelineStage,
    RepairPlan,
)
from modules.pipeline.orchestrator import ConversionPipeline
from modules.pipeline.pool_manager import SharedWorkerPoolManager
from modules.pipeline.repair import RepairRuntime, execute_repair_plan
from modules.pipeline.runtime import PipelineRunState
from modules.pipeline.worker_errors import (
    StartupErrorKind,
    StartupFailureAction,
    classify_worker_startup_error,
    resolve_startup_failure_action,
)
from modules.tts.runtimes.errors import TTSRuntimeMemoryError
from tests._factories_group2_pipeline import PermissiveMemoryPolicy

_WAIT = 5.0


@dataclass
class _Chapter:
    number: int
    title: str
    content: str = "Some chapter text."


def _join_threads_named(name: str) -> None:
    for thread in threading.enumerate():
        if thread.name == name and thread is not threading.current_thread():
            thread.join(timeout=_WAIT)
            assert not thread.is_alive()


# --- Worker pool shutdown -------------------------------------------------


class _HandoffLock:
    """Waiter lock whose first non-blocking acquire loses to a finishing holder."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lost_first = False

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if not blocking and not self._lost_first:
            # The previous holder released and cleared the stop request just
            # after this attempt failed.
            self._lost_first = True
            return False
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()


def test_shutdown_racing_a_finishing_waiter_does_not_leave_cleanup_pending() -> None:
    manager = SharedWorkerPoolManager(max_workers=1)
    manager._shutdown_waiter_lock = _HandoffLock()  # noqa: SLF001

    lock_held = threading.Event()
    release_lock = threading.Event()

    def hold_lifecycle_lock() -> None:
        with manager._lock:  # noqa: SLF001
            lock_held.set()
            release_lock.wait(_WAIT)

    holder = threading.Thread(target=hold_lifecycle_lock)
    holder.start()
    assert lock_held.wait(_WAIT)
    try:
        assert manager.shutdown(timeout_seconds=0.0) is False
        assert manager.cleanup_pending is True
    finally:
        release_lock.set()
        holder.join(_WAIT)

    _join_threads_named("pending-worker-pool-shutdown")
    assert manager.cleanup_pending is False


def test_unexpected_startup_probe_error_detaches_and_stops_the_new_executor() -> None:
    manager = SharedWorkerPoolManager(max_workers=1, shutdown_timeout_seconds=_WAIT)
    manager._use_processes = False  # noqa: SLF001
    started: list[object] = []
    original_start = manager._start_executor_locked  # noqa: SLF001

    def start_and_record(**kwargs):
        original_start(**kwargs)
        started.append(manager._state.executor)  # noqa: SLF001

    def failing_probes() -> None:
        raise OSError("cannot spawn worker")

    with (
        patch.object(manager, "_start_executor_locked", side_effect=start_and_record),
        patch.object(manager, "_run_startup_probes_locked", side_effect=failing_probes),
    ):
        with pytest.raises(OSError, match="cannot spawn worker"):
            manager.ensure_started("fingerprint")

    assert len(started) == 1
    assert manager._state.executor is None  # noqa: SLF001
    assert started[0]._shutdown is True  # noqa: SLF001
    assert manager.cleanup_pending is False
    manager.shutdown(timeout_seconds=0.0)


# --- Idle TTS unload ------------------------------------------------------


class _GatedLock:
    """RLock that parks the timer thread before it enters the idle section."""

    def __init__(self, gated_thread_name: str) -> None:
        self._lock = threading.RLock()
        self._gated_thread_name = gated_thread_name
        self.arrived = threading.Event()
        self.proceed = threading.Event()

    def __enter__(self):
        if threading.current_thread().name == self._gated_thread_name:
            self.arrived.set()
            assert self.proceed.wait(_WAIT)
        self._lock.acquire()
        return self

    def __exit__(self, *exc_info) -> bool:
        self._lock.release()
        return False


def test_idle_unload_that_already_fired_yields_to_a_cancelling_run() -> None:
    engine = MagicMock()
    state = PipelineRunState(tts_engine=engine)
    runtime = SimpleNamespace(
        state=state,
        config=SimpleNamespace(tts_idle_timeout_seconds=0.001),
        is_running=False,
    )
    session = ModelSession(runtime)

    gate = _GatedLock("idle-unload-timer")
    state.tts_idle_lock = gate
    original_timer = threading.Timer

    def named_timer(*args, **kwargs):
        timer = original_timer(*args, **kwargs)
        timer.name = "idle-unload-timer"
        return timer

    with patch("modules.pipeline.model_session.threading.Timer", side_effect=named_timer):
        session.schedule_tts_idle_unload()
    timer = state.tts_idle_timer
    assert timer is not None
    assert gate.arrived.wait(_WAIT)

    # A new run starts: it cancels the pending unload and keeps the engine.
    runtime.is_running = True
    session.cancel_tts_idle_unload()
    gate.proceed.set()
    timer.join(_WAIT)

    engine.unload.assert_not_called()
    assert state.tts_engine is engine


def test_idle_unload_still_releases_an_idle_engine() -> None:
    engine = MagicMock()
    state = PipelineRunState(tts_engine=engine)
    runtime = SimpleNamespace(
        state=state,
        config=SimpleNamespace(tts_idle_timeout_seconds=0.001),
        is_running=False,
    )
    session = ModelSession(runtime)
    created: list[threading.Timer] = []
    original_timer = threading.Timer

    def recording_timer(*args, **kwargs):
        created.append(original_timer(*args, **kwargs))
        return created[-1]

    with patch("modules.pipeline.model_session.threading.Timer", side_effect=recording_timer):
        session.schedule_tts_idle_unload()
    assert len(created) == 1
    created[0].join(_WAIT)

    engine.unload.assert_called_once_with(keep_in_cache=False)
    assert state.tts_engine is None
    assert state.tts_idle_timer is None


# --- Sequential encode overlap --------------------------------------------


def test_overlapped_encode_does_not_rewrite_the_next_chapters_stage(tmp_path) -> None:
    config = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        enable_parallel=False,
        enable_pipelining=True,
    )
    pipeline = ConversionPipeline(config)
    runtime = pipeline.stages.runtime
    events: list[tuple[str, int]] = []
    pipeline.progress_callback = (
        lambda stage, chapter_idx, *_args: events.append((stage.value, chapter_idx))
    )
    chapters = [_Chapter(1, "One"), _Chapter(2, "Two")]
    next_prepare_started = threading.Event()
    earlier_encode_reported = threading.Event()
    observed: dict[str, PipelineStage] = {}

    def fake_prepare(*, chapter, chapter_idx, total_chapters, output_dir):
        _ = chapter, output_dir
        runtime.set_stage(PipelineStage.SYNTHESIZING)
        runtime.notify_progress(chapter_idx=chapter_idx, total_chapters=total_chapters)
        if chapter_idx == 1:
            next_prepare_started.set()
            assert earlier_encode_reported.wait(_WAIT)
            observed["stage"] = runtime.state.stage
        result = ChapterResult(chapter_number=chapter_idx + 1, chapter_title="")
        return result, tmp_path / f"{chapter_idx}.wav", False

    def fake_finalize(*, result, chapter_wav, chapter, chapter_idx, total_chapters, output_dir):
        _ = chapter_wav, chapter, output_dir
        if chapter_idx == 0:
            assert next_prepare_started.wait(_WAIT)
        runtime.set_stage(PipelineStage.ENCODING)
        runtime.notify_progress(chapter_idx=chapter_idx, total_chapters=total_chapters)
        if chapter_idx == 0:
            earlier_encode_reported.set()
        result.duration_ms = 1000
        return result

    with (
        patch.object(ChapterProcessor, "prepare_chapter_audio", side_effect=fake_prepare),
        patch.object(ChapterProcessor, "finalize_chapter_encoding", side_effect=fake_finalize),
    ):
        results = pipeline.stages.process_chapters_sequential(chapters, config.output_dir)

    assert [result.error for result in results] == [None, None]
    assert observed["stage"] == PipelineStage.SYNTHESIZING
    assert ("encoding", 0) not in events
    # Nothing runs alongside the final chapter's encode, so it still reports.
    assert events[-1] == ("encoding", 1)


# --- Parallel worker temp artifacts ---------------------------------------


def _worker_config(tmp_path: Path) -> PipelineConfig:
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    return PipelineConfig(output_dir=tmp_path / "output", temp_dir=temp_dir)


def _run_worker(tmp_path: Path, process_chapter) -> tuple[ChapterResult, Path]:
    config = _worker_config(tmp_path)
    with (
        patch("modules.pipeline.orchestrator.get_worker_cached_config", return_value=config),
        patch("modules.pipeline.orchestrator.get_worker_cached_models", return_value=(None, None)),
        patch.object(ChapterProcessor, "process_chapter", new=process_chapter),
    ):
        result = process_chapter_worker(
            chapter=_Chapter(1, "One"),
            chapter_idx=0,
            output_dir=tmp_path / "output",
            total_chapters=1,
        )
    return result, config.temp_dir


def _write(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def test_worker_discards_its_scratch_but_keeps_returned_artifacts(tmp_path) -> None:
    def process_chapter(self, chapter, chapter_idx, total_chapters, output_dir):
        _ = chapter_idx, total_chapters, output_dir
        temp_dir = self.runtime.config.temp_dir
        for scratch in (temp_dir / "ch_001_tts" / "chunk_0000.wav", temp_dir / "chapter_01.wav"):
            self.runtime.track_temp_cleanup_path(_write(scratch))
        narrated = _write(temp_dir / "chapter_01_narrated.wav")
        aac = _write(temp_dir / "chapter_01.m4a")
        self.runtime.track_temp_cleanup_path(narrated)
        self.runtime.track_temp_cleanup_path(aac)
        return ChapterResult(
            chapter_number=chapter.number,
            chapter_title=chapter.title,
            wav_path=narrated,
            aac_path=aac,
        )

    result, temp_dir = _run_worker(tmp_path, process_chapter)

    assert result.error is None
    assert not (temp_dir / "ch_001_tts").exists()
    assert not (temp_dir / "chapter_01.wav").exists()
    assert result.wav_path.is_file()
    assert result.aac_path.is_file()


def test_failed_worker_chapter_discards_all_of_its_scratch(tmp_path) -> None:
    def process_chapter(self, chapter, chapter_idx, total_chapters, output_dir):
        _ = chapter, chapter_idx, total_chapters, output_dir
        temp_dir = self.runtime.config.temp_dir
        self.runtime.track_temp_cleanup_path(_write(temp_dir / "ch_001_tts" / "chunk_0000.wav"))
        self.runtime.track_temp_cleanup_path(_write(temp_dir / "chapter_01.wav"))
        raise RuntimeError("synthesis failed")

    result, temp_dir = _run_worker(tmp_path, process_chapter)

    assert result.error == "synthesis failed"
    assert list(temp_dir.iterdir()) == []


def _parallel_pipeline(tmp_path: Path) -> ConversionPipeline:
    config = PipelineConfig(
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        enable_parallel=True,
        max_parallel_chapters=2,
        memory_policy=PermissiveMemoryPolicy(),
    )
    config.temp_dir.mkdir(parents=True)
    return ConversionPipeline(config=config)


def test_partial_retry_still_cleans_artifacts_returned_by_workers(tmp_path) -> None:
    pipeline = _parallel_pipeline(tmp_path)
    runtime = pipeline.stages.runtime
    temp_dir = pipeline.config.temp_dir
    chapters = [_Chapter(1, "One"), _Chapter(2, "Two")]
    worker_aac = _write(temp_dir / "chapter_01.m4a")
    worker_wav = _write(temp_dir / "chapter_01_narrated.wav")

    def fake_parallel(self, *, chapters, process_func, progress_callback, cancel_requested=None):
        _ = self, process_func, progress_callback, cancel_requested
        return [
            ChapterResult(
                chapter_number=1,
                chapter_title="One",
                wav_path=worker_wav,
                aac_path=worker_aac,
                duration_ms=1000,
            ),
            ChapterResult(chapter_number=2, chapter_title="Two", error="boom"),
        ]

    def fake_retry(chapter, chapter_idx, total_chapters, output_dir):
        _ = chapter_idx, total_chapters, output_dir
        # The parent's own retry tracks its artifacts, disabling the sweep.
        retried = _write(temp_dir / "chapter_02_narrated.wav")
        runtime.track_temp_cleanup_path(retried)
        return ChapterResult(
            chapter_number=chapter.number,
            chapter_title=chapter.title,
            wav_path=retried,
            duration_ms=1000,
        )

    runner = pipeline.stages._parallel_runner  # noqa: SLF001
    with (
        patch(
            "modules.pipeline.parallel.ParallelPipelineOrchestrator.process_chapters_parallel",
            new=fake_parallel,
        ),
        patch.object(runner, "process_chapter", side_effect=fake_retry),
    ):
        results = pipeline.stages.process_chapters_parallel(chapters, pipeline.config.output_dir)
    assert [result.error for result in results] == [None, None]

    pipeline._cleanup_temp()  # noqa: SLF001
    assert list(temp_dir.iterdir()) == []


def test_untracked_sweep_removes_temp_chapter_aac(tmp_path) -> None:
    pipeline = _parallel_pipeline(tmp_path)
    temp_dir = pipeline.config.temp_dir
    _write(temp_dir / "chapter_03.m4a")
    keep = _write(temp_dir / "unrelated.m4a")

    pipeline._cleanup_temp()  # noqa: SLF001

    assert list(temp_dir.iterdir()) == [keep]


def test_partial_failure_retry_stops_when_cancelled(tmp_path) -> None:
    pipeline = _parallel_pipeline(tmp_path)
    chapters = [_Chapter(n, f"Chapter {n}") for n in (1, 2, 3)]
    retried: list[int] = []

    def fake_parallel(self, *, chapters, process_func, progress_callback, cancel_requested=None):
        _ = self, process_func, progress_callback, cancel_requested
        return [
            ChapterResult(chapter_number=1, chapter_title="1", duration_ms=1000),
            ChapterResult(chapter_number=2, chapter_title="2", error="boom"),
            ChapterResult(chapter_number=3, chapter_title="3", error="boom"),
        ]

    def fake_retry(chapter, chapter_idx, total_chapters, output_dir):
        _ = total_chapters, output_dir
        retried.append(chapter_idx)
        pipeline.cancel()
        return ChapterResult(chapter_number=chapter.number, chapter_title=chapter.title, error="Cancelled")

    runner = pipeline.stages._parallel_runner  # noqa: SLF001
    with (
        patch(
            "modules.pipeline.parallel.ParallelPipelineOrchestrator.process_chapters_parallel",
            new=fake_parallel,
        ),
        patch.object(runner, "process_chapter", side_effect=fake_retry),
    ):
        pipeline.stages.process_chapters_parallel(chapters, pipeline.config.output_dir)

    assert retried == [1]


# --- Repair cancellation --------------------------------------------------


def _repair_runtime(state: dict, **overrides) -> RepairRuntime:
    def set_stage(stage):
        state["stage"] = stage

    fields = dict(
        begin_run=lambda: None,
        finish_run=lambda: state.setdefault("finished", True),
        check_cancelled=lambda: state.get("cancelled", False),
        set_stage=set_stage,
        set_total_chars=lambda _total: None,
        ingest_source=lambda _path: SimpleNamespace(chapters=[_Chapter(1, "One")]),
        process_missing_chapters=lambda chapters, output_dir, total: [],
        package_results=lambda *args: None,
        cancel=lambda: state.__setitem__("cancelled", True),
        shutdown_workers=lambda: state.setdefault("pool_shutdowns", []).append(1) or True,
    )
    fields.update(overrides)
    return RepairRuntime(**fields)


def _repair_plan(tmp_path: Path, **overrides) -> RepairPlan:
    output_dir = tmp_path / "book"
    fields = dict(
        source_path=tmp_path / "source.md",
        stored_chapters=[SimpleNamespace(id=1, number=1, title="One", duration_ms=0, mp3_path=None)],
        title="Book",
        author=None,
        output_dir=output_dir,
        chapters_dir=output_dir / "chapters",
        resolve_chapter_audio=lambda _stored: None,
    )
    fields.update(overrides)
    return RepairPlan(**fields)


def test_repair_cancelled_during_packaging_reports_cancelled_and_cleans_up(tmp_path) -> None:
    state: dict = {}
    plan = _repair_plan(tmp_path)

    def process_missing(chapters, output_dir, total):
        _ = output_dir, total
        _write(plan.chapters_dir / ".0123456789abcdef0123456789abcdef.tmp.m4a")
        return [
            ChapterResult(
                chapter_number=chapter.number,
                chapter_title=chapter.title,
                mp3_path=_write(plan.chapters_dir / "01_One.m4a"),
                duration_ms=1000,
            )
            for chapter in chapters
        ]

    def package(*_args):
        state["cancelled"] = True
        raise RuntimeError("FFmpeg failed: cancelled")

    result = execute_repair_plan(
        _repair_runtime(state, process_missing_chapters=process_missing, package_results=package),
        plan,
    )

    assert result.error == "Cancelled"
    assert state["stage"] == PipelineStage.CANCELLED
    assert state["pool_shutdowns"] == [1]
    assert not (plan.chapters_dir / ".0123456789abcdef0123456789abcdef.tmp.m4a").exists()
    assert (plan.chapters_dir / "01_One.m4a").is_file()


def test_repair_cancelled_duration_probe_is_not_treated_as_missing_audio(tmp_path) -> None:
    state: dict = {}
    audio = _write(tmp_path / "book" / "chapters" / "01_One.m4a")
    cancel = threading.Event()
    processed: list[object] = []
    plan = _repair_plan(
        tmp_path,
        resolve_chapter_audio=lambda _stored: audio,
        cancel_requested=cancel.is_set,
    )

    def get_duration(self, path, *, cancel_requested=None):
        _ = self, path
        cancel.set()
        assert cancel_requested is not None and cancel_requested()
        raise RuntimeError("FFprobe failed: cancelled")

    with patch("modules.audio.encoder.AudioEncoder.get_duration", new=get_duration):
        result = execute_repair_plan(
            _repair_runtime(
                state,
                process_missing_chapters=lambda chapters, *_: processed.extend(chapters) or [],
            ),
            plan,
        )

    assert result.error == "Cancelled"
    assert processed == []
    assert state["stage"] == PipelineStage.CANCELLED


def test_repair_failure_without_cancel_is_still_an_error(tmp_path) -> None:
    state: dict = {}

    def ingest(_path):
        raise ValueError("unreadable source")

    result = execute_repair_plan(_repair_runtime(state, ingest_source=ingest), _repair_plan(tmp_path))

    assert result.error == "unreadable source"
    assert state["stage"] == PipelineStage.ERROR
    assert "pool_shutdowns" not in state


# --- Book workspace -------------------------------------------------------


def test_workspace_claims_a_fresh_directory_when_the_name_appears_concurrently(tmp_path) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()
    original_mkdir = Path.mkdir
    raced: list[Path] = []

    def mkdir_after_rival(self, *args, **kwargs):
        if self == output_root / "Title" and not raced:
            # Another run claims the same name between our check and create.
            original_mkdir(self)
            raced.append(self)
        return original_mkdir(self, *args, **kwargs)

    with (
        patch.object(Path, "exists", return_value=False),
        patch.object(Path, "mkdir", new=mkdir_after_rival),
    ):
        workspace = prepare_book_workspace(output_root, "Title")

    assert raced == [output_root / "Title"]
    assert workspace.output_dir == output_root / "Title (2)"
    assert workspace.chapters_dir.is_dir()


# --- Worker initialization ------------------------------------------------


def _worker_init_config(**overrides) -> dict:
    config = {
        "voice": "af_bella",
        "cleaner_model_name": "model",
        "cleaner_backend": "mlx",
        "cleaner_api_base_url": "https://example.invalid/v1",
        "cleaner_api_key_env": "UNUSED_KEY",
        "cleaner_api_model": "model",
        "pipeline_config": {"chunk_size": 321},
    }
    config.update(overrides)
    return config


def _init_worker(worker_init_config: dict):
    from modules.pipeline import worker_runtime

    engine = MagicMock()
    cleaner = MagicMock()
    spec = SimpleNamespace(
        provider="mlx", device=None, engine="kokoro", factory_kwargs=lambda: {},
    )
    with (
        patch.object(worker_runtime, "_runtime_spec_from_worker_config", return_value=spec),
        patch.object(worker_runtime, "_worker_environment_from_config", return_value={}),
        patch.object(worker_runtime, "build_worker_pipeline_config", return_value=None),
        patch.object(worker_runtime.signal, "signal"),
        patch("modules.tts.factory.TTSEngineFactory.create", return_value=engine) as create,
        patch("modules.tts.cleaner.TextCleaner", return_value=cleaner),
    ):
        worker_runtime._init_worker(  # noqa: SLF001
            available_vram_gb=8.0,
            active_workers=1,
            worker_init_config=worker_init_config,
        )
    status = worker_runtime.get_worker_startup_status()
    return create, cleaner, status


def test_worker_defers_cleaner_load_when_rule_gating_is_enabled() -> None:
    _create, cleaner, status = _init_worker(
        _worker_init_config(cleaner_skip_llm_when_not_needed=True),
    )

    assert status.success is True
    cleaner.load.assert_not_called()


def test_worker_skips_model_load_for_basic_cleanup() -> None:
    _create, cleaner, status = _init_worker(
        _worker_init_config(cleaner_backend="basic", cleaner_skip_llm_when_not_needed=False),
    )

    assert status.success is True
    cleaner.load.assert_not_called()


def test_worker_loads_cleaner_eagerly_when_llm_cleanup_always_runs() -> None:
    _create, cleaner, status = _init_worker(
        _worker_init_config(cleaner_skip_llm_when_not_needed=False),
    )

    assert status.success is True
    cleaner.load.assert_called_once_with()


def test_worker_tts_engine_receives_configured_chunk_size() -> None:
    create, _cleaner, _status = _init_worker(_worker_init_config())

    assert create.call_args.kwargs["max_chunk_tokens"] == 321


# --- Worker startup classification ----------------------------------------


def test_tts_runtime_memory_error_selects_memory_fallback() -> None:
    status = classify_worker_startup_error(
        TTSRuntimeMemoryError("CUDA runtime unavailable: out of memory"),
        provider="cuda",
        device="cuda:0",
        phase="tts_load",
    )

    assert status.error_type == StartupErrorKind.RUNTIME_MEMORY
    assert (
        resolve_startup_failure_action(status, memory_degrade_allowed=True)
        == StartupFailureAction.FALLBACK_MEMORY
    )


# --- Executor shutdown that raises ------------------------------------------


class _FakeWorkerProcess:
    def __init__(self, *, dies_on_terminate: bool = True) -> None:
        self.pid = None
        self.alive = True
        self.dies_on_terminate = dies_on_terminate
        self.terminate_calls = 0
        self.kill_calls = 0

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.dies_on_terminate:
            self.alive = False

    def kill(self) -> None:
        self.kill_calls += 1
        if self.dies_on_terminate:
            self.alive = False

    def join(self, timeout=None) -> None:
        _ = timeout


class _RaisingProcessExecutor:
    """Process-backed executor whose shutdown() itself fails."""

    def __init__(self, processes: list[_FakeWorkerProcess]) -> None:
        self._processes = {index: process for index, process in enumerate(processes)}

    def shutdown(self, wait=True, *, cancel_futures=False) -> None:
        _ = wait, cancel_futures
        raise OSError("handle is closed")


def _manager_owning(executor) -> SharedWorkerPoolManager:
    manager = SharedWorkerPoolManager(max_workers=1, idle_shutdown_seconds=0)
    manager._state.executor = executor  # noqa: SLF001
    manager._state.use_processes = True  # noqa: SLF001
    return manager


def _wait_for_stopping_executor_returns(manager: SharedWorkerPoolManager) -> bool:
    waiter = threading.Thread(target=manager._wait_for_stopping_executor)  # noqa: SLF001
    waiter.daemon = True
    waiter.start()
    waiter.join(_WAIT)
    return not waiter.is_alive()


def test_failed_executor_shutdown_stops_workers_and_releases_the_barrier() -> None:
    worker = _FakeWorkerProcess()
    manager = _manager_owning(_RaisingProcessExecutor([worker]))

    assert manager.shutdown(timeout_seconds=_WAIT) is True

    assert worker.alive is False
    assert worker.terminate_calls == 1
    assert manager.cleanup_pending is False
    assert _wait_for_stopping_executor_returns(manager)


def test_failed_executor_shutdown_keeps_barrier_while_a_worker_survives() -> None:
    worker = _FakeWorkerProcess(dies_on_terminate=False)
    manager = _manager_owning(_RaisingProcessExecutor([worker]))

    # Bounded: the call returns instead of hanging, and reports not stopped.
    assert manager.shutdown(timeout_seconds=1.0) is False

    assert worker.terminate_calls >= 1
    assert worker.kill_calls >= 1
    # Fail closed: admission reports pending cleanup instead of starting a
    # successor alongside a worker that may still write chapter outputs.
    assert manager.cleanup_pending is True
    assert worker.alive is True
