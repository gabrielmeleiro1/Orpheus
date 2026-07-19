"""Shared model cache primitives for TTS engines."""

from __future__ import annotations

import gc
import threading
from collections import OrderedDict
from typing import Any, Optional


_model_cache: OrderedDict[str, Any] = OrderedDict()
_model_cache_lock = threading.RLock()

# One use-lock per cache key. get_cached_model hands the same object to every
# caller, so holders must serialize their calls into it; a per-instance lock
# cannot do that. Kept separate from the cache itself so a model evicted and
# reloaded mid-run still contends on the same lock.
_model_use_locks: dict[str, threading.RLock] = {}


def get_model_lock(model_path: str) -> threading.RLock:
    """Return the shared use-lock guarding calls into a cached model."""
    with _model_cache_lock:
        lock = _model_use_locks.get(model_path)
        if lock is None:
            lock = threading.RLock()
            _model_use_locks[model_path] = lock
        return lock


def get_cached_model(model_path: str) -> Optional[Any]:
    """Get model from cache if available."""
    with _model_cache_lock:
        model = _model_cache.get(model_path)
        if model is not None:
            _model_cache.move_to_end(model_path)
        return model


def set_cached_model(model_path: str, model: Any) -> None:
    """Cache a model with small LRU behavior."""
    with _model_cache_lock:
        _model_cache[model_path] = model
        _model_cache.move_to_end(model_path)
        while len(_model_cache) > 2:
            _model_cache.popitem(last=False)


def pop_cached_model(model_path: str) -> Optional[Any]:
    """Remove and return a cached model, if present."""
    with _model_cache_lock:
        return _model_cache.pop(model_path, None)


def clear_model_cache(mx_module: Any = None, mlx_available: bool = False) -> None:
    """Clear cache and optionally clear MLX cache."""
    with _model_cache_lock:
        _model_cache.clear()
        gc.collect()
        if mlx_available and mx_module is not None:
            try:
                mx_module.clear_cache()
            except Exception:
                pass
        gc.collect()
