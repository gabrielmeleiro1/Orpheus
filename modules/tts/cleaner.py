"""
Text Cleaner Module
===================
LLM-based text normalization for improved TTS output.
Uses MLX-LM for efficient local inference on Apple Silicon.
Supports model caching between chunks for improved performance.
"""

import asyncio
import logging
import os
import ssl
import threading
from typing import Any, Optional

try:
    import aiohttp
except ImportError:  # pragma: no cover - runtime dependency guard
    aiohttp = None

from .memory import VRAMManager
from .artifact_scorer import score_text_artifacts
from . import cleaner_api as cleaner_api_backend
from . import cleaner_mlx as cleaner_mlx_backend
from . import cleaner_rules as cleaner_rules_backend
from . import cleaner_trace as cleaner_trace_backend
from . import cleaner_transport as cleaner_transport_backend
from .cleaner_config import (
    API_MODEL_CHOICES as API_MODEL_CHOICES,
    CLEANER_MODEL_CHOICES as CLEANER_MODEL_CHOICES,
    CLEANER_PROMPT_TEMPLATE,  # noqa: F401 - compatibility re-export
    DEFAULT_API_MODEL as DEFAULT_API_MODEL,
    CleanerConfig,
    CleanTrace,
    build_cleaner_prompt,  # noqa: F401 - compatibility re-export
    list_api_cleaner_models as list_api_cleaner_models,
    list_cleaner_models as list_cleaner_models,
)
from .cleaner_backends.registry import get_cleaner_backend, validate_cleaner_backend
from modules.async_bridge import run_coroutine_sync

# MLX-LM runtime compatibility globals retained for direct monkeypatch tests.
MLX_LM_AVAILABLE = cleaner_mlx_backend.MLX_LM_AVAILABLE
MLX_LM_IMPORT_ERROR: Optional[Exception] = cleaner_mlx_backend.MLX_LM_IMPORT_ERROR
_MLX_LM_RUNTIME_CHECKED = cleaner_mlx_backend._MLX_LM_RUNTIME_CHECKED
mx = cleaner_mlx_backend.mx
load = cleaner_mlx_backend.load
generate = cleaner_mlx_backend.generate
make_sampler = cleaner_mlx_backend.make_sampler
logger = logging.getLogger(__name__)

# Preserve the urllib module seam used by tests that monkeypatch transport.
urlrequest = cleaner_api_backend.urlrequest


def _ensure_mlx_lm_runtime() -> None:
    """Import MLX-LM on demand."""
    global MLX_LM_AVAILABLE, MLX_LM_IMPORT_ERROR, _MLX_LM_RUNTIME_CHECKED, mx, load, generate, make_sampler
    mx, load, generate, make_sampler = cleaner_mlx_backend.ensure_mlx_lm_runtime()
    MLX_LM_AVAILABLE = cleaner_mlx_backend.MLX_LM_AVAILABLE
    MLX_LM_IMPORT_ERROR = cleaner_mlx_backend.MLX_LM_IMPORT_ERROR
    _MLX_LM_RUNTIME_CHECKED = cleaner_mlx_backend._MLX_LM_RUNTIME_CHECKED


