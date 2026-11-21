"""Shared model cache primitives for TTS engines."""

from __future__ import annotations

import gc
import sys
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
    evicted: list[Any] = []
    with _model_cache_lock:
        _model_cache[model_path] = model
        _model_cache.move_to_end(model_path)
        while len(_model_cache) > 2:
            evicted.append(_model_cache.popitem(last=False)[1])
    if evicted:
        del evicted
        release_device_memory()


def discard_cached_model_if_same(model_path: str, expected_model: Any) -> bool:
    """Discard only the cache entry still owned by a retiring engine.

    The caller still references ``expected_model``; it must drop its own
    references and then call :func:`release_device_memory` to free it.
    """
    with _model_cache_lock:
        if _model_cache.get(model_path) is not expected_model:
            return False
        _model_cache.pop(model_path, None)
        return True


def release_device_memory() -> None:
    """Collect unreachable models and return their accelerator buffers.

    Only memory no live object references is released, so a model another
    engine still holds is unaffected. Device runtimes are looked up rather than
    imported, and CUDA is only touched once this process has initialized it.
    """
    gc.collect()
    mx_module = sys.modules.get("mlx.core")
    clear_cache = getattr(mx_module, "clear_cache", None)
    if callable(clear_cache):
        try:
            clear_cache()
        except Exception:
            pass
    cuda = getattr(sys.modules.get("torch"), "cuda", None)
    is_initialized = getattr(cuda, "is_initialized", None)
    empty_cache = getattr(cuda, "empty_cache", None)
    if callable(is_initialized) and callable(empty_cache):
        try:
            if is_initialized():
                empty_cache()
        except Exception:
            pass


def clear_model_cache() -> None:
    """Clear the cache and release the memory of models nothing else holds."""
    with _model_cache_lock:
        _model_cache.clear()
    release_device_memory()
