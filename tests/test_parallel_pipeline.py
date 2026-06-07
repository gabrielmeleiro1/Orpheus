"""
Tests for the surviving parallel pipeline architecture.
"""

from concurrent.futures import Future
import os
import asyncio
import importlib
import pickle
import sys
import threading
import time
import types
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

import pytest
import modules.pipeline as pipeline_module
import numpy as np

from modules.pipeline.chapter_dispatch import (
    ChapterDispatchContext,
    adaptive_parallel_workers,
    dispatch_chapters,
    effective_workers,
)
from modules.pipeline.models import WorkerPlan
from modules.pipeline.pool_manager import SharedWorkerPoolManager

# Import parallel components
try:
    from modules.pipeline.parallel import (
        ParallelConfig,
        VRAMBudget,
        get_optimal_worker_count,
        create_parallel_config,
        ParallelPipelineOrchestrator,
        _init_worker,
    )

    PARALLEL_AVAILABLE = True
except ImportError as e:
    PARALLEL_AVAILABLE = False
    print(f"Parallel module not available: {e}")

# Mark all tests in this module
pytestmark = [
    pytest.mark.skipif(not PARALLEL_AVAILABLE, reason="Parallel module not available"),
    pytest.mark.pipeline,
    pytest.mark.parallel,
]

from modules.pipeline.orchestrator import (
    ConversionPipeline,
    PipelineConfig,
    ChapterResult,
)
from modules.pipeline.worker_config import (
    build_shared_pool_fingerprint,
    build_worker_init_config,
    build_worker_pipeline_config,
)
from modules.pipeline.chapter_runners import _process_chapter_worker_entry
from modules.pipeline.pipeline_stages import PipelineStages
from modules.pipeline.progress import estimate_eta_from_progress
from modules.tts.runtime_options import TTSRuntimeOptions
from modules.tts.runtime_spec import TTSRuntimeSpec


def test_memory_policy_unknown_memory_allows_one_worker():
    from modules.pipeline.memory_policy import SafeMemoryPolicy

    policy = SafeMemoryPolicy(memory_sampler=lambda: None)

    assert policy.allowed_workers(requested_max=4) == 1


def test_memory_policy_low_headroom_allows_one_worker():
    from modules.pipeline.memory_policy import MemorySnapshot, SafeMemoryPolicy

    snapshot = MemorySnapshot(
        total_bytes=16 * 1024**3,
        available_bytes=2 * 1024**3,
        process_rss_bytes=1 * 1024**3,
        mlx_active_bytes=0,
        mlx_cache_bytes=0,
        mlx_peak_bytes=0,
    )
    policy = SafeMemoryPolicy(memory_sampler=lambda: snapshot)

    assert policy.allowed_workers(requested_max=4) == 1
    assert policy.safe_mode_reason is not None


def test_memory_policy_conservative_unknown_worker_estimate_allows_two_with_headroom():
    from modules.pipeline.memory_policy import MemorySnapshot, SafeMemoryPolicy

    snapshot = MemorySnapshot(
        total_bytes=16 * 1024**3,
        available_bytes=12 * 1024**3,
        process_rss_bytes=1 * 1024**3,
    )
    policy = SafeMemoryPolicy(
        memory_sampler=lambda: snapshot,
        estimated_worker_bytes=4 * 1024**3,
    )

    assert policy.allowed_workers(requested_max=3) == 3
    assert policy.allowed_workers(requested_max=2) == 2
    assert policy.safe_mode_reason is None


def test_memory_sampler_returns_snapshot_when_psutil_probe_works():
    from modules.pipeline.memory_policy import sample_memory_snapshot

    process = MagicMock()
    process.memory_info.return_value = types.SimpleNamespace(rss=3 * 1024**3)

    snapshot = sample_memory_snapshot(
        virtual_memory=lambda: types.SimpleNamespace(
            total=32 * 1024**3,
            available=20 * 1024**3,
        ),
        process_factory=lambda: process,
    )

    assert snapshot is not None
    assert snapshot.total_bytes == 32 * 1024**3
    assert snapshot.available_bytes == 20 * 1024**3
    assert snapshot.process_rss_bytes == 3 * 1024**3


def test_memory_sampler_returns_none_when_detection_fails():
    from modules.pipeline.memory_policy import sample_memory_snapshot

    def fail_virtual_memory():
        raise RuntimeError("memory probe failed")

    assert sample_memory_snapshot(virtual_memory=fail_virtual_memory) is None


def test_memory_sampler_defaults_mlx_fields_to_zero_when_unavailable():
    from modules.pipeline.memory_policy import sample_memory_snapshot

    snapshot = sample_memory_snapshot(
        virtual_memory=lambda: types.SimpleNamespace(
            total=16 * 1024**3,
            available=12 * 1024**3,
        ),
        process_factory=lambda: None,
        mlx_core=False,
    )

    assert snapshot is not None
    assert snapshot.mlx_active_bytes == 0
    assert snapshot.mlx_cache_bytes == 0
    assert snapshot.mlx_peak_bytes == 0


def test_memory_policy_calibration_limits_workers_by_observed_cost():
    from modules.pipeline.memory_policy import MemorySnapshot, SafeMemoryPolicy

    baseline = MemorySnapshot(
        total_bytes=32 * 1024**3,
        available_bytes=20 * 1024**3,
        process_rss_bytes=1 * 1024**3,
    )
    warmup = MemorySnapshot(
        total_bytes=32 * 1024**3,
        available_bytes=13 * 1024**3,
        process_rss_bytes=8 * 1024**3,
    )
    current = MemorySnapshot(
        total_bytes=32 * 1024**3,
        available_bytes=20 * 1024**3,
        process_rss_bytes=8 * 1024**3,
    )
    policy = SafeMemoryPolicy(
        memory_sampler=lambda: current,
        calibration_buffer_bytes=0,
        warmup_safety_multiplier=1.0,
    )

    policy.record_baseline(baseline)
    policy.record_warmup(warmup)

    assert policy.estimated_worker_bytes == 7 * 1024**3
    assert policy.allowed_workers(requested_max=4) == 2


def test_memory_policy_warmup_estimate_can_replace_static_seed_with_clear_headroom():
    from modules.pipeline.memory_policy import MemorySnapshot, SafeMemoryPolicy

    baseline = MemorySnapshot(
        total_bytes=16 * 1024**3,
        available_bytes=12 * 1024**3,
        process_rss_bytes=1 * 1024**3,
    )
    warmup = MemorySnapshot(
        total_bytes=16 * 1024**3,
        available_bytes=10 * 1024**3,
        process_rss_bytes=3 * 1024**3,
    )
    current = MemorySnapshot(
        total_bytes=16 * 1024**3,
        available_bytes=12 * 1024**3,
        process_rss_bytes=3 * 1024**3,
    )
    policy = SafeMemoryPolicy(
        memory_sampler=lambda: current,
        estimated_worker_bytes=4 * 1024**3,
        calibration_buffer_bytes=0,
        warmup_safety_multiplier=1.5,
        warmup_floor_bytes=1024**3,
    )

    policy.record_baseline(baseline)
    policy.record_warmup(warmup)

    assert policy.measured_warmup_delta_bytes == 2 * 1024**3
    assert policy.estimated_worker_bytes == 3 * 1024**3
    assert policy.estimated_per_worker_memory_bytes == 3 * 1024**3
    assert policy.allowed_workers(requested_max=3) == 3
    assert policy.selected_worker_count_reason == "warmup estimate with live headroom"


def test_memory_policy_record_memory_error_forces_one_worker():
    from modules.pipeline.memory_policy import MemorySnapshot, SafeMemoryPolicy

    snapshot = MemorySnapshot(
        total_bytes=64 * 1024**3,
        available_bytes=48 * 1024**3,
        process_rss_bytes=1 * 1024**3,
    )
    policy = SafeMemoryPolicy(
        memory_sampler=lambda: snapshot,
        calibration_buffer_bytes=0,
    )
    policy.record_baseline(snapshot)
    policy.record_warmup(
        MemorySnapshot(
            total_bytes=64 * 1024**3,
            available_bytes=44 * 1024**3,
            process_rss_bytes=5 * 1024**3,
        )
    )

    policy.record_memory_error()

    assert policy.allowed_workers(requested_max=4) == 1
    assert policy.safe_mode_reason == "memory pressure error"


class _PermissiveMemoryPolicy:
    safe_mode_reason = None

    def allowed_workers(self, requested_max: int) -> int:
        return requested_max

    def should_submit_more(self, active_workers: int) -> bool:
        _ = active_workers
        return True


