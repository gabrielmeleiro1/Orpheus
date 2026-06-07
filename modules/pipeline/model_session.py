"""Model lifecycle session for pipeline stage collaborators."""

from __future__ import annotations

import logging
import threading

from .runtime import PipelineRuntimeProtocol

logger = logging.getLogger(__name__)


class ModelSession:
    """Owns warmup, cleanup, and idle unload for retained model runtimes."""

    def __init__(self, runtime: PipelineRuntimeProtocol) -> None:
        self.runtime = runtime

    def warmup_models(self) -> None:
        self._warmup_models()

    def cleanup_models(self, *, force: bool = False) -> None:
        self._cleanup_models(force=force)

    def cancel_tts_idle_unload(self) -> None:
        self._cancel_tts_idle_unload()

    def schedule_tts_idle_unload(self) -> None:
        self._schedule_tts_idle_unload()

    def _warmup_models(self) -> None:
        """
        Pre-load TTS and cleaner models before processing chapters.

        This ensures models are loaded once and reused across all chapters
        in sequential processing mode, avoiding per-chapter loading overhead.
        """
        from .model_lifecycle import warmup_models

        self._cancel_tts_idle_unload()
        warmup_models(self.runtime)

    def _cancel_tts_idle_unload(self) -> None:
        """Cancel any pending idle unload for a retained TTS engine."""
        with self.runtime.state.tts_idle_lock:
            timer = self.runtime.state.tts_idle_timer
            self.runtime.state.tts_idle_timer = None
        if timer is not None:
            timer.cancel()

    def _schedule_tts_idle_unload(self) -> None:
        """Unload a retained TTS engine after the configured idle timeout."""
        if self.runtime.state.tts_engine is None:
            return

        self._cancel_tts_idle_unload()
        timeout_seconds = float(
            getattr(self.runtime.config, "tts_idle_timeout_seconds", 300.0) or 0.0
        )
        if timeout_seconds <= 0:
            self._unload_idle_tts_engine()
            return

        timer = threading.Timer(timeout_seconds, self._unload_idle_tts_engine)
        timer.daemon = True
        with self.runtime.state.tts_idle_lock:
            self.runtime.state.tts_idle_timer = timer
        timer.start()

    def _unload_idle_tts_engine(self) -> None:
        """Unload retained TTS runtime once the pipeline has been idle long enough."""
        if self.runtime.is_running:
            return

        with self.runtime.state.tts_idle_lock:
            tts_engine = self.runtime.state.tts_engine
            self.runtime.state.tts_engine = None
            self.runtime.state.tts_idle_timer = None

        if tts_engine is None:
            return
        try:
            tts_engine.unload(keep_in_cache=False)
        except Exception:
            logger.debug("Failed to unload idle TTS engine", exc_info=True)

    def _cleanup_models(self, *, force: bool = False):
        """Unload cached models to free memory."""
        from .model_lifecycle import cleanup_models

        keep_tts_warm = cleanup_models(self.runtime, force=force)
        if keep_tts_warm:
            self._schedule_tts_idle_unload()
        else:
            self._cancel_tts_idle_unload()
