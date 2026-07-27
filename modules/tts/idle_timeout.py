"""Automatic model unloading after a period of inactivity."""

from __future__ import annotations

import logging
import threading
import time
import warnings
from typing import Callable, Dict, Optional

from .vram_policy import IdleTimeoutConfig

logger = logging.getLogger(__name__)


class IdleTimeoutManager:
    """
    Manages automatic model unloading after idle timeout.

    Tracks activity and automatically unloads models when idle
    for a specified duration to free VRAM.

    Example:
        idle_manager = IdleTimeoutManager(timeout_seconds=300)
        idle_manager.register_model("tts", tts_engine.unload_model)
        idle_manager.register_model("cleaner", cleaner.unload_model)
        idle_manager.start()

        # Activity is tracked automatically
        idle_manager.record_activity()

        # After 5 minutes of no activity, models are auto-unloaded
    """

    _instance: Optional[IdleTimeoutManager] = None
    _instance_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        """Singleton pattern for global manager."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, timeout_seconds: float = 300.0, config: Optional[IdleTimeoutConfig] = None):
        # Only initialize once
        if hasattr(self, '_initialized') and self._initialized:
            return

        self.config = config or IdleTimeoutConfig()
        self._timeout_seconds = timeout_seconds

        # Registered models and their unload functions
        self._models: Dict[str, Callable[[], None]] = {}
        self._models_lock = threading.Lock()

        # Activity tracking
        self._last_activity = time.time()
        self._activity_lock = threading.Lock()
        self._warning_issued = False

        # Monitoring state
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Callbacks
        self._on_unload_callbacks: list[Callable[[], None]] = []
        self._on_warning_callbacks: list[Callable[[float], None]] = []

        self._initialized = True

    def register_model(self, name: str, unload_fn: Callable[[], None]) -> None:
        """
        Register a model for idle timeout management.

        Args:
            name: Model identifier
            unload_fn: Function to call to unload the model
        """
        with self._models_lock:
            self._models[name] = unload_fn

    def unregister_model(self, name: str) -> None:
        """Unregister a model from idle management."""
        with self._models_lock:
            self._models.pop(name, None)

    def record_activity(self) -> None:
        """Record activity to reset idle timer."""
        with self._activity_lock:
            self._last_activity = time.time()
            self._warning_issued = False

    def start(self) -> None:
        """Start the idle timeout monitoring thread."""
        if self._running:
            return

        self._stop_event.clear()
        self._running = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def stop(self) -> None:
        """Stop the idle timeout monitoring thread."""
        if not self._running:
            return

        self._stop_event.set()
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5.0)

    def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while not self._stop_event.is_set():
            try:
                self._check_idle_timeout()
            except Exception as e:
                warnings.warn(f"Idle timeout error: {e}")

            self._stop_event.wait(self.config.check_interval_seconds)

    def _check_idle_timeout(self) -> None:
        """Check if idle timeout has been reached."""
        with self._activity_lock:
            idle_time = time.time() - self._last_activity

        # Issue warning before unload
        warning_time = self._timeout_seconds - self.config.warning_before_unload_seconds
        if idle_time >= warning_time and not self._warning_issued:
            self._warning_issued = True
            time_remaining = self._timeout_seconds - idle_time
            for callback in self._on_warning_callbacks:
                try:
                    callback(time_remaining)
                except Exception:
                    pass

        # Unload if timeout reached
        if idle_time >= self._timeout_seconds:
            self._unload_all_models()
            # Reset activity to prevent repeated unloading
            with self._activity_lock:
                self._last_activity = time.time()

    def _unload_all_models(self) -> None:
        """Unload all registered models."""
        with self._models_lock:
            models = list(self._models.items())

        unloaded = []
        for name, unload_fn in models:
            try:
                unload_fn()
                unloaded.append(name)
            except Exception as e:
                warnings.warn(f"Failed to unload model {name}: {e}")

        if unloaded:
            logger.info(f"Idle timeout: Unloaded models: {', '.join(unloaded)}")
            for callback in self._on_unload_callbacks:
                try:
                    callback()
                except Exception:
                    pass

    def get_idle_time(self) -> float:
        """Get current idle time in seconds."""
        with self._activity_lock:
            return time.time() - self._last_activity

    def on_unload(self, callback: Callable[[], None]) -> None:
        """Register callback for when models are unloaded."""
        self._on_unload_callbacks.append(callback)

    def on_warning(self, callback: Callable[[float], None]) -> None:
        """Register callback for idle warning (receives seconds remaining)."""
        self._on_warning_callbacks.append(callback)