@dataclass
class MockChapter:
    """Mock chapter for testing."""

    number: int
    title: str
    content: str = "Test content for chapter."


def _attach_dispatch_stage_runner(pipeline, calls):
    pipeline.stages = types.SimpleNamespace(
        warmup_models=lambda: calls.append(("warmup", "info")),
        cleanup_models=lambda force=False: calls.append(("cleanup", force)),
        process_chapters_parallel=lambda chapters, output_dir: [
            ("parallel", len(chapters), output_dir)
        ],
        process_chapters_sequential=lambda chapters, output_dir: [
            ("sequential", len(chapters), output_dir)
        ],
    )
    pipeline.dispatch_context = ChapterDispatchContext(
        runtime=_DispatchRuntimeProbe(pipeline),
        runner=pipeline.stages,
    )
    return pipeline


class _DispatchRuntimeProbe:
    def __init__(self, pipeline):
        self._pipeline = pipeline
        self.config = pipeline.config
        self.state = types.SimpleNamespace(
            total_chars=getattr(pipeline, "_total_chars", 0)
        )

    @property
    def is_parallel(self):
        return self._pipeline.is_parallel

    def check_cancelled(self):
        check_cancelled = getattr(self._pipeline, "_check_cancelled", None)
        return bool(callable(check_cancelled) and check_cancelled())

    def log_verbose(self, message, log_type="info"):
        log_verbose = getattr(self._pipeline, "_log_verbose", None)
        if callable(log_verbose):
            log_verbose(message, log_type)

    def parallel_config_max_workers(self, fallback=1):
        parallel_config = getattr(self._pipeline, "_parallel_config", None)
        if parallel_config is None:
            return max(1, int(fallback or 1))
        return max(1, int(getattr(parallel_config, "max_workers", fallback) or 1))

    def set_parallel_execution_active(self, active):
        self._pipeline._parallel_execution_active = bool(active)  # noqa: SLF001

    def set_selected_parallel_chapters(self, count):
        self._pipeline._selected_parallel_chapters = max(  # noqa: SLF001
            1,
            int(count or 1),
        )

    def set_selected_parallel_worker_count_reason(self, reason):
        self._pipeline._selected_parallel_worker_count_reason = reason  # noqa: SLF001


class TestVRAMBudget:
    """Tests for VRAM budget management."""

    def test_vram_budget_calculation(self):
        """Test VRAM budget calculations."""
        budget = VRAMBudget(
            total_vram_gb=32.0,
            reserved_vram_gb=2.0,
            safety_margin_gb=1.0,
        )

        # Available VRAM should be total - reserved - margin
        assert budget.available_vram_gb == 29.0
        assert budget.available_device_memory_gb == 29.0

        # Max concurrent chapters calculation
        # Per chapter: ~0.5GB (TTS) + ~1.5GB (Cleaner) = ~2GB
        # 29GB / 2GB = ~14, but capped at 4
        assert budget.max_concurrent_chapters >= 1
        assert budget.max_concurrent_chapters <= 4

    def test_vram_budget_with_low_memory(self):
        """Test VRAM budget with limited memory."""
        budget = VRAMBudget(
            total_vram_gb=4.0,  # Limited VRAM
            reserved_vram_gb=1.0,
            safety_margin_gb=0.5,
        )

        # Should still allow at least 1 chapter
        assert budget.max_concurrent_chapters >= 1

    def test_vram_budget_auto_adjustment(self):
        """Test that config auto-adjusts based on VRAM."""
        config = ParallelConfig(
            max_workers=10,  # Unrealistically high
            vram_budget=VRAMBudget(total_vram_gb=16.0),
        )

        # Should be reduced based on VRAM
        assert config.max_workers <= 4
        assert config.vram_budget.total_device_memory_gb == config.vram_budget.total_vram_gb

    def test_worker_init_uses_active_worker_count_for_mlx_vram_split(self, monkeypatch):
        """Worker memory env should come from serialized provider policy output."""
        monkeypatch.delenv("MLX_METAL_MEMORY", raising=False)
        fake_engine = MagicMock()
        fake_cleaner = MagicMock()
        expected = int((12.0 / 3) * 0.9 * 1024 * 1024 * 1024)

        with (
            patch("modules.tts.factory.TTSEngineFactory.create", return_value=fake_engine),
            patch("modules.tts.cleaner.TextCleaner", return_value=fake_cleaner),
        ):
            _init_worker(
                available_vram_gb=12.0,
                active_workers=3,
                worker_init_config={
                    "tts_runtime": TTSRuntimeSpec.from_legacy(
                        "kokoro",
                        "bf16",
                        provider="mlx",
                    ).to_dict(),
                    "voice": "af_bella",
                    "cleaner_model_name": "mlx-community/Llama-3.2-3B-Instruct-4bit",
                    "cleaner_backend": "mlx",
                    "cleaner_api_base_url": "https://api.openai.com/v1",
                    "cleaner_api_key_env": "OPENAI_API_KEY",
                    "cleaner_api_model": "gpt-5-nano",
                    "cleaner_api_global_concurrency": 6,
                    "cleaner_api_global_limiter": None,
                    "device_memory_policy": {
                        "provider": "mlx",
                        "device": None,
                    },
                },
            )

        assert os.environ["MLX_METAL_MEMORY"] == str(expected)

    def test_worker_init_cuda_policy_does_not_import_mlx(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "mlx", None)
        monkeypatch.setitem(sys.modules, "mlx.core", None)
        monkeypatch.delenv("MLX_METAL_MEMORY", raising=False)
        fake_engine = MagicMock()
        fake_cleaner = MagicMock()

        with (
            patch("modules.tts.factory.TTSEngineFactory.create", return_value=fake_engine),
            patch("modules.tts.cleaner.TextCleaner", return_value=fake_cleaner),
        ):
            _init_worker(
                available_vram_gb=12.0,
                active_workers=3,
                worker_init_config={
                    "tts_runtime": TTSRuntimeSpec(
                        provider="cuda",
                        engine="kokoro",
                        model=None,
                        quantization="bf16",
                        device="cuda:0",
                    ).to_dict(),
                    "voice": "af_bella",
                    "cleaner_model_name": "mlx-community/Llama-3.2-3B-Instruct-4bit",
                    "cleaner_backend": "api",
                    "cleaner_api_base_url": "https://api.openai.com/v1",
                    "cleaner_api_key_env": "OPENAI_API_KEY",
                    "cleaner_api_model": "gpt-5-nano",
                    "cleaner_api_global_concurrency": 6,
                    "cleaner_api_global_limiter": None,
                    "device_memory_policy": {
                        "provider": "cuda",
                        "device": "cuda:0",
                    },
                },
            )

        assert "MLX_METAL_MEMORY" not in os.environ

    def test_worker_init_runs_tts_probe_warmup_when_models_are_configured(self):
        fake_engine = MagicMock()
        fake_cleaner = MagicMock()

        with (
            patch(
                "modules.tts.factory.TTSEngineFactory.create", return_value=fake_engine
            ) as mock_create,
            patch("modules.tts.cleaner.TextCleaner", return_value=fake_cleaner),
        ):
            _init_worker(
                available_vram_gb=8.0,
                active_workers=2,
                worker_init_config={
                    "tts_runtime": TTSRuntimeSpec.from_legacy(
                        "kokoro",
                        "bf16",
                        model="mlx-community/Kokoro-82M",
                        provider="mlx",
                    ).to_dict(),
                    "voice": "af_bella",
                    "cleaner_model_name": "mlx-community/Llama-3.2-3B-Instruct-4bit",
                    "cleaner_backend": "mlx",
                    "cleaner_api_base_url": "https://api.openai.com/v1",
                    "cleaner_api_key_env": "OPENAI_API_KEY",
                    "cleaner_api_model": "gpt-5-nano",
                    "cleaner_api_global_concurrency": 6,
                    "cleaner_api_global_limiter": None,
                },
            )

        mock_create.assert_called_once()
        fake_engine.load.assert_called_once_with()
        fake_engine.warmup.assert_called_once_with(voice="af_bella")
        fake_cleaner.load.assert_called_once_with()


