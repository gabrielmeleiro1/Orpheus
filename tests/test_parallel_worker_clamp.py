"""A requested worker count never overrides device memory safety, and idle pools release models."""

from __future__ import annotations

import time
from types import SimpleNamespace

from modules.pipeline.chapter_dispatch import (
    ChapterDispatchContext,
    dispatch_chapters,
    effective_workers,
)
from modules.pipeline.models import ChapterResult, build_worker_plan
from modules.pipeline.pool_manager import SharedWorkerPoolManager


def _config(*, cleaner_backend="basic", cleaner_device="cuda", memory_policy=None, requested=2):
    return SimpleNamespace(
        requested_parallel_chapters=requested,
        max_parallel_chapters=requested,
        parallel_worker_mode="manual",
        tts_worker_auto_mode="sequential",
        tts_process_safe=False,
        enable_parallel=True,
        tts_engine="kokoro",
        tts_runtime=SimpleNamespace(provider="cuda", device=None),
        cleaner_backend=cleaner_backend,
        cleaner_cuda_device=cleaner_device,
        memory_policy=memory_policy,
    )


class _OneWorkerPolicy:
    selected_worker_count_reason = "memory headroom limits parallel workers"

    def allowed_workers(self, requested: int) -> int:
        return 1


class _RoomyPolicy:
    selected_worker_count_reason = None

    def allowed_workers(self, requested: int) -> int:
        return requested


def test_manual_count_is_clamped_when_tts_and_cuda_cleaner_share_the_gpu():
    plan = build_worker_plan(_config(cleaner_backend="cuda"))

    assert plan.max_workers == 1
    assert plan.initial_reason == "the TTS model and the CUDA cleaner share one GPU"


def test_manual_count_is_clamped_to_what_device_memory_allows_at_dispatch():
    plan = build_worker_plan(_config())
    selection = effective_workers(
        worker_plan=plan, total_chapters=4, total_chars=50_000, memory_policy=_OneWorkerPolicy(),
    )

    assert selection.count == 1
    assert selection.requested == 2
    assert selection.reason == "memory headroom limits parallel workers"


def test_manual_count_is_kept_when_memory_allows_it():
    plan = build_worker_plan(_config(requested=3))
    selection = effective_workers(
        worker_plan=plan, total_chapters=4, total_chars=50_000, memory_policy=_RoomyPolicy(),
    )

    assert plan.max_workers == 3
    assert selection.count == 3
    assert selection.reason == "manual worker count"


def test_dispatch_uses_the_memory_policy_in_force_when_chapters_are_dispatched():
    logs: list[tuple[str, str]] = []
    plan = build_worker_plan(_config(memory_policy=_RoomyPolicy()))
    config = SimpleNamespace(
        worker_plan=plan, memory_policy=_OneWorkerPolicy(), shared_worker_pool_manager=None,
    )
    calls: list[str] = []
    runtime = SimpleNamespace(
        config=config,
        state=SimpleNamespace(total_chars=50_000),
        is_parallel=True,
        check_cancelled=lambda: False,
        log_verbose=lambda message, level="info": logs.append((message, level)),
        parallel_config_max_workers=lambda fallback: 2,
        set_parallel_execution_active=lambda active: None,
        set_selected_parallel_chapters=lambda count: None,
        set_selected_parallel_worker_count_reason=lambda reason: None,
    )
    runner = SimpleNamespace(
        warmup_models=lambda: calls.append("warmup"),
        process_chapters_sequential=lambda chapters, output_dir: calls.append("sequential") or [],
        process_chapters_parallel=lambda chapters, output_dir: calls.append("parallel") or [],
    )

    dispatch_chapters(
        ChapterDispatchContext(runtime=runtime, runner=runner), [object(), object()], None,
    )

    assert calls == ["warmup", "sequential"]
    assert (
        "[PARALLEL] Using 1 worker of 2 requested: memory headroom limits parallel workers",
        "warning",
    ) in logs


def test_dispatch_logs_the_effective_worker_count_and_why():
    logs: list[tuple[str, str]] = []
    plan = build_worker_plan(_config(cleaner_backend="cuda"))
    config = SimpleNamespace(worker_plan=plan, memory_policy=None, shared_worker_pool_manager=None)
    state = SimpleNamespace(total_chars=50_000)
    calls: list[str] = []

    runtime = SimpleNamespace(
        config=config,
        state=state,
        is_parallel=True,
        check_cancelled=lambda: False,
        log_verbose=lambda message, level="info": logs.append((message, level)),
        parallel_config_max_workers=lambda fallback: 2,
        set_parallel_execution_active=lambda active: None,
        set_selected_parallel_chapters=lambda count: None,
        set_selected_parallel_worker_count_reason=lambda reason: None,
    )
    runner = SimpleNamespace(
        warmup_models=lambda: calls.append("warmup"),
        process_chapters_sequential=lambda chapters, output_dir: calls.append("sequential") or [],
        process_chapters_parallel=lambda chapters, output_dir: calls.append("parallel") or [],
    )

    dispatch_chapters(
        ChapterDispatchContext(runtime=runtime, runner=runner), [object(), object()], None,
    )

    assert calls == ["warmup", "sequential"]
    assert (
        "[PARALLEL] Using 1 worker of 2 requested: the TTS model and the CUDA cleaner share one GPU",
        "warning",
    ) in logs


def _chapter_result(chapter, idx, _manager):
    return ChapterResult(chapter_number=idx + 1, chapter_title=str(chapter), duration_ms=1)


def _slow_chapter_result(chapter, idx, _manager):
    time.sleep(0.4)
    return _chapter_result(chapter, idx, _manager)


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_idle_pool_shuts_down_after_the_idle_timeout_but_not_mid_run():
    manager = SharedWorkerPoolManager(max_workers=2, idle_shutdown_seconds=0.1)
    manager._use_processes = False
    try:
        results = manager.run_parallel(
            chapters=["a", "b", "c"], process_func=_slow_chapter_result, config_fingerprint=("idle",),
        )
        # Chapters outlasted the idle timeout without the pool being torn down.
        assert [result.error for result in results] == [None, None, None]
        assert manager.snapshot()["running"] is True or _wait_until(
            lambda: not manager.snapshot()["running"]
        )

        assert _wait_until(lambda: not manager.snapshot()["running"])
        assert not manager.cleanup_pending

        # The next job starts a fresh pool normally.
        again = manager.run_parallel(
            chapters=["d"], process_func=_chapter_result, config_fingerprint=("idle",),
        )
        assert [result.error for result in again] == [None]
    finally:
        manager.shutdown(timeout_seconds=5.0)
