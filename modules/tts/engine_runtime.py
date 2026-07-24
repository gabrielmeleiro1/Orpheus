"""Runtime and lifecycle helpers for the legacy TTS engine facade."""

from __future__ import annotations

import logging
import time
from typing import Optional

from . import mlx_compat as mlx_compat_backend
from . import model_cache as model_cache_backend
from .quantization import normalize_quantization

logger = logging.getLogger(__name__)


def _get_cached_model(model_path: str):
    """Get model from cache if available."""
    return model_cache_backend.get_cached_model(model_path)


def _set_cached_model(model_path: str, model: object) -> None:
    """Cache a model for reuse."""
    model_cache_backend.set_cached_model(model_path, model)


def get_model_path(config) -> str:  # noqa: ANN001
    """Resolve the current Kokoro model path from config."""
    base = config.model_name
    quantization = normalize_quantization(getattr(config, "quantization", "bf16"))
    if quantization == "bf16":
        return f"{base}-bf16"
    if quantization == "6bit":
        return f"{base}-6bit"
    if quantization == "8bit":
        return f"{base}-8bit"
    if quantization == "4bit":
        return f"{base}-4bit"
    return f"{base}-bf16"


def get_model_cache_key(config) -> str:  # noqa: ANN001
    """Resolve the model cache key for the concrete runtime."""
    runtime_cache_key = getattr(config, "runtime_cache_key", None)
    if runtime_cache_key:
        return str(runtime_cache_key)
    return get_model_path(config)


def _get_override_cache_key(
    canonical_key: str,
    model_path: str,
    quantization: str,
) -> str:
    """Keep an explicit runtime override private to its effective variant."""
    return repr((canonical_key, str(model_path), normalize_quantization(quantization)))


def _infer_quantization_from_model_path(model_path: object) -> str | None:
    normalized_path = str(model_path or "").strip().lower()
    for quantization in ("bf16", "4bit", "6bit", "8bit"):
        if normalized_path.endswith(f"-{quantization}"):
            return quantization
    return None


def load_model(engine, quantization: Optional[str] = None) -> None:  # noqa: ANN001
    """Load the configured model, optionally updating quantization first."""
    if engine._model_loaded:  # noqa: SLF001
        return

    canonical_cache_key = getattr(engine, "_canonical_cache_key", None)  # noqa: SLF001
    if canonical_cache_key is None:
        canonical_cache_key = getattr(engine, "_model_cache_key", None)  # noqa: SLF001
        if canonical_cache_key is None:
            canonical_cache_key = get_model_cache_key(engine.config)
        engine._canonical_cache_key = canonical_cache_key  # noqa: SLF001

    canonical_quantization = getattr(engine, "_canonical_quantization", None)  # noqa: SLF001
    if canonical_quantization is None:
        canonical_quantization = _infer_quantization_from_model_path(
            getattr(engine, "_model_path", None)
        ) or normalize_quantization(getattr(engine.config, "quantization", "bf16"))
        engine._canonical_quantization = canonical_quantization  # noqa: SLF001
    canonical_model_path = getattr(engine, "_canonical_model_path", None)  # noqa: SLF001
    if canonical_model_path is None:
        canonical_model_path = getattr(engine, "_model_path", None)  # noqa: SLF001
        engine._canonical_model_path = canonical_model_path  # noqa: SLF001

    if quantization is not None:
        engine.config.quantization = quantization
    effective_quantization = normalize_quantization(
        getattr(engine.config, "quantization", "bf16")
    )
    engine._model_path = engine._get_model_path()  # noqa: SLF001
    if (
        effective_quantization != canonical_quantization
        or engine._model_path != canonical_model_path  # noqa: SLF001
    ):
        engine._model_cache_key = _get_override_cache_key(  # noqa: SLF001
            canonical_cache_key,
            engine._model_path,  # noqa: SLF001
            effective_quantization,
        )
    else:
        engine._model_cache_key = canonical_cache_key  # noqa: SLF001

    cached_model = _get_cached_model(engine._model_cache_key)  # noqa: SLF001
    if cached_model is not None:
        engine._model = cached_model  # noqa: SLF001
        engine._model_loaded = True  # noqa: SLF001
        logger.info("TTS Engine loaded from cache: %s", engine._model_cache_key)  # noqa: SLF001
        return

    engine._model = mlx_compat_backend.load_tts_model(engine._model_path)  # noqa: SLF001
    engine._model_loaded = True  # noqa: SLF001
    logger.info("TTS Engine loaded: %s", engine._model_path)  # noqa: SLF001