class TestParallelConfig:
    """Tests for parallel configuration."""

    def test_default_config(self):
        """Test default parallel configuration."""
        config = ParallelConfig()

        assert config.max_workers >= 1
        assert config.max_workers <= 4
        assert config.use_processes is True
        assert set(ParallelConfig.__dataclass_fields__) == {
            "max_workers",
            "use_processes",
            "vram_budget",
            "worker_init_config",
            "enforce_vram_budget",
        }

    def test_create_parallel_config(self):
        """Test helper function for creating config."""
        config = create_parallel_config(
            max_workers=2,
            total_vram_gb=32.0,
        )

        assert config.max_workers == 2
        assert config.use_processes is True
        assert config.vram_budget.total_vram_gb == 32.0

    def test_get_optimal_worker_count(self):
        """Test optimal worker count calculation."""
        # High VRAM should allow max workers
        high_vram = get_optimal_worker_count(total_vram_gb=64.0, max_desired=4)
        assert high_vram <= 4
        assert high_vram >= 1

        # Low VRAM should be constrained
        low_vram = get_optimal_worker_count(total_vram_gb=4.0, max_desired=4)
        assert low_vram >= 1


class TestParallelPipelineOrchestrator:
    """Tests for parallel pipeline orchestrator."""

    @pytest.mark.asyncio
    async def test_orchestrator_initialization(self):
        """Test orchestrator initialization."""
        if not PARALLEL_AVAILABLE:
            pytest.skip("Parallel module not available")

        config = ParallelConfig(max_workers=2)
        orchestrator = ParallelPipelineOrchestrator(config)

        assert orchestrator.config == config
        assert orchestrator._cancelled is False

    def test_process_chapters_parallel(self):
        """Test parallel chapter processing."""
        if not PARALLEL_AVAILABLE:
            pytest.skip("Parallel module not available")

        config = ParallelConfig(max_workers=2, use_processes=False)
        orchestrator = ParallelPipelineOrchestrator(config)

        chapters = [
            MockChapter(1, "Chapter 1", "Content 1"),
            MockChapter(2, "Chapter 2", "Content 2"),
            MockChapter(3, "Chapter 3", "Content 3"),
        ]

        def mock_process(chapter, idx, _fm):
            return ChapterResult(
                chapter_number=chapter.number,
                chapter_title=chapter.title,
                duration_ms=idx + 1,
            )

        progress_calls = []

        def progress_cb(current, total, msg):
            progress_calls.append((current, total, msg))

        results = orchestrator.process_chapters_parallel(
            chapters=chapters,
            process_func=mock_process,
            progress_callback=progress_cb,
        )

        # Should get results for all chapters
        assert len(results) == 3
        assert all(isinstance(result, ChapterResult) for result in results)
        assert [result.chapter_number for result in results] == [1, 2, 3]
        assert [result.duration_ms for result in results] == [1, 2, 3]

    def test_orchestrator_cancel(self):
        """Test orchestrator cancellation."""
        config = ParallelConfig()
        orchestrator = ParallelPipelineOrchestrator(config)

        orchestrator.cancel()
        assert orchestrator._cancelled is True


class TestPipelineIntegration:
    """Integration tests for parallel pipeline with orchestrator."""

    def test_pipeline_parallel_mode_detection(self):
        """Test pipeline detects and enables parallel mode."""
        config = PipelineConfig(
            enable_parallel=True,
            max_parallel_chapters=2,
            memory_policy=_PermissiveMemoryPolicy(),
        )

        pipeline = ConversionPipeline(config)

        # Should detect parallel availability
        if PARALLEL_AVAILABLE:
            assert pipeline.is_parallel is True
        else:
            assert pipeline.is_parallel is False

    def test_pipeline_sequential_fallback(self):
        """Test pipeline falls back to sequential when parallel disabled."""
        config = PipelineConfig(enable_parallel=False)
        pipeline = ConversionPipeline(config)

        assert pipeline.is_parallel is False

    def test_pipeline_parallel_mode_uses_worker_plan_enablement(self):
        config = PipelineConfig(
            enable_parallel=True,
            max_parallel_chapters=1,
            requested_parallel_chapters=1,
        )

        pipeline = ConversionPipeline(config)

        assert config.enable_parallel is True
        assert config.worker_plan.enable_parallel is False
        assert pipeline.is_parallel is False
        assert pipeline._parallel_config is None

    def test_pipeline_vram_config_adjustment(self):
        """Test pipeline keeps requested config while the worker plan carries the cap."""
        config = PipelineConfig(
            enable_parallel=True,
            max_parallel_chapters=10,  # Unrealistically high
            total_vram_gb=16.0,
        )

        pipeline = ConversionPipeline(config)

        assert pipeline.config.max_parallel_chapters == 10
        assert pipeline.config.worker_plan.requested_workers == 10
        assert pipeline._parallel_config is not None
        assert pipeline._parallel_config.max_workers <= 4

    def test_safe_memory_clamps_parallel_startup(self, tmp_path):
        """Low memory policy should force an effective sequential plan."""

        class LowMemoryPolicy:
            safe_mode_reason = "test low memory"

            def allowed_workers(self, requested_max):
                assert requested_max == 4
                return 1

        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=4,
            total_vram_gb=64.0,
            memory_policy=LowMemoryPolicy(),
        )

        pipeline = ConversionPipeline(config)

        assert pipeline.config.max_parallel_chapters == 4
        assert pipeline.config.enable_parallel is True
        assert pipeline.config.worker_plan.requested_workers == 4
        assert pipeline.config.worker_plan.max_workers == 1
        assert pipeline.is_parallel is True

    def test_performance_snapshot_reports_worker_plan_after_raw_mode_mutation(
        self, tmp_path
    ):
        class MemoryPolicy:
            safe_mode_reason = "memory headroom unknown"

            def allowed_workers(self, requested_max):
                return requested_max

        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=3,
            total_vram_gb=64.0,
            memory_policy=MemoryPolicy(),
            parallel_worker_mode="manual",
            tts_engine="other",
            cleaner_backend="api",
        )
        pipeline = ConversionPipeline(config)
        config.parallel_worker_mode = "auto"

        snapshot = pipeline._performance_snapshot()  # noqa: SLF001

        assert snapshot["parallel_worker_mode"] == "manual"
        assert snapshot["memory_safe_mode"] is False
        assert "safe_mode_reason" not in snapshot

    def test_safe_memory_reason_in_performance_snapshot(self, tmp_path):
        class MemoryPolicy:
            safe_mode_reason = "memory headroom unknown"

            def allowed_workers(self, requested_max):
                return requested_max

        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=2,
            total_vram_gb=64.0,
            memory_policy=MemoryPolicy(),
        )

        pipeline = ConversionPipeline(config)
        snapshot = pipeline._performance_snapshot()  # noqa: SLF001

        assert snapshot["safe_mode_reason"] == "memory headroom unknown"
        assert snapshot["memory_safe_mode"] is True

    def test_shared_pool_fingerprint_includes_tts_engine_identity(self):
        kokoro = ConversionPipeline(
            PipelineConfig(
                tts_engine="kokoro",
                tts_model_name="mlx-community/Kokoro-82M",
                tts_quantization="bf16",
                enable_parallel=False,
            )
        )
        alternate = ConversionPipeline(
            PipelineConfig(
                tts_engine="future_mlx",
                tts_model_name="mlx-community/Kokoro-82M",
                tts_quantization="bf16",
                enable_parallel=False,
            )
        )

        assert kokoro._shared_pool_fingerprint() != alternate._shared_pool_fingerprint()

    def test_async_chapter_processing(self):
        """Test async chapter processing coordination - simplified."""
        # Skip this test as it requires complex mocking with parallel execution
        pytest.skip("Complex async test - tested via integration tests")

    def test_sequential_pipelining_overlaps_encode_with_next_prepare(self, tmp_path):
        """Sequential mode should overlap encode(N) with prepare(N+1) when pipelining is on."""
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
            enable_pipelining=True,
        )
        pipeline = ConversionPipeline(config)
        chapters = [
            MockChapter(1, "One", "a"),
            MockChapter(2, "Two", "b"),
            MockChapter(3, "Three", "c"),
        ]
        timings: dict[str, float] = {}

        def _fake_prepare(*, chapter, chapter_idx, total_chapters, output_dir):
            _ = chapter, total_chapters, output_dir
            timings[f"prepare_start_{chapter_idx}"] = time.perf_counter()
            time.sleep(0.03)
            timings[f"prepare_end_{chapter_idx}"] = time.perf_counter()
            return (
                ChapterResult(
                    chapter_number=chapter_idx + 1,
                    chapter_title=f"Chapter {chapter_idx + 1}",
                ),
                np.zeros(128, dtype=np.float32),
                False,
            )

        def _fake_finalize(
            *, result, chapter_wav, chapter, chapter_idx, total_chapters, output_dir
        ):
            _ = chapter_wav, chapter, total_chapters
            timings[f"encode_start_{chapter_idx}"] = time.perf_counter()
            time.sleep(0.05)
            timings[f"encode_end_{chapter_idx}"] = time.perf_counter()
            mp3_path = output_dir / f"{chapter_idx + 1:02d}.mp3"
            mp3_path.parent.mkdir(parents=True, exist_ok=True)
            mp3_path.write_bytes(b"mp3")
            result.mp3_path = mp3_path
            result.duration_ms = 1000
            return result

        with (
            patch.object(
                pipeline.stages.chapter_processor,
                "prepare_chapter_audio",
                side_effect=_fake_prepare,
            ),
            patch.object(
                pipeline.stages.chapter_processor,
                "finalize_chapter_encoding",
                side_effect=_fake_finalize,
            ),
        ):
            results = pipeline.stages.process_chapters_sequential(chapters, config.output_dir)

        assert len(results) == 3
        assert timings["encode_start_0"] < timings["prepare_end_1"]
        assert timings["encode_start_1"] < timings["prepare_end_2"]


