"""Model-free cleanup using the shared rule-only trace path."""

from __future__ import annotations

from typing import Any


class CleanerBasicStrategy:
    name = "basic"
    display_name = "Basic cleanup — no model"
    is_local = True
    requires_model = False
    requires_api_key = False
    supported_quantizations = None
    default_device = None

    def load(self, cleaner: Any) -> None:
        cleaner._loaded = True

    async def clean_batch_with_trace_async(self, cleaner: Any, texts: list[str]) -> list[Any]:
        return [cleaner.clean_with_trace(text) for text in texts]

    def generate_llm_response(self, cleaner: Any, prompt: str) -> str:
        raise RuntimeError("Basic cleanup does not use a language model")

    def unload(self, cleaner: Any, *, keep_in_cache: bool = True) -> None:
        cleaner._loaded = False

    def clear_cache(self, cleaner: Any) -> None:
        pass

    def list_models(self) -> list[str]:
        return []
