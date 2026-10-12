"""Latency tests and baseline checks for cleaner operations."""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import modules.tts.cleaner as cleaner_module


from tests._factories_group2_cleaner import make_loaded_cleaner as _make_cleaner


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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-o", "addopts="]))
