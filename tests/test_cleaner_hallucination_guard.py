"""Tests for cleaner hallucination-length guard behavior."""

import json
import sys
from pathlib import Path

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import modules.tts.cleaner as cleaner_module
from modules.tts.cleaner import TextCleaner


from tests._factories_group2_cleaner import make_loaded_cleaner


def _make_cleaner(backend: str = "mlx") -> TextCleaner:
    cleaner = make_loaded_cleaner(backend)
    cleaner._needs_llm_cleaning = lambda _text: True
    return cleaner


def test_output_within_bounds_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    cleaner = _make_cleaner("mlx")
    input_text = "The crash of 1987 forced 42 traders to react quickly in London."

    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "make_sampler", lambda temp=0.0: ("sampler", temp))
    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "generate", lambda _m, _t, **_k: input_text)

    trace = cleaner.clean_with_trace(input_text)
    assert trace.mode == "llm_success"
    assert trace.cleaned_text == input_text


def test_standard_abbreviation_expansion_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaner = _make_cleaner("mlx")
    input_text = "Dr. Smith met Mr. Jones in 1987."
    expanded = "Doctor Smith met Mister Jones in 1987."

    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "make_sampler", lambda temp=0.0: ("sampler", temp))
    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "generate", lambda _m, _t, **_k: expanded)

    trace = cleaner.clean_with_trace(input_text)
    assert trace.mode == "llm_success"
    assert trace.cleaned_text == expanded
    assert trace.llm_error is None
    assert trace.llm_rejected_edits == []


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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-o", "addopts="]))
