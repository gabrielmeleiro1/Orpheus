"""Fake torch helpers for CUDA cleaner unit tests."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from modules.tts.cleaner import TextCleaner
from modules.tts.cleaner_config import CleanerConfig
from modules.tts.cleaner_backends.registry import get_cleaner_backend, validate_cleaner_backend
from modules.tts.cleaner_config import CleanTrace


def reset_cuda_runtime_state(cuda_module: Any) -> None:
    """Reset lazy-import globals in cleaner_cuda for isolated tests."""
    cuda_module.TORCH_CUDA_AVAILABLE = False
    cuda_module.TORCH_CUDA_IMPORT_ERROR = None
    cuda_module._TORCH_CUDA_RUNTIME_CHECKED = False
    cuda_module.torch = None
    cuda_module._cleaner_model_cache.clear()


def make_fake_torch_module(*, cuda_available: bool = True) -> MagicMock:
    """Build a minimal torch stand-in with optional CUDA availability."""
    fake_torch = MagicMock(name="torch")
    fake_torch.float16 = "float16"
    fake_cuda = MagicMock(name="cuda")
    fake_cuda.is_available = MagicMock(return_value=cuda_available)
    fake_cuda.empty_cache = MagicMock(name="empty_cache")
    fake_cuda.OutOfMemoryError = type("OutOfMemoryError", (RuntimeError,), {})
    fake_torch.cuda = fake_cuda
    return fake_torch


def make_partial_cuda_cleaner(
    config: CleanerConfig | None = None,
    *,
    loaded: bool = False,
) -> TextCleaner:
    """Build a CUDA-backed TextCleaner without running full __init__."""
    resolved = config or CleanerConfig(
        backend="cuda",
        model_name="meta-llama/Llama-3.2-3B-Instruct",
    )
    cleaner = TextCleaner.__new__(TextCleaner)
    cleaner.config = resolved
    cleaner._backend_strategy = get_cleaner_backend(
        validate_cleaner_backend(resolved.backend)
    )
    cleaner._model = object() if loaded else None
    cleaner._tokenizer = object() if loaded else None
    cleaner._loaded = loaded
    cleaner._manager = SimpleNamespace(
        ensure_can_load=lambda *_args, **_kwargs: None,
        register_model=lambda *_args, **_kwargs: None,
    )
    cleaner._model_lock = threading.RLock()
    cleaner._api_ssl_context_cache = None
    cleaner._total_chunks_cleaned = 0
    cleaner._total_time_ms = 0
    cleaner._llm_success_count = 0
    cleaner._llm_fallback_count = 0
    cleaner._rule_only_count = 0
    return cleaner


def make_clean_trace(text: str, *, cleaned_suffix: str = "") -> CleanTrace:
    """Build a predictable CleanTrace for batch-order assertions."""
    cleaned = f"{text}{cleaned_suffix}"
    return CleanTrace(
        original_text=text,
        rule_based_text=text,
        cleaned_text=cleaned,
        mode="llm_success",
    )
