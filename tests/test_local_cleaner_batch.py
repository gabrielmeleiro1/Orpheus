import pytest

import modules.tts.cleaner as cleaner_module
from modules.tts.cleaner import TextCleaner
from modules.tts.cleaner_config import CleanerConfig


def test_local_cleaner_batch_scores_all_chunks_and_preserves_order(monkeypatch):
    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "ensure_mlx_lm_runtime", lambda: None)
    cleaner = TextCleaner(CleanerConfig(backend="mlx", skip_llm_when_not_needed=True))
    calls: list[str] = []

    monkeypatch.setattr(cleaner, "load", lambda: setattr(cleaner, "_loaded", True))
    monkeypatch.setattr(
        cleaner,
        "_generate_llm_response",
        lambda prompt: calls.append(prompt) or "I think this should be fixed.",
    )

    traces = cleaner.clean_chunks_with_trace(
        [
            "Clean prose that should stay on the deterministic path.",
            "Ithink this should be fixed.",
        ]
    )

    assert [trace.cleaned_text for trace in traces] == [
        "Clean prose that should stay on the deterministic path.",
        "I think this should be fixed.",
    ]
    assert [trace.mode for trace in traces] == ["rule_only", "llm_success"]
    assert len(calls) == 1
    assert traces[0].artifact_score is not None
    assert traces[1].artifact_score is not None


def test_text_cleaner_rejects_unknown_backend_at_init():
    with pytest.raises(ValueError, match="not supported"):
        TextCleaner(CleanerConfig(backend="bogus"))
