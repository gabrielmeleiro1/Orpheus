"""Test doubles for the cleaner backend registry."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from modules.tts.cleaner_backends.registry import get_cleaner_backend, validate_cleaner_backend
from modules.tts.cleaner_config import CleanTrace

if TYPE_CHECKING:
    from modules.tts.cleaner import CleanerConfig, TextCleaner


class FakeCleanerBackend:
    """Minimal cleaner backend for registry integration tests."""

    name = "fake"
    is_local = True
    requires_model = False
    display_name = "Fake Backend"
    requires_api_key = False

    def load(self, cleaner) -> None:
        cleaner._loaded = True

    async def clean_batch_with_trace_async(self, cleaner, texts):
        return [
            CleanTrace(
                original_text=text,
                rule_based_text=text,
                cleaned_text=f"fake:{text}",
                mode="rule_only",
            )
            for text in texts
        ]

    def generate_llm_response(self, cleaner, prompt: str) -> str:
        return f"fake-response:{prompt}"

    def unload(self, cleaner, *, keep_in_cache: bool = True) -> None:
        _ = keep_in_cache
        cleaner._loaded = False

    def clear_cache(self, cleaner) -> None:
        _ = cleaner

    def list_models(self) -> list[str]:
        return ["fake-model"]


def make_partial_text_cleaner(
    config: CleanerConfig | None = None,
    *,
    loaded: bool = False,
) -> TextCleaner:
    """Build a TextCleaner without running full __init__, with backend strategy set."""
    from modules.tts.cleaner import CleanerConfig, TextCleaner

    cleaner = TextCleaner.__new__(TextCleaner)
    cleaner.config = config or CleanerConfig()
    cleaner._backend_strategy = get_cleaner_backend(cleaner.config.backend)
    cleaner._model = object() if loaded else None
    cleaner._tokenizer = object() if loaded else None
    cleaner._loaded = loaded
    cleaner._manager = None
    cleaner._model_lock = threading.RLock()
    cleaner._api_ssl_context_cache = None
    cleaner._total_chunks_cleaned = 0
    cleaner._total_time_ms = 0
    cleaner._llm_success_count = 0
    cleaner._llm_fallback_count = 0
    cleaner._rule_only_count = 0
    return cleaner


def attach_cleaner_backend_strategy(cleaner: TextCleaner) -> TextCleaner:
    """Ensure a partially constructed TextCleaner has a registry-backed strategy."""
    cleaner._backend_strategy = get_cleaner_backend(
        validate_cleaner_backend(cleaner.config.backend)
    )
    return cleaner
