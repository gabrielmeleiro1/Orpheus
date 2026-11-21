"""Golden-set tests for cleaner quality using real trace-derived samples."""

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
from tests.fixtures.fake_cleaner_backend import attach_cleaner_backend_strategy, fake_mlx_stream


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "cleaner_golden_samples.json"


def _load_golden_samples() -> list[dict[str, object]]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _make_rule_only_cleaner() -> TextCleaner:
    cleaner = TextCleaner.__new__(TextCleaner)
    cleaner.config = CleanerConfig()
    cleaner._loaded = False
    cleaner._model = None
    cleaner._tokenizer = None
    cleaner._model_lock = threading.RLock()
    cleaner._total_chunks_cleaned = 0
    cleaner._total_time_ms = 0
    cleaner._llm_success_count = 0
    cleaner._llm_fallback_count = 0
    cleaner._rule_only_count = 0
    return attach_cleaner_backend_strategy(cleaner)


def _make_loaded_mlx_cleaner() -> TextCleaner:
    cleaner = _make_rule_only_cleaner()
    cleaner.config = CleanerConfig(backend="mlx")
    cleaner._loaded = True
    cleaner._model = object()
    cleaner._tokenizer = object()
    cleaner._needs_llm_cleaning = lambda _text: True
    return cleaner


GOLDEN_SAMPLES = _load_golden_samples()


@pytest.mark.parametrize("sample", GOLDEN_SAMPLES, ids=[s["description"] for s in GOLDEN_SAMPLES])
def test_rule_based_removes_header_artifacts(sample: dict[str, object]) -> None:
    cleaner = _make_rule_only_cleaner()
    result = cleaner._rule_based_clean(str(sample["dirty"]))
    assert result == sample["rule_based"]


@pytest.mark.parametrize("sample", GOLDEN_SAMPLES, ids=[s["description"] for s in GOLDEN_SAMPLES])
def test_llm_fixes_trace_artifacts(sample: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    cleaner = _make_loaded_mlx_cleaner()

    def _fake_generate(_model, _tokenizer, **_kwargs):
        return str(sample["expected_clean"])

    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "make_sampler", lambda temp=0.0: ("sampler", temp))
    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "stream_generate", fake_mlx_stream(_fake_generate))

    cleaned = cleaner.clean(str(sample["dirty"]))
    assert cleaned == sample["expected_clean"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-o", "addopts="]))
