"""Cleaner backend protocol for TextCleaner."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from modules.tts.cleaner_config import CleanTrace


@runtime_checkable
class CleanerBackend(Protocol):
    """Protocol for text cleaner backend implementations."""

    name: str
    is_local: bool
    requires_model: bool
    display_name: str
    requires_api_key: bool

    def load(self, cleaner: Any) -> None:
        """Load backend resources required for cleaning."""

    async def clean_batch_with_trace_async(
        self,
        cleaner: Any,
        texts: list[str],
    ) -> list[CleanTrace]:
        """Clean multiple texts concurrently while preserving trace metadata."""

    def generate_llm_response(self, cleaner: Any, prompt: str) -> str:
        """Generate an LLM response for the given prompt."""

    def unload(self, cleaner: Any, *, keep_in_cache: bool = True) -> None:
        """Unload backend resources."""

    def clear_cache(self, cleaner: Any) -> None:
        """Clear any backend-local caches."""

    def list_models(self) -> list[str]:
        """Return model identifiers offered by this backend."""
