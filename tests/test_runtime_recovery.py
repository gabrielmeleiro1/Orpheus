"""
Test runtime recovery helpers in pipeline orchestrator.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from modules.pipeline.orchestrator import ConversionPipeline, PipelineConfig
from modules.pipeline.retry import is_infrastructure_runtime_error
from modules.tts.engine import BatchItem, BatchResult


class _ExplodingCleaner:
    def __init__(self) -> None:
        self.clean_calls = 0

    def clean(self, text: str) -> str:
        self.clean_calls += 1
        raise ValueError("bad value(s) in fds_to_keep")

    def _rule_based_clean(self, text: str) -> str:
        return text.upper()


class _OtherErrorCleaner:
    def clean(self, text: str) -> str:
        _ = text
        raise ValueError("model parse failed")


class _SerialRecoveryEngine:
    sample_rate = 24000

    def __init__(self) -> None:
        self.serial_calls = 0

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray:
        _ = text, voice, speed
        self.serial_calls += 1
        return np.zeros(2400, dtype=np.float32)


def _make_pipeline() -> ConversionPipeline:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg = PipelineConfig(
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            enable_parallel=False,
        )
        return ConversionPipeline(config=cfg)


def _test_infrastructure_marker_detection() -> None:
    assert is_infrastructure_runtime_error("bad value(s) in fds_to_keep")
    assert is_infrastructure_runtime_error("BrokenProcessPool worker exited")
    assert is_infrastructure_runtime_error("cannot pickle 'X' object")
    assert not is_infrastructure_runtime_error("invalid chapter title")
    print("✓ infrastructure marker detection")


def _test_clean_chunk_falls_back_to_rule_based_mode() -> None:
    pipeline = _make_pipeline()
    cleaner = _ExplodingCleaner()

    cleaned, llm_enabled, trace1 = pipeline.stages._chapter_processor.clean_chunk_safely(
        cleaner=cleaner,
        chunk="hello world",
        llm_cleaning_enabled=True,
    )
    assert cleaned == "HELLO WORLD"
    assert llm_enabled is False
    assert trace1 is not None
    assert trace1["mode"] == "llm_fallback"
    assert cleaner.clean_calls == 1

    cleaned2, llm_enabled2, trace2 = pipeline.stages._chapter_processor.clean_chunk_safely(
        cleaner=cleaner,
        chunk="again",
        llm_cleaning_enabled=llm_enabled,
    )
    assert cleaned2 == "AGAIN"
    assert llm_enabled2 is False
    assert trace2 is not None
    assert trace2["mode"] == "rule_only"
    assert cleaner.clean_calls == 1, "LLM cleaner should not be called again after fallback"
    print("✓ cleaner fallback to rule-based mode")


def _test_clean_chunk_reraises_non_infrastructure_errors() -> None:
    pipeline = _make_pipeline()
    cleaner = _OtherErrorCleaner()

    try:
        pipeline.stages._chapter_processor.clean_chunk_safely(
            cleaner=cleaner,
            chunk="hello",
            llm_cleaning_enabled=True,
        )
        raise AssertionError("expected clean helper to re-raise non-infrastructure errors")
    except ValueError as exc:
        assert "model parse failed" in str(exc)

    print("✓ non-infrastructure cleaner errors are re-raised")


def _test_batch_failure_retries_serial_synthesis() -> None:
    pipeline = _make_pipeline()
    engine = _SerialRecoveryEngine()
    pipeline._tts_engine = engine  # noqa: SLF001

    items = [
        BatchItem(text="one", voice="am_adam", speed=1.0, index=0),
        BatchItem(text="two", voice="am_adam", speed=1.0, index=1),
    ]
    failed_batch_results = [
        BatchResult(audio=None, index=0, error="[E101] Speech synthesis failed", duration_ms=0),
        BatchResult(audio=None, index=1, error="[E101] Speech synthesis failed", duration_ms=0),
    ]

    progress: list[tuple[int, int]] = []
    recovered = pipeline.stages._chapter_processor.retry_batch_results_serially(
        batch_items=items,
        batch_results=failed_batch_results,
        total_chunks=2,
        on_batch_progress=lambda current, total: progress.append((current, total)),
    )

    assert len(recovered) == 2
    assert all(result.error is None for result in recovered)
    assert engine.serial_calls == 2
    assert progress == [(1, 2), (2, 2)]
    print("✓ all-batch-failure path retries serial synthesis")


def test_runtime_recovery() -> bool:
    print("\n" + "=" * 50)
    print("RUNTIME RECOVERY TEST SUITE")
    print("=" * 50 + "\n")

    _test_infrastructure_marker_detection()
    _test_clean_chunk_falls_back_to_rule_based_mode()
    _test_clean_chunk_reraises_non_infrastructure_errors()
    _test_batch_failure_retries_serial_synthesis()

    print("\n" + "=" * 50)
    print("ALL RUNTIME RECOVERY TESTS PASSED ✓")
    print("=" * 50 + "\n")
    return True


if __name__ == "__main__":
    success = test_runtime_recovery()
    sys.exit(0 if success else 1)