class TestParallelOrchestratorFacade:
    """Tests for shared-manager-backed orchestrator facade behavior."""

    def test_orchestrator_uses_borrowed_shared_manager_without_shutdown(self):
        class _BorrowedManager:
            def __init__(self):
                self.run_calls = []
                self.shutdown_calls = 0

            def run_parallel(self, **kwargs):
                self.run_calls.append(kwargs)
                return ["borrowed-result"]

            def shutdown(self):
                self.shutdown_calls += 1

        manager = _BorrowedManager()
        orchestrator = ParallelPipelineOrchestrator(
            ParallelConfig(max_workers=2, use_processes=False),
            pool_manager=manager,
            config_fingerprint="borrowed-fingerprint",
            worker_init_config={"voice": "af_bella"},
        )

        results = orchestrator.process_chapters_parallel(
            chapters=[MockChapter(1, "One", "alpha")],
            process_func=lambda ch, idx, _fm: {"idx": idx},  # noqa: ARG005
        )

        assert results == ["borrowed-result"]
        assert len(manager.run_calls) == 1
        assert manager.run_calls[0]["config_fingerprint"] == "borrowed-fingerprint"
        assert manager.run_calls[0]["worker_init_config"] == {"voice": "af_bella"}
        assert manager.shutdown_calls == 0

    def test_orchestrator_creates_and_shuts_down_owned_manager(self):
        created: list[SharedWorkerPoolManager] = []

        class _OwnedManager:
            def __init__(
                self,
                max_workers: int,
                available_vram_gb: float | None = None,
                available_device_memory_gb: float | None = None,
            ):
                self.max_workers = max_workers
                self.available_vram_gb = (
                    available_device_memory_gb
                    if available_device_memory_gb is not None
                    else available_vram_gb
                )
                self.run_calls = []
                self.shutdown_calls = 0
                created.append(self)

            def run_parallel(self, **kwargs):
                self.run_calls.append(kwargs)
                return ["owned-result"]

            def shutdown(self):
                self.shutdown_calls += 1

        config = ParallelConfig(max_workers=3, use_processes=False)
        with patch("modules.pipeline.parallel.SharedWorkerPoolManager", _OwnedManager):
            orchestrator = ParallelPipelineOrchestrator(config)
            results = orchestrator.process_chapters_parallel(
                chapters=[MockChapter(1, "One", "alpha")],
                process_func=lambda ch, idx, _fm: {"idx": idx},  # noqa: ARG005
            )

        assert results == ["owned-result"]
        assert len(created) == 1
        assert created[0].max_workers == 3
        assert created[0].available_vram_gb == config.vram_budget.available_vram_gb
        assert created[0].shutdown_calls == 1


class TestParallelPublicSurface:
    """Tests for the surviving public parallel API."""

    def test_parallel_modules_do_not_export_legacy_symbols(self):
        parallel_module = importlib.import_module("modules.pipeline.parallel")

        for name in [
            "AsyncFileManager",
            "PipelinedStage",
            "ChapterWorkerPool",
            "PipelineTask",
            "StageStatus",
        ]:
            assert not hasattr(parallel_module, name)
            assert name not in pipeline_module.__all__


class TestPipelineConfigOptions:
    """Tests for pipeline configuration options."""

    def test_config_defaults(self):
        """Test default configuration values."""
        config = PipelineConfig()

        assert config.voice == "am_adam"
        assert config.speed == 1.0
        assert config.chunk_size == 700
        assert config.enable_parallel is True
        assert config.max_parallel_chapters == 2
        assert config.enable_pipelining is True
        assert config.enable_tts_file_batch_results is False
        assert config.keep_tts_engine_warm is True
        assert config.tts_idle_timeout_seconds == 300.0
        assert config.emit_chapter_mp3_artifacts is False
        assert config.total_vram_gb >= 2.0
        assert "use_process_pool" not in PipelineConfig.__dataclass_fields__
        assert "enable_async_io" not in PipelineConfig.__dataclass_fields__

    def test_config_custom_values(self):
        """Test custom configuration values."""
        config = PipelineConfig(
            voice="af_bella",
            speed=1.2,
            chunk_size=1000,
            enable_parallel=False,
            max_parallel_chapters=4,
            total_vram_gb=64.0,
        )

        assert config.voice == "af_bella"
        assert config.speed == 1.2
        assert config.chunk_size == 1000
        assert config.enable_parallel is False
        assert config.max_parallel_chapters == 4
        assert config.total_vram_gb == 64.0

    def test_tts_runtime_is_derived_from_supplied_typed_runtime_options(self):
        runtime_options = TTSRuntimeOptions(
            model_name="Kokoro-82M",
            quantization="bf16",
            provider="mlx",
            device="mps",
            dtype="fp16",
        )

        config = PipelineConfig(tts_runtime_options=runtime_options)

        assert config.tts_runtime == TTSRuntimeSpec.from_legacy(
            "kokoro",
            "bf16",
            model="Kokoro-82M",
            provider="mlx",
            device="mps",
            options={"dtype": "fp16"},
        )
        assert config.tts_engine == "kokoro"
        assert config.tts_model_name == "Kokoro-82M"
        assert config.tts_quantization == "bf16"

    def test_tts_runtime_options_must_match_supplied_tts_runtime(self):
        runtime_spec = TTSRuntimeSpec.from_legacy(
            "kokoro",
            "bf16",
            model="Kokoro-82M",
            provider="mlx",
        )
        runtime_options = TTSRuntimeOptions(
            model_name="Kokoro-82M",
            quantization="bf16",
            provider="cuda",
            device="cuda:0",
            dtype="fp16",
        )

        with pytest.raises(ValueError, match="tts_runtime_options disagree with tts_runtime"):
            PipelineConfig(
                tts_runtime=runtime_spec,
                tts_runtime_options=runtime_options,
            )

    def test_detect_available_ram_gb_caches_fallback_result(self):
        parallel_module = importlib.import_module("modules.pipeline.parallel")
        parallel_module._detect_available_ram_gb.cache_clear()

        real_import = __import__
        calls = {"psutil": 0}

        def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "psutil":
                calls["psutil"] += 1
                raise ImportError("no psutil")
            return real_import(name, globals, locals, fromlist, level)

        with (
            patch(
                "modules.pipeline.parallel.os.sysconf",
                side_effect=AttributeError("missing"),
            ),
            patch("builtins.__import__", side_effect=_fake_import),
        ):
            first = parallel_module._detect_available_ram_gb()
            second = parallel_module._detect_available_ram_gb()

        assert first == 8.0
        assert second == 8.0
        assert calls["psutil"] == 1