def warmup_model(
    engine,  # noqa: ANN001
    voice: Optional[str] = None,
    speed: float = 1.0,
    probe_text: str = "warmup check",
) -> None:
    """Prime the first synthesis path without changing public load semantics."""
    if engine._warmup_complete:  # noqa: SLF001
        return

    if not engine._model_loaded:  # noqa: SLF001
        engine.load_model()

    started_at = time.perf_counter()
    probe_voice = voice or engine.config.default_voice
    engine._synthesize_to_temp_audio(probe_text, probe_voice, speed)  # noqa: SLF001
    engine._warmup_complete = True  # noqa: SLF001
    engine._last_warmup_ms = round((time.perf_counter() - started_at) * 1000, 2)  # noqa: SLF001


def unload_model(engine, keep_in_cache: bool = True) -> None:  # noqa: ANN001
    """Unload the current model and reset runtime state."""
    if not keep_in_cache:
        cache_key = getattr(engine, "_model_cache_key", None)
        owned_model = getattr(engine, "_model", None)
        if owned_model is None:
            owned_model = getattr(engine, "_last_cached_model", None)
        if cache_key is not None and owned_model is not None:
            model_cache_backend.discard_cached_model_if_same(
                cache_key,
                owned_model,
                mx_module=mlx_compat_backend.mx,
                mlx_available=mlx_compat_backend.MLX_AVAILABLE,
            )
        else:
            # Legacy/detached engines without ownership metadata still need a
            # fail-safe hard clear.
            model_cache_backend.clear_model_cache(
                mx_module=mlx_compat_backend.mx,
                mlx_available=mlx_compat_backend.MLX_AVAILABLE,
            )
        engine._last_cached_model = None  # noqa: SLF001

    if not engine._model_loaded:  # noqa: SLF001
        return

    if keep_in_cache and engine._model is not None:  # noqa: SLF001
        _set_cached_model(engine._model_cache_key, engine._model)  # noqa: SLF001
        engine._last_cached_model = engine._model  # noqa: SLF001

    engine._model = None  # noqa: SLF001
    engine._model_loaded = False  # noqa: SLF001
    engine._warmup_complete = False  # noqa: SLF001
    engine._last_warmup_ms = None  # noqa: SLF001

    logger.info("TTS Engine unloaded (cached: %s)", keep_in_cache)


def get_metrics(engine) -> dict[str, float | int | None]:  # noqa: ANN001
    """Return runtime performance counters for observability."""
    now = time.time()
    elapsed = max(now - engine._engine_start_time, 1e-6)  # noqa: SLF001
    startup_ms: Optional[float] = None
    if engine._first_audio_time is not None:  # noqa: SLF001
        startup_ms = (engine._first_audio_time - engine._engine_start_time) * 1000  # noqa: SLF001

    peak_mb: Optional[float] = None
    if engine._memory_manager and hasattr(engine._memory_manager, "vram_monitor"):  # noqa: SLF001
        try:
            snapshot = engine._memory_manager.vram_monitor.get_current_usage()  # noqa: SLF001
            if snapshot is not None:
                peak_mb = snapshot.peak_mb
        except Exception:
            peak_mb = None

    return {
        "startup_to_first_audio_ms": startup_ms,
        "chunks_processed": engine._total_chunks_processed,  # noqa: SLF001
        "chars_processed": engine._total_chars_processed,  # noqa: SLF001
        "chunks_per_second": engine._total_chunks_processed / elapsed,  # noqa: SLF001
        "chars_per_second": engine._total_chars_processed / elapsed,  # noqa: SLF001
        "average_chunk_time_ms": engine.average_chunk_time_ms,
        "retry_count": engine._retry_count,  # noqa: SLF001
        "batch_pause_total_ms": engine._batch_pause_total_ms,  # noqa: SLF001
        "peak_memory_mb": peak_mb,
        "warmup_complete": engine._warmup_complete,  # noqa: SLF001
        "last_warmup_ms": engine._last_warmup_ms,  # noqa: SLF001
    }
