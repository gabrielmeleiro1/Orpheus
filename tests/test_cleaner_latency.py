"""Latency tests and baseline checks for cleaner operations."""

from __future__ import annotations

import json
import statistics
import sys
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import modules.tts.cleaner as cleaner_module
from modules.tts.cleaner import TextCleaner
from modules.tts.cleaner_config import CleanerConfig
from tests.fixtures.fake_cleaner_backend import attach_cleaner_backend_strategy


BASELINE_PATH = Path(__file__).parent / ".cleaner_latency_baseline.json"
REGRESSION_THRESHOLD = 0.20
MIN_EFFECTIVE_BASELINE_MS = 5.0
ABSOLUTE_TOLERANCE_MS = 10.0


@dataclass
class LatencyResult:
    name: str
    elapsed_ms: float
    timestamp: str


class CleanerLatencyBaseline:
    @staticmethod
    def load() -> dict[str, dict[str, float | str]]:
        if BASELINE_PATH.exists():
            return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        return {}

    @staticmethod
    def save(data: dict[str, dict[str, float | str]]) -> None:
        BASELINE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @staticmethod
    def update(result: LatencyResult) -> None:
        baseline = CleanerLatencyBaseline.load()
        baseline[result.name] = asdict(result)
        CleanerLatencyBaseline.save(baseline)


def _make_cleaner(backend: str = "mlx") -> TextCleaner:
    cleaner = TextCleaner.__new__(TextCleaner)
    cleaner.config = CleanerConfig(
        backend=backend,
        api_key_env="TEST_OPENAI_API_KEY",
        api_model="gpt-5-nano",
        api_base_url="https://api.openai.com/v1",
    )
    cleaner._loaded = True
    cleaner._model = object()
    cleaner._tokenizer = object()
    cleaner._manager = None
    cleaner._model_lock = threading.RLock()
    cleaner._total_chunks_cleaned = 0
    cleaner._total_time_ms = 0
    cleaner._llm_success_count = 0
    cleaner._llm_fallback_count = 0
    cleaner._rule_only_count = 0
    return attach_cleaner_backend_strategy(cleaner)


def _measure_ms(fn, iterations: int = 20) -> float:
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


def test_rule_based_clean_under_50ms() -> None:
    cleaner = _make_cleaner("mlx")
    input_text = "The trading floor was loud and intense. " * 60

    elapsed_ms = _measure_ms(lambda: cleaner._rule_based_clean(input_text), iterations=30)
    assert elapsed_ms < 50.0


def test_full_clean_no_llm_under_50ms() -> None:
    cleaner = _make_cleaner("mlx")
    cleaner._needs_llm_cleaning = lambda _text: False
    input_text = "Simple clean prose for rule-only fast path."

    elapsed_ms = _measure_ms(lambda: cleaner.clean(input_text), iterations=30)
    assert elapsed_ms < 50.0


def test_api_clean_overhead_under_200ms(monkeypatch: pytest.MonkeyPatch) -> None:
    cleaner = _make_cleaner("api")
    input_text = "The ratio changed in 2018 after 42 large trades across the desk."

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

        def read(self) -> bytes:
            payload = {"choices": [{"message": {"content": input_text}}]}
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cleaner_module.cleaner_api_backend.urlrequest, "urlopen", lambda *_a, **_k: _FakeResponse())

    elapsed_ms = _measure_ms(lambda: cleaner.clean(input_text), iterations=10)
    assert elapsed_ms < 200.0


def test_latency_vs_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    mlx_cleaner = _make_cleaner("mlx")
    mlx_cleaner._needs_llm_cleaning = lambda _text: False
    short_clean_text = "Simple clean prose for rule-only fast path."
    long_rule_text = "The trading floor was loud and intense. " * 60

    api_cleaner = _make_cleaner("api")
    api_text = "The ratio changed in 2018 after 42 large trades across the desk."

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

        def read(self) -> bytes:
            payload = {"choices": [{"message": {"content": api_text}}]}
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cleaner_module.cleaner_api_backend.urlrequest, "urlopen", lambda *_a, **_k: _FakeResponse())

    current_metrics = {
        "cleaner_rule_based_ms": _measure_ms(lambda: mlx_cleaner._rule_based_clean(long_rule_text), iterations=30),
        "cleaner_full_rule_only_ms": _measure_ms(lambda: mlx_cleaner.clean(short_clean_text), iterations=30),
        "cleaner_api_mocked_ms": _measure_ms(lambda: api_cleaner.clean(api_text), iterations=10),
    }

    baseline = CleanerLatencyBaseline.load()
    if any(key not in baseline for key in current_metrics):
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        for key, elapsed in current_metrics.items():
            CleanerLatencyBaseline.update(
                LatencyResult(name=key, elapsed_ms=elapsed, timestamp=timestamp)
            )
        pytest.skip("Cleaner latency baseline created; run again to compare")

    regressions: dict[str, float] = {}
    over_budget: dict[str, tuple[float, float]] = {}
    for key, current in current_metrics.items():
        baseline_ms = float(baseline[key]["elapsed_ms"])
        effective_baseline = max(baseline_ms, MIN_EFFECTIVE_BASELINE_MS)
        regressions[key] = (current - effective_baseline) / effective_baseline
        absolute_delta = current - baseline_ms
        if regressions[key] >= REGRESSION_THRESHOLD and absolute_delta >= ABSOLUTE_TOLERANCE_MS:
            over_budget[key] = (regressions[key], absolute_delta)

    assert not over_budget, (
        "Cleaner latency regression exceeded thresholds: "
        + ", ".join(
            f"{name}={reg:.2%} (+{delta:.2f}ms)"
            for name, (reg, delta) in sorted(over_budget.items())
        )
    )

    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    for key, current in current_metrics.items():
        baseline_ms = float(baseline[key]["elapsed_ms"])
        if current < baseline_ms:
            CleanerLatencyBaseline.update(
                LatencyResult(name=key, elapsed_ms=current, timestamp=timestamp)
            )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-o", "addopts="]))