class TestPipelineAdaptiveWorkers:
    """Tests for adaptive sequential/parallel worker decisions."""

    def test_adaptive_worker_selection_uses_total_book_size(self):
        assert (
            adaptive_parallel_workers(
                total_chapters=3,
                total_chars=30_000,
                max_parallel_chapters=4,
            )
            == 1
        )

        assert (
            adaptive_parallel_workers(
                total_chapters=4,
                total_chars=120_000,
                max_parallel_chapters=4,
            )
            >= 2
        )

        assert (
            adaptive_parallel_workers(
                total_chapters=4,
                total_chars=900_000,
                max_parallel_chapters=4,
            )
            >= 3
        )

    def test_effective_workers_honors_worker_plan_cpu_core_policy(self):
        plan = WorkerPlan(
            requested_workers=4,
            max_workers=4,
            enable_parallel=True,
            worker_mode="auto",
            worker_auto_mode="cpu_cores",
            process_safe=True,
            enforce_vram_budget=False,
            initial_reason="process-safe CPU-core auto worker policy",
        )

        selection = effective_workers(
            worker_plan=plan,
            total_chapters=4,
            total_chars=400_000,
            parallel_capacity=4,
            adaptive_selector=lambda **_kwargs: 1,
        )

        assert selection.count == 4
        assert selection.reason == "process-safe CPU-core auto worker policy"

    def test_dispatch_chapters_uses_worker_plan_not_raw_local_mlx_fields(
        self, tmp_path
    ):
        chapters = [
            MockChapter(1, "One", "x" * 100_000),
            MockChapter(2, "Two", "x" * 100_000),
            MockChapter(3, "Three", "x" * 100_000),
            MockChapter(4, "Four", "x" * 100_000),
        ]
        calls = []
        plan = WorkerPlan(
            requested_workers=4,
            max_workers=4,
            enable_parallel=True,
            worker_mode="auto",
            worker_auto_mode="cpu_cores",
            process_safe=True,
            enforce_vram_budget=False,
            initial_reason="process-safe CPU-core auto worker policy",
        )
        pipeline = types.SimpleNamespace(
            _total_chars=400_000,
            config=types.SimpleNamespace(
                worker_plan=plan,
                max_parallel_chapters=99,
                requested_parallel_chapters=99,
                parallel_worker_mode="auto",
                tts_engine="kokoro",
                cleaner_backend="mlx",
                tts_worker_auto_mode="memory_safe",
                tts_process_safe=False,
                memory_policy=None,
            ),
            _parallel_config=types.SimpleNamespace(max_workers=4),
            is_parallel=True,
            _parallel_execution_active=False,
            _log_verbose=lambda message, level="info": calls.append((message, level)),
        )
        _attach_dispatch_stage_runner(pipeline, calls)

        results = dispatch_chapters(pipeline, chapters, tmp_path / "chapters")

        assert results == [("parallel", 4, tmp_path / "chapters")]
        assert pipeline._selected_parallel_chapters == 4
        assert (
            pipeline._selected_parallel_worker_count_reason
            == "process-safe CPU-core auto worker policy"
        )
        assert ("warmup", "info") not in calls

    def test_dispatch_chapters_applies_adaptive_guard_before_selecting_sequential_mode(
        self, tmp_path
    ):
        chapters = [
            MockChapter(1, "One", "tiny"),
            MockChapter(2, "Two", "tiny"),
            MockChapter(3, "Three", "tiny"),
        ]
        calls = []
        pipeline = types.SimpleNamespace(
            _total_chars=30_000,
            config=types.SimpleNamespace(
                worker_plan=WorkerPlan(
                    requested_workers=4,
                    max_workers=4,
                    enable_parallel=True,
                    worker_mode="auto",
                    worker_auto_mode="memory_safe",
                    process_safe=False,
                ),
                max_parallel_chapters=4,
                parallel_worker_mode="auto",
                tts_engine="other",
                cleaner_backend="api",
            ),
            _parallel_config=types.SimpleNamespace(max_workers=4),
            is_parallel=True,
            _parallel_execution_active=False,
            _log_verbose=lambda message, level="info": calls.append((message, level)),
        )
        _attach_dispatch_stage_runner(pipeline, calls)

        results = dispatch_chapters(pipeline, chapters, tmp_path / "chapters")

        assert results == [("sequential", 3, tmp_path / "chapters")]
        assert pipeline.config.max_parallel_chapters == 4
        assert pipeline._parallel_config.max_workers == 4
        assert pipeline._selected_parallel_chapters == 1
        assert ("warmup", "info") in calls

    def test_dispatch_chapters_manual_mode_bypasses_adaptive_guard(self, tmp_path):
        chapters = [
            MockChapter(1, "One", "tiny"),
            MockChapter(2, "Two", "tiny"),
            MockChapter(3, "Three", "tiny"),
        ]
        calls = []
        pipeline = types.SimpleNamespace(
            _total_chars=30_000,
            config=types.SimpleNamespace(
                worker_plan=WorkerPlan(
                    requested_workers=3,
                    max_workers=3,
                    enable_parallel=True,
                    worker_mode="manual",
                    worker_auto_mode="memory_safe",
                    process_safe=False,
                    initial_reason="manual worker count",
                ),
                max_parallel_chapters=3,
                parallel_worker_mode="manual",
                tts_engine="kokoro",
                cleaner_backend="mlx",
                memory_policy=None,
            ),
            _parallel_config=types.SimpleNamespace(max_workers=3),
            is_parallel=True,
            _parallel_execution_active=False,
            _log_verbose=lambda message, level="info": calls.append((message, level)),
        )
        _attach_dispatch_stage_runner(pipeline, calls)

        results = dispatch_chapters(pipeline, chapters, tmp_path / "chapters")

        assert results == [("parallel", 3, tmp_path / "chapters")]
        assert pipeline.config.max_parallel_chapters == 3
        assert pipeline._parallel_config.max_workers == 3
        assert pipeline._selected_parallel_chapters == 3
        assert pipeline._selected_parallel_worker_count_reason == "manual worker count"
        assert ("warmup", "info") not in calls

    def test_dispatch_chapters_auto_mode_keeps_local_mlx_pipeline_sequential(
        self, tmp_path
    ):
        chapters = [
            MockChapter(1, "One", "x" * 100_000),
            MockChapter(2, "Two", "x" * 100_000),
            MockChapter(3, "Three", "x" * 100_000),
        ]
        calls = []
        pipeline = types.SimpleNamespace(
            _total_chars=300_000,
            config=types.SimpleNamespace(
                worker_plan=WorkerPlan(
                    requested_workers=3,
                    max_workers=1,
                    enable_parallel=True,
                    worker_mode="auto",
                    worker_auto_mode="memory_safe",
                    process_safe=False,
                    initial_reason="local MLX model contention guard",
                ),
                max_parallel_chapters=3,
                parallel_worker_mode="auto",
                tts_engine="kokoro",
                cleaner_backend="mlx",
            ),
            _parallel_config=types.SimpleNamespace(max_workers=3),
            is_parallel=True,
            _parallel_execution_active=False,
            _log_verbose=lambda message, level="info": calls.append((message, level)),
        )
        _attach_dispatch_stage_runner(pipeline, calls)

        results = dispatch_chapters(pipeline, chapters, tmp_path / "chapters")

        assert results == [("sequential", 3, tmp_path / "chapters")]
        assert pipeline._selected_parallel_chapters == 1
        assert (
            pipeline._selected_parallel_worker_count_reason
            == "local MLX model contention guard"
        )
        assert ("warmup", "info") in calls

    def test_manual_worker_mode_preserves_requested_workers_despite_memory_policy(self, tmp_path):
        class _ClampMemoryPolicy:
            safe_mode_reason = "memory headroom below safe reserve"
            selected_worker_count_reason = "memory headroom below safe reserve"
            measured_warmup_delta_bytes = None
            estimated_per_worker_memory_bytes = 4 * 1024**3

            def allowed_workers(self, requested_max):
                raise AssertionError("manual worker mode should not ask memory policy to clamp workers")

        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=3,
            parallel_worker_mode="manual",
            requested_parallel_chapters=3,
            memory_policy=_ClampMemoryPolicy(),
        )
        pipeline = ConversionPipeline(cfg)

        snapshot = pipeline._performance_snapshot()  # noqa: SLF001

        assert snapshot["parallel_worker_mode"] == "manual"
        assert snapshot["requested_parallel_chapters"] == 3
        assert snapshot["effective_parallel_chapters"] == 3
        assert snapshot["max_workers"] == 3
        assert snapshot["memory_safe_mode"] is False
        assert "safe_mode_reason" not in snapshot

    def test_manual_worker_mode_does_not_throttle_shared_pool_submissions_with_memory_policy(self, tmp_path):
        captured: dict[str, object] = {}

        class _ClampMemoryPolicy:
            def should_submit_more(self, active_workers):
                _ = active_workers
                raise AssertionError("manual worker mode should not throttle shared pool submissions")

        class _SharedPoolManager:
            def run_parallel(self, **kwargs):
                captured.update(kwargs)
                return [
                    ChapterResult(
                        chapter_number=1,
                        chapter_title="One",
                        duration_ms=1000,
                    )
                ]

        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=4,
            requested_parallel_chapters=4,
            parallel_worker_mode="manual",
            memory_policy=_ClampMemoryPolicy(),
            shared_worker_pool_manager=_SharedPoolManager(),
        )
        pipeline = ConversionPipeline(cfg)
        cfg.parallel_worker_mode = "auto"
        chapter = types.SimpleNamespace(number=1, title="One", content="body")

        results = pipeline.stages.process_chapters_parallel([chapter, chapter], tmp_path)  # noqa: SLF001

        assert cfg.worker_plan.worker_mode == "manual"
        assert cfg.parallel_worker_mode == "auto"
        assert [result.chapter_number for result in results] == [1]
        assert captured["max_workers"] == 4
        assert captured["memory_policy"] is None

    def test_manual_worker_mode_submits_requested_workers_at_shared_pool_boundary(self, tmp_path):
        class _ClampMemoryPolicy:
            def should_submit_more(self, active_workers):
                _ = active_workers
                raise AssertionError("manual worker mode should not call runtime memory throttles")

        class _AsyncTrackingExecutor:
            instances: list["_AsyncTrackingExecutor"] = []

            def __init__(self, *args, **kwargs):  # noqa: ANN003
                _ = args, kwargs
                self.release = threading.Event()
                self._lock = threading.Lock()
                self.in_flight = 0
                self.max_in_flight = 0
                self.submit_calls = 0
                type(self).instances.append(self)

            def submit(self, fn, *args, **kwargs):  # noqa: ANN003
                future: Future = Future()
                with self._lock:
                    self.submit_calls += 1
                    self.in_flight += 1
                    self.max_in_flight = max(self.max_in_flight, self.in_flight)

                def _run() -> None:
                    self.release.wait(timeout=3)
                    try:
                        result = fn(*args, **kwargs)
                    except Exception as exc:  # pragma: no cover - defensive
                        with self._lock:
                            self.in_flight -= 1
                        future.set_exception(exc)
                        return

                    with self._lock:
                        self.in_flight -= 1
                    future.set_result(result)

                threading.Thread(target=_run, daemon=True).start()
                return future

            def shutdown(self, wait: bool = True, cancel_futures: bool = True):  # noqa: ARG002
                self.release.set()

        def _fake_worker_wrapper(chapter, chapter_idx, _file_manager, process_func):  # noqa: ANN001
            _ = process_func, _file_manager
            return {
                "chapter_number": chapter.number,
                "chapter_title": chapter.title,
                "duration_ms": chapter_idx + 1,
            }

        chapters = [
            MockChapter(1, "One"),
            MockChapter(2, "Two"),
            MockChapter(3, "Three"),
            MockChapter(4, "Four"),
        ]
        shared_pool = SharedWorkerPoolManager(max_workers=4)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=4,
            requested_parallel_chapters=4,
            parallel_worker_mode="manual",
            memory_policy=_ClampMemoryPolicy(),
            shared_worker_pool_manager=shared_pool,
        )
        pipeline = ConversionPipeline(cfg)
        results: list[ChapterResult] = []

        def _run_pipeline() -> None:
            with (
                patch("modules.pipeline.pool_manager.ProcessPoolExecutor", _AsyncTrackingExecutor),
                patch("modules.pipeline.parallel._process_chapter_wrapper", _fake_worker_wrapper),
            ):
                results.extend(pipeline.stages.process_chapters_parallel(chapters, tmp_path))  # noqa: SLF001

        thread = threading.Thread(target=_run_pipeline)
        thread.start()

        deadline = time.perf_counter() + 2
        while time.perf_counter() < deadline:
            if _AsyncTrackingExecutor.instances:
                executor = _AsyncTrackingExecutor.instances[0]
                if executor.submit_calls == 4:
                    break
            time.sleep(0.01)
        else:
            raise AssertionError("manual mode did not submit the requested worker count")

        executor = _AsyncTrackingExecutor.instances[0]
        assert executor.submit_calls == 4
        assert executor.max_in_flight == 4

        executor.release.set()
        thread.join(timeout=3)
        shared_pool.shutdown(timeout_seconds=0.0)

        assert not thread.is_alive()
        assert [result.chapter_number for result in results] == [1, 2, 3, 4]

    def test_auto_mode_runtime_backpressure_updates_log_before_parallel_dispatch(self, tmp_path):
        class _RuntimeOneWorkerPolicy:
            enable_warmup_calibration = False
            measured_warmup_delta_bytes = 1024
            safe_mode_reason = None
            selected_worker_count_reason = None

            def should_submit_more(self, active_workers: int) -> bool:
                return active_workers == 0

        chapters = [
            MockChapter(1, "One", "x" * 100_000),
            MockChapter(2, "Two", "x" * 100_000),
            MockChapter(3, "Three", "x" * 100_000),
            MockChapter(4, "Four", "x" * 100_000),
        ]
        calls = []
        pipeline = types.SimpleNamespace(
            _total_chars=800_000,
            config=types.SimpleNamespace(
                worker_plan=WorkerPlan(
                    requested_workers=4,
                    max_workers=4,
                    enable_parallel=True,
                    worker_mode="auto",
                    worker_auto_mode="memory_safe",
                    process_safe=False,
                ),
                max_parallel_chapters=4,
                requested_parallel_chapters=4,
                parallel_worker_mode="auto",
                tts_engine="other",
                cleaner_backend="api",
                memory_policy=_RuntimeOneWorkerPolicy(),
            ),
            _parallel_config=types.SimpleNamespace(max_workers=4),
            is_parallel=True,
            _parallel_execution_active=False,
            _log_verbose=lambda message, level="info": calls.append((message, level)),
        )
        _attach_dispatch_stage_runner(pipeline, calls)

        results = dispatch_chapters(pipeline, chapters, tmp_path / "chapters")

        assert results == [("sequential", 4, tmp_path / "chapters")]
        assert pipeline._selected_parallel_chapters == 1
        assert pipeline._parallel_config.max_workers == 4
        assert not any("Using 4 concurrent workers" in message for message, _level in calls)
        assert any("Runtime backpressure guard: 4 -> 1" in message for message, _level in calls)

    def test_performance_snapshot_reports_stage_throughput_counters(self, tmp_path):
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
        )
        pipeline = ConversionPipeline(cfg)
        pipeline._performance_tts_chars = 1_000  # noqa: SLF001
        pipeline._performance_tts_seconds = 2.0  # noqa: SLF001
        pipeline._performance_encode_chapters = 3  # noqa: SLF001
        pipeline._performance_encode_seconds = 6.0  # noqa: SLF001

        snapshot = pipeline._performance_snapshot()  # noqa: SLF001

        assert snapshot["tts_chars_per_second"] == 500.0
        assert snapshot["encoder_chapters_per_second"] == 0.5

    def test_dispatch_chapters_auto_kokoro_sequential_policy_keeps_one_worker(
        self, tmp_path
    ):
        chapters = [
            MockChapter(1, "One", "x" * 100_000),
            MockChapter(2, "Two", "x" * 100_000),
            MockChapter(3, "Three", "x" * 100_000),
        ]
        calls = []
        pipeline = types.SimpleNamespace(
            _total_chars=300_000,
            config=types.SimpleNamespace(
                worker_plan=WorkerPlan(
                    requested_workers=4,
                    max_workers=1,
                    enable_parallel=True,
                    worker_mode="auto",
                    worker_auto_mode="sequential",
                    process_safe=False,
                    initial_reason="backend sequential auto worker policy",
                ),
                max_parallel_chapters=4,
                parallel_worker_mode="auto",
                tts_engine="kokoro",
                cleaner_backend="api",
                tts_worker_auto_mode="sequential",
                tts_process_safe=False,
            ),
            _parallel_config=types.SimpleNamespace(max_workers=4),
            is_parallel=True,
            _parallel_execution_active=False,
            _log_verbose=lambda message, level="info": calls.append((message, level)),
        )
        _attach_dispatch_stage_runner(pipeline, calls)

        results = dispatch_chapters(pipeline, chapters, tmp_path / "chapters")

        assert results == [("sequential", 3, tmp_path / "chapters")]
        assert pipeline.config.max_parallel_chapters == 4
        assert pipeline._parallel_config.max_workers == 4
        assert pipeline._selected_parallel_chapters == 1
        assert (
            pipeline._selected_parallel_worker_count_reason
            == "backend sequential auto worker policy"
        )
        assert ("warmup", "info") in calls


