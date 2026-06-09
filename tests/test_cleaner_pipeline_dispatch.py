"""Tests for registry-driven cleaner dispatch in the pipeline."""

from __future__ import annotations

import types
from unittest.mock import patch

import pytest

from modules.pipeline.models import PipelineConfig, build_worker_plan
from modules.pipeline.stages.cleaning import clean_chunks_with_trace
from modules.tts.cleaner_backends.registry import (
    _REGISTRY,
    is_local_cleaner_backend,
    register_cleaner_backend,
    requires_api_key_cleaner_backend,
)
from modules.tts.cleaner_config import CleanTrace


@pytest.fixture
def registry_snapshot():
    """Preserve registry contents across tests that register custom backends."""
    original = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(original)


def test_is_local_cleaner_backend_mlx_and_api():
    assert is_local_cleaner_backend("mlx") is True
    assert is_local_cleaner_backend("MLX") is True
    assert is_local_cleaner_backend("api") is False
    assert is_local_cleaner_backend("unknown") is False
    assert requires_api_key_cleaner_backend("mlx") is False
    assert requires_api_key_cleaner_backend("api") is True
    assert requires_api_key_cleaner_backend("unknown") is False


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
    class FakeLocalCleanerBackend:
        name = "fake-local"
        is_local = True
        requires_model = False
        display_name = "Fake Local"
        requires_api_key = False

        def load(self, cleaner) -> None:
            return None

        async def clean_batch_with_trace_async(self, cleaner, texts):
            return []

        def generate_llm_response(self, cleaner, prompt: str) -> str:
            return prompt

        def unload(self, cleaner, *, keep_in_cache: bool = True) -> None:
            return None

        def clear_cache(self, cleaner) -> None:
            return None

        def list_models(self) -> list[str]:
            return ["fake-model"]

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


def test_cleaning_stage_uses_local_dispatch_for_mlx():
    runtime = types.SimpleNamespace(
        check_cancelled=lambda: False,
        log_verbose=lambda *_args, **_kwargs: None,
        config=types.SimpleNamespace(save_cleaning_debug=False),
    )
    cleaner = types.SimpleNamespace(
        config=types.SimpleNamespace(backend="mlx"),
        clean_chunks_with_trace=lambda chunks: [
            CleanTrace(
                original_text=text,
                rule_based_text=text,
                cleaned_text=text,
                mode="rule_only",
            )
            for text in chunks
        ],
    )
    hooks = types.SimpleNamespace()

    with patch(
        "modules.pipeline.stages.cleaning._run_sync_trace_batch_cleaning",
        return_value=([], False),
    ) as local_dispatch:
        cleaned, llm_inputs, trace_rows, cancelled = clean_chunks_with_trace(
            runtime,
            hooks,
            chapter_idx=0,
            total_chapters=1,
            chunks=["one", "two"],
            cleaner=cleaner,
        )

    local_dispatch.assert_called_once()
    assert cleaned == []
    assert llm_inputs == []
    assert trace_rows == []
    assert cancelled is False


def test_cleaning_stage_uses_api_batch_path_for_api_backend():
    runtime = types.SimpleNamespace(
        check_cancelled=lambda: False,
        log_verbose=lambda *_args, **_kwargs: None,
        config=types.SimpleNamespace(save_cleaning_debug=False),
    )
    cleaner = types.SimpleNamespace(
        config=types.SimpleNamespace(backend="api"),
    )
    hooks = types.SimpleNamespace()

    with (
        patch(
            "modules.pipeline.stages.cleaning._run_sync_trace_batch_cleaning",
        ) as local_dispatch,
        patch(
            "modules.pipeline.stages.cleaning._run_batch_trace_cleaning",
            return_value=(
                [
                    types.SimpleNamespace(
                        cleaned_text="one",
                        llm_input_text="one",
                        trace_row=None,
                    )
                ],
                False,
            ),
        ) as api_batch,
    ):
        cleaned, llm_inputs, trace_rows, cancelled = clean_chunks_with_trace(
            runtime,
            hooks,
            chapter_idx=0,
            total_chapters=1,
            chunks=["one"],
            cleaner=cleaner,
        )

    local_dispatch.assert_not_called()
    api_batch.assert_called_once()
    assert cleaned == ["one"]
    assert llm_inputs == ["one"]
    assert trace_rows == []
    assert cancelled is False
