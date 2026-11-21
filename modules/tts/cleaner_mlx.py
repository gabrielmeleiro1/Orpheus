"""MLX-LM runtime and model-cache helpers for TextCleaner."""

from __future__ import annotations

import gc
import logging
import threading
from typing import Any, Callable, Optional

from .cleaner_trace import run_local_batch_async
from .model_snapshots import installed_model_path

MLX_LM_AVAILABLE = False
MLX_LM_IMPORT_ERROR: Optional[Exception] = None
_MLX_LM_RUNTIME_CHECKED = False
_MLX_LM_RUNTIME_LOCK = threading.Lock()
mx = None
load = None
stream_generate = None
make_sampler = None
logger = logging.getLogger(__name__)


def mlx_lm_runtime_error_message(import_error: Optional[Exception]) -> str:
    return (
        "mlx-lm runtime unavailable. "
        f"Import error: {import_error}. "
        "Install the supported MLX profile with: "
        "python -m pip install -r requirements/mlx.txt."
    )


def ensure_mlx_lm_runtime() -> tuple[Any, Any, Any, Any]:
    """Import MLX-LM on demand and return runtime callables."""
    if not _MLX_LM_RUNTIME_CHECKED:
        with _MLX_LM_RUNTIME_LOCK:
            if not _MLX_LM_RUNTIME_CHECKED:
                _import_mlx_lm_runtime()
    if not MLX_LM_AVAILABLE:
        raise ImportError(mlx_lm_runtime_error_message(MLX_LM_IMPORT_ERROR))
    return mx, load, stream_generate, make_sampler


def _import_mlx_lm_runtime() -> None:
    global MLX_LM_AVAILABLE, MLX_LM_IMPORT_ERROR, _MLX_LM_RUNTIME_CHECKED
    global mx, load, stream_generate, make_sampler

    try:
        import mlx.core as mx_module
        from mlx_lm import load as mlx_lm_load
        from mlx_lm import stream_generate as mlx_lm_stream_generate
        from mlx_lm.sample_utils import make_sampler as mlx_make_sampler

        mx = mx_module
        load = mlx_lm_load
        stream_generate = mlx_lm_stream_generate
        make_sampler = mlx_make_sampler
        MLX_LM_AVAILABLE = True
        MLX_LM_IMPORT_ERROR = None
    except ImportError as exc:
        MLX_LM_AVAILABLE = False
        MLX_LM_IMPORT_ERROR = exc
    finally:
        _MLX_LM_RUNTIME_CHECKED = True


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


_cleaner_model_locks: dict[str, threading.RLock] = {}


def get_cleaner_model_lock(model_name: str) -> threading.RLock:
    """Return the use-lock shared by every cleaner that can hold this model."""
    with _cleaner_cache_lock:
        lock = _cleaner_model_locks.get(model_name)
        if lock is None:
            lock = _cleaner_model_locks[model_name] = threading.RLock()
        return lock


def _free_mlx_memory_locked() -> None:
    gc.collect()
    if MLX_LM_AVAILABLE and mx is not None:
        try:
            mx.clear_cache()
        except Exception:
            pass
    gc.collect()


def clear_cleaner_cache() -> None:
    """Clear cleaner cache and best-effort free MLX memory."""
    with _cleaner_cache_lock:
        _cleaner_model_cache.clear()
        _free_mlx_memory_locked()


def _evict_cached_cleaners() -> None:
    """Free a cached model before loading its replacement, not after."""
    with _cleaner_cache_lock:
        if _cleaner_model_cache:
            clear_cleaner_cache()


def discard_cached_cleaner(model_name: str, model: Any) -> None:
    """Drop the cache entry only when it holds ``model``; keep another cleaner's."""
    with _cleaner_cache_lock:
        cached = _cleaner_model_cache.get(model_name)
        if cached is not None and cached[0] is model:
            del _cleaner_model_cache[model_name]


def release_cleaner_memory() -> None:
    """Best-effort return of freed model memory to the system."""
    with _cleaner_cache_lock:
        _free_mlx_memory_locked()


class CleanerMLXStrategy:
    """MLX cleaner backend strategy for TextCleaner facade."""

    name = "mlx"
    is_local = True
    requires_model = True
    requires_api_key = False
    display_name = "Local MLX"
    backend = "mlx"
    supported_quantizations = None
    default_device = None

    @classmethod
    def list_models(cls) -> list[str]:
        from modules.tts.cleaner_config import list_cleaner_models

        return list_cleaner_models()

    def load(self, cleaner: Any) -> None:
        ensure_mlx_lm_runtime()
        if cleaner._loaded:
            return
        # Cleaners sharing a cached model must serialize generation on it.
        cleaner._model_lock = get_cleaner_model_lock(cleaner.config.model_name)

        if cleaner.config.cache_model:
            cached = get_cached_cleaner(cleaner.config.model_name)
            if cached is not None:
                installed_model_path(cleaner.config.model_name)
                cleaner._model, cleaner._tokenizer = cached
                cleaner._loaded = True
                logger.info("Text cleaner loaded from cache: %s", cleaner.config.model_name)
                return
            _evict_cached_cleaners()

        logger.info("Loading text cleaner: %s", cleaner.config.model_name)
        cleaner._model, cleaner._tokenizer = load(
            installed_model_path(cleaner.config.model_name),
            tokenizer_config={"local_files_only": True},
        )
        cleaner._loaded = True

        if cleaner.config.cache_model:
            set_cached_cleaner(cleaner.config.model_name, cleaner._model, cleaner._tokenizer)

        logger.info("Text cleaner loaded")

    async def clean_batch_with_trace_async(
        self,
        cleaner: Any,
        texts: list[str],
        cancel_requested: Callable[[], bool] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[Any]:
        return await run_local_batch_async(cleaner, texts, cancel_requested, progress_callback)

    def generate_llm_response(self, cleaner: Any, prompt: str) -> str:
        return cleaner._generate_mlx_response(prompt)

    def unload(self, cleaner: Any, *, keep_in_cache: bool = True) -> None:
        if not cleaner._loaded:
            return
        model = cleaner._model
        cleaner._model = None
        cleaner._tokenizer = None
        cleaner._loaded = False

        if not keep_in_cache:
            discard_cached_cleaner(cleaner.config.model_name, model)
            del model
            release_cleaner_memory()

        logger.info("Text cleaner unloaded (cached: %s)", keep_in_cache)

    def clear_cache(self, cleaner: Any) -> None:
        _ = cleaner
        clear_cleaner_cache()
        logger.info("Cleaner model cache cleared")