class TestWorkerProgressTotals:
    """Tests for chapter-total propagation into worker execution."""

    def test_worker_init_config_excludes_parent_shared_pool_manager(self, tmp_path):
        manager = SharedWorkerPoolManager(max_workers=2)
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=True,
            max_parallel_chapters=2,
            shared_worker_pool_manager=manager,
        )
        pipeline = ConversionPipeline(config)

        try:
            worker_init_config = pipeline._build_worker_init_config()  # noqa: SLF001
            encoded = pickle.dumps(worker_init_config)
        finally:
            manager.shutdown(timeout_seconds=0)

        assert encoded
        assert "shared_worker_pool_manager" not in worker_init_config["pipeline_config"]

    def test_worker_init_config_serializes_direct_cleaner_api_key(self, tmp_path):
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            cleaner_backend="api",
            cleaner_api_key="pasted-key",
        )
        pipeline = ConversionPipeline(config)

        worker_init_config = pipeline._build_worker_init_config()  # noqa: SLF001

        assert worker_init_config["cleaner_api_key"] == "pasted-key"
        assert worker_init_config["pipeline_config"]["cleaner_api_key"] == "pasted-key"

    def test_worker_init_config_serializes_cleaner_skip_llm_experiment(self, tmp_path):
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            cleaner_skip_llm_when_not_needed=True,
        )
        pipeline = ConversionPipeline(config)

        worker_init_config = pipeline._build_worker_init_config()  # noqa: SLF001

        assert worker_init_config["cleaner_skip_llm_when_not_needed"] is True
        assert (
            worker_init_config["pipeline_config"]["cleaner_skip_llm_when_not_needed"]
            is True
        )

    def test_worker_init_config_serializes_tts_runtime_spec(self, tmp_path):
        runtime_spec = TTSRuntimeSpec(
            provider="cuda",
            engine="kokoro",
            model="Kokoro-82M",
            quantization="bf16",
            device="cuda:0",
            runtime_options=TTSRuntimeSpec.canonicalize_runtime_options({"dtype": "fp16"}),
        )
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            tts_runtime=runtime_spec,
        )
        pipeline = ConversionPipeline(config)

        worker_init_config = pipeline._build_worker_init_config()  # noqa: SLF001
        worker_config = build_worker_pipeline_config(worker_init_config["pipeline_config"])

        assert worker_init_config["tts_runtime"] == runtime_spec.to_dict()
        assert "worker_environment" not in worker_init_config
        assert "tts_runtime_cache_key" not in worker_init_config
        assert worker_init_config["pipeline_config"]["tts_runtime"] == runtime_spec.to_dict()
        assert worker_init_config["pipeline_config"]["device_memory_policy"] == {
            "provider": "cuda",
            "device": "cuda:0",
        }
        assert "tts_runtime_cache_key" not in worker_init_config["pipeline_config"]
        derived_cache_key = TTSRuntimeSpec.from_serialized(
            worker_init_config["tts_runtime"],
        ).cache_key()
        assert derived_cache_key == runtime_spec.cache_key()
        assert worker_config.tts_runtime == runtime_spec
        assert worker_config.tts_engine == "kokoro"
        assert worker_config.tts_model_name == "Kokoro-82M"
        assert worker_config.tts_quantization == "bf16"
        assert worker_config.device_memory_policy.provider == "cuda"

    def test_worker_init_config_serializes_mlx_device_memory_policy(self, tmp_path):
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            total_vram_gb=16.0,
        )
        pipeline = ConversionPipeline(config)

        worker_init_config = pipeline._build_worker_init_config()  # noqa: SLF001

        assert "worker_environment" not in worker_init_config
        assert worker_init_config["pipeline_config"]["device_memory_policy"] == {
            "provider": "mlx",
            "device": None,
        }

    def test_worker_init_config_preserves_typed_tts_runtime_options(self, tmp_path):
        options = {
            "use_tf32": True,
            "device_id": 1,
            "temperature": 0.25,
            "optional": None,
        }
        runtime_spec = TTSRuntimeSpec(
            provider="cuda",
            engine="kokoro",
            model="Kokoro-82M",
            quantization="bf16",
            device="cuda:0",
            runtime_options=TTSRuntimeSpec.canonicalize_runtime_options(options),
        )
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            tts_runtime=runtime_spec,
        )
        pipeline = ConversionPipeline(config)

        worker_init_config = pipeline._build_worker_init_config()  # noqa: SLF001
        worker_config = build_worker_pipeline_config(worker_init_config["pipeline_config"])

        assert worker_init_config["tts_runtime"]["options"] == {
            "device_id": "1",
            "optional": "null",
            "temperature": "0.25",
            "use_tf32": "true",
        }
        assert worker_init_config["tts_runtime"]["runtime_options"] == {
            "device_id": 1,
            "optional": None,
            "temperature": 0.25,
            "use_tf32": True,
        }
        assert worker_config.tts_runtime == runtime_spec
        assert worker_config.tts_runtime.factory_kwargs()["runtime_options"] == {
            "device_id": 1,
            "optional": None,
            "temperature": 0.25,
            "use_tf32": True,
        }

    def test_worker_init_caches_rehydrated_pipeline_config(self, tmp_path):
        from modules.pipeline.parallel import get_worker_cached_config

        runtime_spec = TTSRuntimeSpec.from_legacy(
            "kokoro",
            "bf16",
            model="Kokoro-82M",
            provider="mlx",
            options={"dtype": "fp16"},
        )
        config = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            tts_runtime=runtime_spec,
        )
        fake_engine = MagicMock()
        fake_cleaner = MagicMock()

        with (
            patch("modules.tts.factory.TTSEngineFactory.create", return_value=fake_engine),
            patch("modules.tts.cleaner.TextCleaner", return_value=fake_cleaner),
        ):
            _init_worker(
                available_vram_gb=8.0,
                active_workers=1,
                worker_init_config=build_worker_init_config(config),
            )

        cached_config = get_worker_cached_config()

        assert isinstance(cached_config, PipelineConfig)
        assert cached_config.tts_runtime == runtime_spec
        assert cached_config.enable_parallel is False

    def test_shared_pool_fingerprint_distinguishes_tts_runtime_spec_provider_device_model_quantization_and_options(
        self,
        tmp_path,
    ):
        first_spec = TTSRuntimeSpec(
            provider="cuda",
            engine="kokoro",
            model="Kokoro-82M",
            quantization="bf16",
            device="cuda:0",
            runtime_options=TTSRuntimeSpec.canonicalize_runtime_options({"dtype": "fp16"}),
        )
        second_spec = TTSRuntimeSpec(
            provider="cuda",
            engine="kokoro",
            model="Kokoro-82M",
            quantization="bf16",
            device="cuda:1",
            runtime_options=TTSRuntimeSpec.canonicalize_runtime_options({"dtype": "fp16"}),
        )
        third_spec = TTSRuntimeSpec(
            provider="cuda",
            engine="kokoro",
            model="Kokoro-82M-v2",
            quantization="bf16",
            device="cuda:0",
            runtime_options=TTSRuntimeSpec.canonicalize_runtime_options({"dtype": "fp16"}),
        )
        fourth_spec = TTSRuntimeSpec(
            provider="cuda",
            engine="kokoro",
            model="Kokoro-82M",
            quantization="q4",
            device="cuda:0",
            runtime_options=TTSRuntimeSpec.canonicalize_runtime_options({"dtype": "fp16"}),
        )
        fifth_spec = TTSRuntimeSpec(
            provider="cuda",
            engine="kokoro",
            model="Kokoro-82M",
            quantization="bf16",
            device="cuda:0",
            runtime_options=TTSRuntimeSpec.canonicalize_runtime_options({"dtype": "bf16"}),
        )
        sixth_spec = TTSRuntimeSpec.from_legacy(
            "kokoro",
            "bf16",
            model="Kokoro-82M",
            provider="mlx",
            device="mps",
            options={"dtype": "fp16"},
        )
        first_config = PipelineConfig(
            output_dir=tmp_path / "one",
            temp_dir=tmp_path / "temp-one",
            tts_runtime=first_spec,
        )
        variant_configs = [
            PipelineConfig(
                output_dir=tmp_path / f"variant-{index}",
                temp_dir=tmp_path / f"temp-variant-{index}",
                tts_runtime=runtime_spec,
            )
            for index, runtime_spec in enumerate(
                [second_spec, third_spec, fourth_spec, fifth_spec, sixth_spec],
                start=1,
            )
        ]

        first_fingerprint = build_shared_pool_fingerprint(first_config, max_workers=2)
        for variant_config in variant_configs:
            assert first_fingerprint != build_shared_pool_fingerprint(
                variant_config,
                max_workers=2,
            )

    def test_shared_pool_fingerprint_derives_runtime_spec_for_legacy_config(self):
        class _LegacyConfig:
            tts_runtime = None
            tts_engine = "kokoro"
            tts_model_name = "Kokoro-82M"
            tts_quantization = "bf16"
            voice = "am_adam"
            cleaner_model_name = "cleaner"
            cleaner_backend = "mlx"
            cleaner_api_base_url = "https://api.example.test/v1"
            cleaner_api_key_env = "OPENAI_API_KEY"
            cleaner_api_key = None
            cleaner_api_model = "gpt-test"
            cleaner_skip_llm_when_not_needed = True
            cleaner_api_global_concurrency = 4

        expected_spec = TTSRuntimeSpec.from_legacy(
            "kokoro",
            "bf16",
            model="Kokoro-82M",
        )

        assert build_shared_pool_fingerprint(_LegacyConfig(), max_workers=2)[0] == (
            expected_spec.cache_key()
        )

    def test_worker_entry_forwards_total_chapters(self, tmp_path):
        chapter = MockChapter(2, "Two", "body")

        with patch(
            "modules.pipeline.chapter_runners.ChapterProcessor.process_chapter_worker",
            return_value=ChapterResult(chapter_number=2, chapter_title="Two"),
        ) as mock_worker:
            _process_chapter_worker_entry(
                chapter=chapter,
                chapter_idx=1,
                _file_manager=None,
                output_dir=tmp_path,
                total_chapters=9,
            )

        assert mock_worker.call_args.kwargs["total_chapters"] == 9

    def test_process_chapter_worker_prefers_explicit_total(self, tmp_path):
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
        )
        chapter = MockChapter(3, "Three", "body")
        seen_total: dict[str, int] = {}

        def _fake_process(self, *, chapter, chapter_idx, total_chapters, output_dir):  # noqa: ANN001
            _ = chapter, chapter_idx, output_dir
            seen_total["value"] = total_chapters
            return ChapterResult(chapter_number=3, chapter_title="Three")

        def _config_dict(c):
            return {
                "tts_engine": c.tts_engine,
                "tts_model_name": c.tts_model_name,
                "tts_quantization": c.tts_quantization,
                "cleaner_model_name": c.cleaner_model_name,
                "cleaner_backend": c.cleaner_backend,
                "cleaner_api_base_url": c.cleaner_api_base_url,
                "cleaner_api_key_env": c.cleaner_api_key_env,
                "cleaner_api_model": c.cleaner_api_model,
                "save_cleaning_debug": c.save_cleaning_debug,
                "voice": c.voice,
                "speed": c.speed,
                "output_dir": c.output_dir,
                "temp_dir": c.temp_dir,
                "mp3_bitrate": c.mp3_bitrate,
                "emit_chapter_mp3_artifacts": c.emit_chapter_mp3_artifacts,
                "normalize_volume": c.normalize_volume,
                "target_dbfs": c.target_dbfs,
                "chunk_size": c.chunk_size,
                "enable_pipelining": c.enable_pipelining,
                "enable_chunk_overlap": c.enable_chunk_overlap,
                "cleaner_api_global_concurrency": c.cleaner_api_global_concurrency,
                "cleaner_api_global_limiter": c.cleaner_api_global_limiter,
                "shared_worker_pool_manager": c.shared_worker_pool_manager,
            }

        with (
            patch.object(PipelineStages, "process_chapter", _fake_process),
            patch(
                "modules.pipeline.orchestrator.get_worker_cached_config",
                return_value=_config_dict(cfg),
            ),
        ):
            PipelineStages.process_chapter_worker(
                chapter=chapter,
                chapter_idx=2,
                output_dir=tmp_path,
                total_chapters=12,
                tts_engine=object(),
                text_cleaner=object(),
            )

        assert seen_total["value"] == 12

    def test_process_chapter_worker_attaches_stage_timings(self, tmp_path):
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            save_cleaning_debug=False,
        )
        chapter = MockChapter(1, "One", "body")

        def _config_dict(c):
            return {
                "tts_engine": c.tts_engine,
                "tts_model_name": c.tts_model_name,
                "tts_quantization": c.tts_quantization,
                "cleaner_model_name": c.cleaner_model_name,
                "cleaner_backend": c.cleaner_backend,
                "cleaner_api_base_url": c.cleaner_api_base_url,
                "cleaner_api_key_env": c.cleaner_api_key_env,
                "cleaner_api_model": c.cleaner_api_model,
                "save_cleaning_debug": c.save_cleaning_debug,
                "voice": c.voice,
                "speed": c.speed,
                "output_dir": c.output_dir,
                "temp_dir": c.temp_dir,
                "mp3_bitrate": c.mp3_bitrate,
                "emit_chapter_mp3_artifacts": c.emit_chapter_mp3_artifacts,
                "normalize_volume": c.normalize_volume,
                "target_dbfs": c.target_dbfs,
                "chunk_size": c.chunk_size,
                "enable_pipelining": c.enable_pipelining,
                "enable_chunk_overlap": c.enable_chunk_overlap,
                "cleaner_api_global_concurrency": c.cleaner_api_global_concurrency,
                "cleaner_api_global_limiter": c.cleaner_api_global_limiter,
                "shared_worker_pool_manager": c.shared_worker_pool_manager,
            }

        def _fake_ensure_tts_engine(_pipeline):  # noqa: ANN001
            time.sleep(0.001)

        def _fake_clean_trace(**_kwargs):  # noqa: ANN001
            time.sleep(0.001)
            return ["body"], ["body"], [], False

        def _fake_synthesize(**_kwargs):  # noqa: ANN001
            time.sleep(0.001)
            return np.zeros(128, dtype=np.float32)

        with (
            patch(
                "modules.pipeline.orchestrator.get_worker_cached_config",
                return_value=_config_dict(cfg),
            ),
            patch(
                "modules.pipeline.model_lifecycle.ensure_chapter_tts_engine",
                side_effect=_fake_ensure_tts_engine,
            ),
            patch(
                "modules.pipeline.stages.cleaning.clean_chunks_with_trace",
                side_effect=_fake_clean_trace,
            ),
            patch(
                "modules.pipeline.stages.synthesis.synthesize_chapter_chunks",
                side_effect=_fake_synthesize,
            ),
        ):
            result = PipelineStages.process_chapter_worker(
                chapter=chapter,
                chapter_idx=0,
                output_dir=tmp_path,
                total_chapters=1,
                tts_engine=object(),
                text_cleaner=object(),
            )

        assert result.error is None
        assert result.stage_timings["tts_model_load"] > 0
        assert result.stage_timings["clean"] > 0
        assert result.stage_timings["tts"] > 0
        assert "encode" not in result.stage_timings


