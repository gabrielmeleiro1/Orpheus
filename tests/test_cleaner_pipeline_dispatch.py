"""Tests for registry-driven cleaner dispatch in the pipeline."""

from __future__ import annotations

import types

from modules.pipeline.models import PipelineConfig, build_worker_plan
from modules.pipeline.stages.cleaning import clean_chunks_with_trace
from modules.tts.cleaner_backends.registry import (
    is_local_cleaner_backend,
    register_cleaner_backend,
    requires_api_key_cleaner_backend,
)
from modules.tts.cleaner_config import CleanTrace
from modules.tts.runtime_spec import TTSRuntimeSpec
from tests.fixtures.fake_cleaner_backend import FakeLocalCleanerBackend


def test_local_cleaning_stops_scheduling_chunks_when_cancelled(monkeypatch):
    from modules.tts.cleaner import TextCleaner
    from modules.tts.cleaner_config import CleanerConfig

    cleaner = TextCleaner(CleanerConfig(backend="mlx"))
    processed = []

    def clean(text):
        processed.append(text)
        return CleanTrace(
            original_text=text, rule_based_text=text, cleaned_text=text, mode="rule_only",
        )

    monkeypatch.setattr(cleaner, "clean_with_trace", clean)
    runtime = types.SimpleNamespace(
        check_cancelled=lambda: bool(processed),
        log_verbose=lambda *_args, **_kwargs: None,
        config=types.SimpleNamespace(save_cleaning_debug=False),
    )
    _cleaned, _inputs, _rows, cancelled = clean_chunks_with_trace(
        runtime, types.SimpleNamespace(), chapter_idx=0, total_chapters=1,
        chunks=["one", "longer two", "longest three"], cleaner=cleaner,
    )

    assert cancelled is True
    assert processed == ["one"]


def test_is_local_cleaner_backend_mlx_and_api():
    assert is_local_cleaner_backend("mlx") is True
    assert is_local_cleaner_backend("MLX") is True
    assert is_local_cleaner_backend("api") is False
    assert is_local_cleaner_backend("unknown") is False
    assert requires_api_key_cleaner_backend("mlx") is False
    assert requires_api_key_cleaner_backend("api") is True
    assert requires_api_key_cleaner_backend("unknown") is False


def test_is_local_cleaner_backend_cuda():
    assert is_local_cleaner_backend("cuda") is True
    assert is_local_cleaner_backend("CUDA") is True
    assert requires_api_key_cleaner_backend("cuda") is False


def test_build_worker_plan_uses_registry_for_local_gpu_contention_guard():
    config = PipelineConfig(
        enable_parallel=True,
        requested_parallel_chapters=3,
        parallel_worker_mode="auto",
        tts_engine="kokoro",
        cleaner_backend="mlx",
        tts_worker_auto_mode="memory_safe",
        tts_process_safe=False,
    )

    plan = build_worker_plan(config)

    assert plan.max_workers == 1
    assert plan.initial_reason == "local GPU model contention guard"


def test_build_worker_plan_cuda_local_gpu_contention():
    config = PipelineConfig(
        enable_parallel=True,
        requested_parallel_chapters=3,
        parallel_worker_mode="auto",
        tts_engine="kokoro",
        cleaner_backend="cuda",
        cleaner_cuda_device="cuda:0",
        cleaner_cuda_quantization="4bit",
        tts_runtime=TTSRuntimeSpec.from_legacy(
            "kokoro",
            "bf16",
            provider="cuda",
            device="cuda:0",
        ),
        tts_worker_auto_mode="memory_safe",
        tts_process_safe=False,
    )

    plan = build_worker_plan(config)

    assert plan.max_workers == 1
    assert plan.initial_reason == "CUDA TTS + CUDA cleaner same-device contention guard"


def test_build_worker_plan_skips_local_gpu_guard_for_api_backend():
    config = PipelineConfig(
        enable_parallel=True,
        requested_parallel_chapters=3,
        parallel_worker_mode="auto",
        tts_engine="kokoro",
        cleaner_backend="api",
        tts_worker_auto_mode="memory_safe",
        tts_process_safe=False,
    )

    plan = build_worker_plan(config)

    assert plan.max_workers == 3
    assert plan.initial_reason is None


def test_build_worker_plan_local_gpu_guard_follows_registered_local_backend(
    registry_snapshot,
):
    register_cleaner_backend("fake-local", FakeLocalCleanerBackend)

    config = PipelineConfig(
        enable_parallel=True,
        requested_parallel_chapters=3,
        parallel_worker_mode="auto",
        tts_engine="kokoro",
        cleaner_backend="fake-local",
        tts_worker_auto_mode="memory_safe",
        tts_process_safe=False,
    )

    plan = build_worker_plan(config)

    assert plan.max_workers == 1
    assert plan.initial_reason == "local GPU model contention guard"


def test_cleaning_stage_cleans_cuda_chunks_through_the_local_batch():
    runtime = types.SimpleNamespace(
        check_cancelled=lambda: False,
        log_verbose=lambda *_args, **_kwargs: None,
        notify_progress=lambda **_kwargs: None,
        config=types.SimpleNamespace(save_cleaning_debug=False),
    )
    # A CUDA cleaner offers only the serial local batch; an API-style
    # concurrent batch would fail on the missing async method.
    cleaner = types.SimpleNamespace(
        config=types.SimpleNamespace(backend="cuda"),
        clean_chunks_with_trace=lambda chunks, cancel_requested=None: [
            CleanTrace(
                original_text=text, rule_based_text=text,
                cleaned_text=f"clean {text}", mode="llm_success",
            )
            for text in chunks
        ],
    )

    cleaned, _inputs, _rows, cancelled = clean_chunks_with_trace(
        runtime, types.SimpleNamespace(), chapter_idx=0, total_chapters=1,
        chunks=["one", "two"], cleaner=cleaner,
    )

    assert cleaned == ["clean one", "clean two"]
    assert cancelled is False
