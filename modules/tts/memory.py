"""
VRAM Memory Management Utility
==============================
Utilities for managing GPU/Unified memory on Apple Silicon.
Ensures models are properly loaded and unloaded sequentially.
"""

import gc
import logging
from typing import Any, Optional, Callable

logger = logging.getLogger(__name__)

MLX_AVAILABLE: Optional[bool] = None
mx: Any = None


def _get_mlx_core() -> Any:
    """Import MLX lazily so API/test imports do not initialize Metal."""
    global MLX_AVAILABLE, mx
    if MLX_AVAILABLE is not None:
        return mx

    try:
        import mlx.core as mlx_core
    except ImportError:
        MLX_AVAILABLE = False
        mx = None
    else:
        MLX_AVAILABLE = True
        mx = mlx_core
    return mx


class VRAMManager:
    """
    Manages VRAM/memory for sequential model loading.
    
    Ensures only one model is loaded at a time to prevent
    out-of-memory errors on 16GB unified memory systems.
    """
    
    _instance: Optional['VRAMManager'] = None
    
    def __new__(cls):
        """Singleton pattern to ensure single manager instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._current_model: Optional[str] = None
        self._model_unloaders: dict[str, Callable] = {}
        self._initialized = True
    
    def register_model(
        self, 
        name: str, 
        unload_fn: Callable[[], None]
    ) -> None:
        """
        Register a model with its unload function.
        
        Args:
            name: Unique model identifier
            unload_fn: Function to call to unload the model
        """
        self._model_unloaders[name] = unload_fn
        self._current_model = name
    
    def unload_current(self) -> None:
        """Unload the currently loaded model."""
        if self._current_model and self._current_model in self._model_unloaders:
            self._model_unloaders[self._current_model]()
            del self._model_unloaders[self._current_model]
        self._current_model = None
        clear_vram()
    
    def ensure_can_load(self, model_name: str) -> None:
        """
        Ensure memory is available to load a new model.
        
        Unloads any existing model before loading a new one.
        
        Args:
            model_name: Name of model about to be loaded
        """
        if self._current_model and self._current_model != model_name:
            logger.info(f"Unloading {self._current_model} to load {model_name}")
            self.unload_current()


def clear_vram() -> None:
    """
    Clear GPU/Unified memory cache.
    
    Forces garbage collection and clears MLX cache
    to free unused memory.
    """
    # Force Python garbage collection
    gc.collect()
    
    # Clear MLX cache if available
    mlx_core = _get_mlx_core()
    if MLX_AVAILABLE and mlx_core is not None:
        try:
            mlx_core.clear_cache()
        except Exception:
            pass
    
    # Second GC pass for thorough cleanup
    gc.collect()