class TestAsyncBridge:
    """Tests for shared sync/async bridge behavior."""

    def test_bridge_reuses_single_loop_thread(self):
        from modules import async_bridge

        async_bridge._reset_runner_for_tests()

        async def _run() -> tuple[int, int, dict[str, object], dict[str, object]]:
            first_value = async_bridge.run_coroutine_sync(asyncio.sleep(0, result=1))
            first_snapshot = async_bridge._debug_snapshot()
            second_value = async_bridge.run_coroutine_sync(asyncio.sleep(0, result=2))
            second_snapshot = async_bridge._debug_snapshot()
            return first_value, second_value, first_snapshot, second_snapshot

        first, second, snap1, snap2 = asyncio.run(_run())
        assert first == 1
        assert second == 2
        assert snap1["thread_ident"] is not None
        assert snap1["thread_ident"] == snap2["thread_ident"]

    def test_bridge_preserves_exception_propagation(self):
        from modules import async_bridge

        async_bridge._reset_runner_for_tests()

        async def _boom():
            raise RuntimeError("expected boom")

        async def _run() -> None:
            with pytest.raises(RuntimeError, match="expected boom"):
                async_bridge.run_coroutine_sync(_boom())

        asyncio.run(_run())


class TestWeightedEta:
    """Tests for weighted ETA helper behavior."""

    def test_estimate_eta_from_progress_uses_weighted_progress_snapshot(self):
        eta = estimate_eta_from_progress(
            start_time=10.0,
            progress=0.25,
            now=30.0,
            min_progress=0.0,
            min_elapsed_seconds=0.0,
        )

        assert eta == 60.0


class TestEdgeCases:
    """Tests for edge cases and error conditions."""

    def test_empty_chapter_list(self):
        """Test handling of empty chapter list."""
        config = ParallelConfig()
        orchestrator = ParallelPipelineOrchestrator(config)

        results = orchestrator.process_chapters_parallel(
            chapters=[],
            process_func=lambda ch, idx, _fm: None,
        )
        assert results == []

    def test_single_chapter(self):
        """Test handling of single chapter."""
        config = ParallelConfig(
            use_processes=False
        )  # Use threads to avoid pickling issues
        orchestrator = ParallelPipelineOrchestrator(config)

        chapters = [MockChapter(1, "Only Chapter")]

        results = orchestrator.process_chapters_parallel(
            chapters=chapters,
            process_func=lambda ch, idx, _fm: ChapterResult(  # noqa: ARG005
                chapter_number=ch.number,
                chapter_title=ch.title,
                duration_ms=idx + 1,
            ),
        )
        assert len(results) == 1
        assert isinstance(results[0], ChapterResult)
        assert results[0].chapter_number == 1
        assert results[0].duration_ms == 1