class _CleanerAPIHTTPError(RuntimeError):
    """Internal transport error for HTTP responses with non-2xx status."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"Cleaner API HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _get_cached_cleaner(model_name: str) -> Optional[tuple[Any, Any]]:
    """Get cleaner model from cache if available."""
    return cleaner_mlx_backend.get_cached_cleaner(model_name)


def _set_cached_cleaner(model_name: str, model: Any, tokenizer: Any):
    """Cache cleaner model."""
    cleaner_mlx_backend.set_cached_cleaner(model_name, model, tokenizer)


def _clear_cleaner_cache():
    """Clear the cleaner model cache."""
    cleaner_mlx_backend.clear_cleaner_cache()


class TextCleaner:
    """
    LLM-based text cleaner for TTS preprocessing.
    
    Uses a small language model to normalize text before TTS:
    - Preserves source wording
    - Fixes extraction artifacts
    - Fixes formatting issues
    
    Supports model caching between chunks for improved performance.
    """
    
    def __init__(self, config: Optional[CleanerConfig] = None):
        """
        Initialize the text cleaner.
        
        Args:
            config: Cleaner configuration (uses defaults if None)
        """
        self.config = config or CleanerConfig()
        self.config.backend = validate_cleaner_backend(self.config.backend)
        self._backend_strategy = get_cleaner_backend(self.config.backend)
        
        self._model = None
        self._tokenizer = None
        self._loaded = False
        self._manager = VRAMManager()
        self._model_lock = threading.RLock()
        self._api_ssl_context_cache = None
        
        # Track statistics
        self._total_chunks_cleaned = 0
        self._total_time_ms = 0
        self._llm_success_count = 0
        self._llm_fallback_count = 0
        self._rule_only_count = 0

    @property
    def is_loaded(self) -> bool:
        """Check if model is currently loaded."""
        return self._loaded
    
    @property
    def average_chunk_time_ms(self) -> float:
        """Get average time per chunk in milliseconds."""
        if self._total_chunks_cleaned == 0:
            return 0.0
        return self._total_time_ms / self._total_chunks_cleaned

    def stats_snapshot(self) -> dict[str, int]:
        """Return cleaner usage counters for observability."""
        return {
            "llm_success": self._llm_success_count,
            "llm_fallback": self._llm_fallback_count,
            "rule_only": self._rule_only_count,
            "total_chunks": self._total_chunks_cleaned,
        }
    
    def load(self) -> None:
        """Load the LLM model into memory."""
        self._backend_strategy.load(self)
    
    def clean(self, text: str) -> str:
        """Compatibility wrapper that returns only cleaned text."""
        return self.clean_with_trace(text).cleaned_text

    async def clean_batch_async(self, texts: list[str]) -> list[str]:
        """
        Clean multiple chunks concurrently.

        For API backend this dispatches chunk cleaning in parallel to improve throughput.
        """
        traces = await self.clean_batch_with_trace_async(texts)
        return [trace.cleaned_text for trace in traces]

    def _get_api_batch_concurrency(self) -> int:
        """Return safe async concurrency for API cleaner batch requests."""
        configured = int(getattr(self.config, "api_batch_concurrency", 4) or 1)
        return max(1, configured)

    async def clean_batch_with_trace_async(self, texts: list[str]) -> list[CleanTrace]:
        """
        Clean multiple chunks concurrently while preserving per-chunk trace data.

        For API backend this avoids sync/async bridge overhead by executing fully async.
        """
        return await self._backend_strategy.clean_batch_with_trace_async(self, texts)

    def clean_chunks(self, texts: list[str]) -> list[str]:
        """Clean multiple chunks with local gating while preserving input order."""
        return [trace.cleaned_text for trace in self.clean_chunks_with_trace(texts)]

    def clean_chunks_with_trace(self, texts: list[str]) -> list[CleanTrace]:
        """Synchronous batch-clean interface for local and fallback paths."""
        indexed = list(enumerate(texts))
        indexed.sort(key=lambda item: len(item[1]))
        traces_by_index: dict[int, CleanTrace] = {}
        for index, text in indexed:
            traces_by_index[index] = self.clean_with_trace(text)
        return [traces_by_index[index] for index in range(len(texts))]

    def _artifact_score_for(self, text: str) -> dict[str, object]:
        return score_text_artifacts(text).as_trace_dict()

    def _should_use_rule_only_cleaning(
        self,
        rule_based_text: str,
        artifact_score: dict[str, object] | None = None,
    ) -> bool:
        """Return whether rule-based output is enough for this chunk."""
        score_total = (
            float(artifact_score.get("total", 0.0))
            if isinstance(artifact_score, dict)
            else score_text_artifacts(rule_based_text).total
        )
        if score_total >= float(getattr(self.config, "artifact_gate_light_threshold", 0.20)):
            return False
        if self._needs_llm_cleaning(rule_based_text):
            return False
        if len(rule_based_text) < 50:
            return True
        return bool(getattr(self.config, "skip_llm_when_not_needed", False))

    async def _clean_with_trace_api_async(
        self,
        text: str,
        semaphore: asyncio.Semaphore,
        *,
        api_session: Any = None,
    ) -> CleanTrace:
        """Async API backend variant of clean_with_trace for batch execution."""
        async def generate_response(prompt: str) -> str:
            async with semaphore:
                if api_session is None:
                    return await self._generate_api_response_async(prompt)
                return await self._generate_api_response_async(
                    prompt,
                    api_session=api_session,
                )

        return await cleaner_trace_backend.run_trace_decision_flow_async(
            self,
            CleanTrace,
            text,
            generate_response,
            warning_logger=logger,
        )

    def clean_with_trace(self, text: str) -> CleanTrace:
        """
        Clean and normalize text for TTS.
        
        Args:
            text: Raw text to clean
            
        Returns:
            Structured cleaning trace with before/after text
        """
        return cleaner_trace_backend.run_trace_decision_flow(
            self,
            CleanTrace,
            text,
            self._generate_llm_response,
            warning_logger=logger,
        )

    @staticmethod
    def _repair_known_leading_letter_fragments(text: str) -> str:
        """Repair recurring dropped-leading-letter artifacts that LLMs can miss."""
        return cleaner_rules_backend.repair_known_leading_letter_fragments(text)

    def _generate_llm_response(self, prompt: str) -> str:
        """
        Generate cleaned text from the LLM.

        Supports both legacy mlx-lm (`temp`) and newer (`temperature`) APIs.
        """
        return self._backend_strategy.generate_llm_response(self, prompt)

    def _generate_mlx_response(self, prompt: str) -> str:
        """Generate cleaned text via local mlx-lm runtime."""
        with self._model_lock:
            if make_sampler is not None:
                return generate(
                    self._model,
                    self._tokenizer,
                    prompt=prompt,
                    max_tokens=self.config.max_tokens,
                    sampler=make_sampler(temp=self.config.temperature),
                    verbose=False
                )

            try:
                return generate(
                    self._model,
                    self._tokenizer,
                    prompt=prompt,
                    max_tokens=self.config.max_tokens,
                    temp=self.config.temperature,
                    verbose=False
                )
            except TypeError as exc:
                if "unexpected keyword argument 'temp'" not in str(exc):
                    raise
                return generate(
                    self._model,
                    self._tokenizer,
                    prompt=prompt,
                    max_tokens=self.config.max_tokens,
                    temperature=self.config.temperature,
                    verbose=False
                )

    def _generate_api_response(self, prompt: str) -> str:
        """
        Generate cleaned text via an OpenAI-compatible Chat Completions API.

        Works with OpenAI by default:
          - base URL: https://api.openai.com/v1
          - key env: OPENAI_API_KEY
        """
        return cleaner_transport_backend.generate_api_response(
            self,
            prompt,
            http_error_cls=_CleanerAPIHTTPError,
            aiohttp_module=aiohttp,
        )

    async def _generate_api_response_async(
        self,
        prompt: str,
        *,
        api_session: Any = None,
    ) -> str:
        """Async variant of API cleaner response generation."""
        return await cleaner_transport_backend.generate_api_response_async(
            self,
            prompt,
            http_error_cls=_CleanerAPIHTTPError,
            aiohttp_module=aiohttp,
            api_session=api_session,
        )

    async def _post_api_chat_completions_with_retry_async(
        self,
        *,
        base_url: str,
        api_key: str,
        payload: dict[str, object],
        ssl_context: ssl.SSLContext,
        aiohttp_module: Any = None,
        api_session: Any = None,
    ) -> str:
        """POST chat completions payload and retry token field once on HTTP 400."""
        aiohttp_runtime = aiohttp if aiohttp_module is None else aiohttp_module
        return await cleaner_api_backend.post_api_chat_completions_with_retry_async(
            config=self.config,
            base_url=base_url,
            api_key=api_key,
            payload=payload,
            ssl_context=ssl_context,
            http_error_cls=_CleanerAPIHTTPError,
            max_tokens=self.config.max_tokens,
            aiohttp_module=aiohttp_runtime,
            api_session=api_session,
        )

    @staticmethod
    def _get_api_aiohttp_module() -> Any:
        """Return the aiohttp module currently wired into this facade."""
        return aiohttp

    @staticmethod
    def _run_coroutine_sync(coro):
        """Run coroutine from synchronous context, even if a loop is already running."""
        return run_coroutine_sync(coro)

    def _parse_api_response_text(self, response_body: str) -> str:
        """Parse and validate chat-completions style response body."""
        return cleaner_api_backend.parse_api_response_text(response_body)

    def _build_api_ssl_context(self) -> ssl.SSLContext:
        """Build SSL context for cleaner API calls with optional custom CA support."""
        cache_key = (
            bool(getattr(self.config, "api_allow_insecure_ssl", False)),
            str(getattr(self.config, "api_ca_bundle", "") or ""),
            os.getenv("CLEANER_API_CA_BUNDLE", ""),
            os.getenv("CLEANER_API_INSECURE_SSL", ""),
        )
        cached = getattr(self, "_api_ssl_context_cache", None)
        if cached is not None and cached[0] == cache_key:
            return cached[1]

        context = cleaner_api_backend.build_api_ssl_context(self.config)
        self._api_ssl_context_cache = (cache_key, context)
        return context
    
    def _rule_based_clean(self, text: str) -> str:
        """Apply rule-based text cleaning (fast, always runs)."""
        return cleaner_rules_backend.rule_based_clean(text)

    def _needs_llm_cleaning(self, text: str) -> bool:
        """Check if text needs LLM-based cleaning."""
        return cleaner_rules_backend.needs_llm_cleaning(text)
    
    def _extract_result(self, response: str) -> str:
        """Extract cleaned text from LLM response."""
        return cleaner_rules_backend.extract_result(response)
    
    def unload(self, keep_in_cache: bool = True) -> None:
        """
        Unload the model and free memory.
        
        Args:
            keep_in_cache: If True, model stays in global cache for reuse
        """
        self._backend_strategy.unload(self, keep_in_cache=keep_in_cache)
    
    def clear_cache(self) -> None:
        """Clear the global cleaner model cache."""
        self._backend_strategy.clear_cache(self)
    
    def __enter__(self):
        """Context manager entry."""
        self.load()
        return self
    
    def __exit__(self, _exc_type, _exc_val, _exc_tb):
        """Context manager exit - ensures cleanup."""
        self.unload(keep_in_cache=self.config.cache_model)
        return False


def test_rule_based_cleaning() -> bool:
    """Test rule-based cleaning without loading LLM."""
    print("\nTesting rule-based text cleaning...")
    
    cleaner = TextCleaner.__new__(TextCleaner)
    cleaner.config = CleanerConfig()
    cleaner._loaded = False
    
    test_cases = [
        ("Dr. Smith went to St. Louis.", "Dr. Smith went to St. Louis."),
        ("e.g. this is an example", "e.g. this is an example"),
        ("Too   many    spaces", "Too many spaces"),
        ("Hello,world", "Hello, world"),
    ]
    
    passed = 0
    for input_text, expected in test_cases:
        result = cleaner._rule_based_clean(input_text)
        if result == expected:
            print(f"✓ '{input_text}' → '{result}'")
            passed += 1
        else:
            print(f"✗ '{input_text}' → '{result}' (expected: '{expected}')")
    
    print(f"\nPassed {passed}/{len(test_cases)} tests")
    return passed == len(test_cases)


if __name__ == "__main__":
    test_rule_based_cleaning()
