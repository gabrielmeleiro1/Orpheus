"""Tests for cleaner hallucination-length guard behavior."""

import json
import sys
import threading
from pathlib import Path

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import modules.tts.cleaner as cleaner_module
from modules.tts.cleaner import TextCleaner
from modules.tts.cleaner_config import CleanerConfig
from tests.fixtures.fake_cleaner_backend import attach_cleaner_backend_strategy


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
    cleaner._needs_llm_cleaning = lambda _text: True
    return attach_cleaner_backend_strategy(cleaner)


def test_output_within_bounds_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    cleaner = _make_cleaner("mlx")
    input_text = "The crash of 1987 forced 42 traders to react quickly in London."

    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "make_sampler", lambda temp=0.0: ("sampler", temp))
    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "generate", lambda _m, _t, **_k: input_text)

    trace = cleaner.clean_with_trace(input_text)
    assert trace.mode == "llm_success"
    assert trace.cleaned_text == input_text


def test_output_1_3x_longer_triggers_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    cleaner = _make_cleaner("mlx")
    input_text = "This took place on the # W trading floor of Handelsbanken in 2018."
    rule_based = cleaner._rule_based_clean(input_text)
    hallucinated = "x" * (int(len(rule_based) * 2.0))

    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "make_sampler", lambda temp=0.0: ("sampler", temp))
    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "generate", lambda _m, _t, **_k: hallucinated)

    trace = cleaner.clean_with_trace(input_text)
    assert trace.mode == "hallucination_fallback"
    assert trace.cleaned_text == rule_based


def test_hallucination_fallback_stat_incremented(monkeypatch: pytest.MonkeyPatch) -> None:
    cleaner = _make_cleaner("mlx")
    input_text = "There were 100 participants and 42 follow-up calls."
    rule_based = cleaner._rule_based_clean(input_text)
    hallucinated = "y" * (int(len(rule_based) * 2.0))

    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "make_sampler", lambda temp=0.0: ("sampler", temp))
    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "generate", lambda _m, _t, **_k: hallucinated)

    before = cleaner.stats_snapshot()["llm_fallback"]
    _ = cleaner.clean(input_text)
    after = cleaner.stats_snapshot()["llm_fallback"]

    assert after == before + 1


def test_abbreviation_expansion_triggers_preservation_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaner = _make_cleaner("mlx")
    input_text = "Dr. Smith met Mr. Jones in 1987."
    rule_based = cleaner._rule_based_clean(input_text)
    expanded = "Doctor Smith met Mister Jones in 1987."

    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "make_sampler", lambda temp=0.0: ("sampler", temp))
    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "generate", lambda _m, _t, **_k: expanded)

    trace = cleaner.clean_with_trace(input_text)
    assert trace.mode == "hallucination_fallback"
    assert trace.cleaned_text == rule_based
    assert "preservation guard" in (trace.llm_error or "")


def test_api_backend_hallucination_also_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    cleaner = _make_cleaner("api")
    input_text = "The ratio changed in 2018 after 42 large trades."
    rule_based = cleaner._rule_based_clean(input_text)
    hallucinated = "z" * (int(len(rule_based) * 2.0))

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

        def read(self) -> bytes:
            payload = {"choices": [{"message": {"content": hallucinated}}]}
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cleaner_module.cleaner_api_backend.urlrequest, "urlopen", lambda *_a, **_k: _FakeResponse())

    trace = cleaner.clean_with_trace(input_text)
    assert trace.mode == "hallucination_fallback"
    assert trace.cleaned_text == rule_based


def test_trace_records_hallucination_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    cleaner = _make_cleaner("mlx")
    input_text = "The broker saw 1987 as a turning point with 42 signals."
    rule_based = cleaner._rule_based_clean(input_text)
    hallucinated = "q" * (int(len(rule_based) * 2.0))

    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "make_sampler", lambda temp=0.0: ("sampler", temp))
    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "generate", lambda _m, _t, **_k: hallucinated)

    trace = cleaner.clean_with_trace(input_text)
    assert trace.mode == "hallucination_fallback"
    assert "suspiciously long" in (trace.llm_error or "")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-o", "addopts="]))
