"""Real API integration test for multi-chapter cleaner before/after traces."""

import json
import os
import sys
import threading
from pathlib import Path

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from modules.tts.cleaner import CleanerConfig, TextCleaner


pytestmark = [pytest.mark.integration, pytest.mark.slow]

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "cleaner_api_trace_multichapter_samples.json"


def _load_samples() -> list[dict[str, object]]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _make_real_api_cleaner() -> TextCleaner:
    cleaner = TextCleaner.__new__(TextCleaner)
    cleaner.config = CleanerConfig(
        backend="api",
        api_base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        api_model="gpt-4o-mini",
    )
    cleaner._loaded = True
    cleaner._model = None
    cleaner._tokenizer = None
    cleaner._manager = None
    cleaner._model_lock = threading.RLock()
    cleaner._total_chunks_cleaned = 0
    cleaner._total_time_ms = 0
    cleaner._llm_success_count = 0
    cleaner._llm_fallback_count = 0
    cleaner._rule_only_count = 0
    cleaner._needs_llm_cleaning = lambda _text: True
    return cleaner


SAMPLES = _load_samples()


@pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY is required for real API integration tests",
)
@pytest.mark.parametrize(
    "sample",
    SAMPLES,
    ids=[f"{s['chapter']}-{s['description']}" for s in SAMPLES],
)
def test_real_api_trace_before_after_for_malformed_chapters(sample: dict[str, object]) -> None:
    cleaner = _make_real_api_cleaner()

    trace = cleaner.clean_with_trace(str(sample["dirty"]))

    print(f"\n[{sample['chapter']}] {sample['description']}")
    print(f"DIRTY: {trace.original_text}")
    print(f"RULE : {trace.rule_based_text}")
    print(f"CLEAN: {trace.cleaned_text}")

    assert trace.original_text == sample["dirty"]
    assert trace.rule_based_text == sample["expected_rule_based"]
    assert trace.mode == "llm_success"
    assert trace.cleaned_text
    assert trace.cleaned_text != trace.rule_based_text

    for token in sample["must_not_contain_after"]:
        assert str(token) not in trace.cleaned_text

    for fragment in sample["required_fragments"]:
        assert str(fragment) in trace.cleaned_text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s", "-o", "addopts="]))

