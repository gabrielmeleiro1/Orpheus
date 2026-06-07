"""MLX-LM runtime and model-cache helpers for TextCleaner."""

from __future__ import annotations

import gc
import logging
import threading
import asyncio
from typing import Any, Optional

MLX_LM_AVAILABLE = False
MLX_LM_IMPORT_ERROR: Optional[Exception] = None
_MLX_LM_RUNTIME_CHECKED = False
mx = None
load = None
generate = None
make_sampler = None
logger = logging.getLogger(__name__)


def mlx_lm_runtime_error_message(import_error: Optional[Exception]) -> str:
    return (
        "mlx-lm runtime unavailable. "
        f"Import error: {import_error}. "
        "Install a compatible set, for example: "
        "pip install 'mlx-audio==0.1.0' 'mlx-lm==0.24.1' 'mlx-vlm==0.3.0'."
    )


def ensure_mlx_lm_runtime() -> tuple[Any, Any, Any, Any]:
    """Import MLX-LM on demand and return runtime callables."""
    global MLX_LM_AVAILABLE, MLX_LM_IMPORT_ERROR, _MLX_LM_RUNTIME_CHECKED
    global mx, load, generate, make_sampler

    if _MLX_LM_RUNTIME_CHECKED:
        if not MLX_LM_AVAILABLE:
            raise ImportError(mlx_lm_runtime_error_message(MLX_LM_IMPORT_ERROR))
        return mx, load, generate, make_sampler

    try:
        import mlx.core as mx_module
        from mlx_lm import generate as mlx_lm_generate
        from mlx_lm import load as mlx_lm_load
        from mlx_lm.sample_utils import make_sampler as mlx_make_sampler

        mx = mx_module
        load = mlx_lm_load
        generate = mlx_lm_generate
        make_sampler = mlx_make_sampler
        MLX_LM_AVAILABLE = True
        MLX_LM_IMPORT_ERROR = None
    except ImportError as exc:
        MLX_LM_AVAILABLE = False
        MLX_LM_IMPORT_ERROR = exc
    finally:
        _MLX_LM_RUNTIME_CHECKED = True

    if not MLX_LM_AVAILABLE:
        raise ImportError(mlx_lm_runtime_error_message(MLX_LM_IMPORT_ERROR))

    return mx, load, generate, make_sampler


_cleaner_model_cache: dict[str, tuple[Any, Any]] = {}
_cleaner_cache_lock = threading.RLock()


def get_cached_cleaner(model_name: str) -> Optional[tuple[Any, Any]]:
    """Get cleaner model from cache if available."""
    with _cleaner_cache_lock:
        return _cleaner_model_cache.get(model_name)


def set_cached_cleaner(model_name: str, model: Any, tokenizer: Any) -> None:
    """Cache cleaner model (single-entry cache)."""
    with _cleaner_cache_lock:
        _cleaner_model_cache.clear()
        _cleaner_model_cache[model_name] = (model, tokenizer)


def clear_cleaner_cache() -> None:
    """Clear cleaner cache and best-effort free MLX memory."""
    with _cleaner_cache_lock:
        _cleaner_model_cache.clear()
        gc.collect()
        if MLX_LM_AVAILABLE and mx is not None:
            try:
                mx.clear_cache()
            except Exception:
                pass
        gc.collect()


class CleanerMLXStrategy:
    """MLX cleaner backend strategy for TextCleaner facade."""

    backend = "mlx"

    def load(self, cleaner: Any) -> None:
        if cleaner._loaded:
            return

        if cleaner.config.cache_model:
            cached = get_cached_cleaner(cleaner.config.model_name)
            if cached is not None:
                cleaner._model, cleaner._tokenizer = cached
                cleaner._loaded = True
                logger.info("Text cleaner loaded from cache: %s", cleaner.config.model_name)
                return

        cleaner._manager.ensure_can_load("text-cleaner")
        logger.info("Loading text cleaner: %s", cleaner.config.model_name)
        cleaner._model, cleaner._tokenizer = load(cleaner.config.model_name)
        cleaner._loaded = True

        if cleaner.config.cache_model:
            set_cached_cleaner(cleaner.config.model_name, cleaner._model, cleaner._tokenizer)

        cleaner._manager.register_model("text-cleaner", cleaner.unload)
        logger.info("Text cleaner loaded")

    async def clean_batch_with_trace_async(self, cleaner: Any, texts: list[str]) -> list[Any]:
        if not texts:
            return []
        results = []
        for text in texts:
            results.append(await asyncio.to_thread(cleaner.clean_with_trace, text))
        return results

    def generate_llm_response(self, cleaner: Any, prompt: str) -> str:
        return cleaner._generate_mlx_response(prompt)

    def unload(self, cleaner: Any, *, keep_in_cache: bool = True) -> None:
        if not cleaner._loaded:
            return
        cleaner._model = None
        cleaner._tokenizer = None
        cleaner._loaded = False

        if not keep_in_cache:
            clear_cleaner_cache()

        logger.info("Text cleaner unloaded (cached: %s)", keep_in_cache)

    def clear_cache(self, cleaner: Any) -> None:
        _ = cleaner
        clear_cleaner_cache()
        logger.info("Cleaner model cache cleared")
