"""Reusable TTS engine session ownership for conversion jobs."""

from __future__ import annotations

import logging
import threading
from typing import Any

from modules.pipeline.models import PipelineConfig
from modules.tts.runtime_spec import TTSRuntimeSpec

logger = logging.getLogger(__name__)


class TtsEngineSession:
    """Owns the service-level hot TTS engine between conversion runs."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._engine: Any = None
        self._key: tuple[Any, ...] | None = None
        self._idle_timer: threading.Timer | None = None

    @staticmethod
    def cache_key(config: PipelineConfig) -> tuple[Any, ...] | None:
        if not bool(getattr(config, "keep_tts_engine_warm", True)):
            return None
        engine_name = str(getattr(config, "tts_engine", "") or "").strip().lower()
        if engine_name != "kokoro":
            return None
        runtime_spec = getattr(config, "tts_runtime", None)
        runtime_key = (
            runtime_spec.cache_key()
            if runtime_spec is not None
            else TTSRuntimeSpec.from_legacy(
                engine_name,
                getattr(config, "tts_quantization", None),
                model=getattr(config, "tts_model_name", None),
            ).cache_key()
        )
        return (
            runtime_key,
            int(getattr(config, "chunk_size", 700) or 700),
        )

    def attach(self, pipeline: Any) -> None:
        config = getattr(pipeline, "config", None)
        if config is None:
            return
        key = self.cache_key(config)
        if key is None:
            return
        with self._lock:
            if self._key != key or self._engine is None:
                return
            engine = self._engine
            self._engine = None
            self._key = None
        self._cancel_idle_unload()
        pipeline._tts_engine = engine  # noqa: SLF001

    def adopt(self, pipeline: Any) -> None:
        config = getattr(pipeline, "config", None)
        if config is None:
            return
        key = self.cache_key(config)
        engine = getattr(pipeline, "_tts_engine", None)
        if key is None or engine is None:
            return
        selected_chapters = int(getattr(pipeline, "_selected_parallel_chapters", 1) or 1)
        if selected_chapters > 1:
            return

        cancel_pipeline_timer = getattr(
            getattr(pipeline, "stages", None),
            "cancel_tts_idle_unload",
            None,
        )
        if callable(cancel_pipeline_timer):
            cancel_pipeline_timer()
        pipeline._tts_engine = None  # noqa: SLF001

        with self._lock:
            previous_engine = self._engine
            self._engine = engine
            self._key = key
        if previous_engine is not None and previous_engine is not engine:
            try:
                previous_engine.unload(keep_in_cache=False)
            except Exception:
                logger.debug("Failed to replace hot TTS engine", exc_info=True)

        self._schedule_idle_unload(
            float(getattr(config, "tts_idle_timeout_seconds", 300.0) or 0.0)
        )

    def release(self, pipeline: Any, *, cleanup_first: bool) -> None:
        if pipeline is None:
            return
        if cleanup_first:
            cleanup = getattr(getattr(pipeline, "stages", None), "cleanup_models", None)
            if callable(cleanup):
                cleanup()
        self.adopt(pipeline)

    def cleanup(self) -> None:
        self._unload_idle_engine()

    def _cancel_idle_unload(self) -> None:
        with self._lock:
            timer = self._idle_timer
            self._idle_timer = None
        if timer is not None:
            timer.cancel()

    def _schedule_idle_unload(self, timeout_seconds: float) -> None:
        self._cancel_idle_unload()
        if timeout_seconds <= 0:
            self._unload_idle_engine()
            return
        timer = threading.Timer(timeout_seconds, self._unload_idle_engine)
        timer.daemon = True
        with self._lock:
            self._idle_timer = timer
        timer.start()

    def _unload_idle_engine(self) -> None:
        with self._lock:
            engine = self._engine
            self._engine = None
            self._key = None
            self._idle_timer = None
        if engine is None:
            return
        try:
            engine.unload(keep_in_cache=False)
        except Exception:
            logger.debug("Failed to unload idle service TTS engine", exc_info=True)
