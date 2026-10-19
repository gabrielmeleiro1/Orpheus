"""Pipeline model lifecycle helpers extracted from orchestrator."""

from __future__ import annotations

import logging
from typing import Any

from modules.tts.cleaner_memory import (
    CLEANER_CUDA_LOAD_HEADROOM_BYTES,
    estimate_cleaner_cuda_bytes,
)
from modules.tts.device_memory import CUDADeviceMemoryPolicy
from modules.tts.factory import TTSEngineFactory

logger = logging.getLogger(__name__)


def _tts_strategy_kwargs(runtime: Any) -> dict[str, Any]:
    """Build generic strategy kwargs from pipeline configuration."""
    kwargs = {
        "quantization": getattr(runtime.config, "tts_quantization", "bf16"),
        "default_voice": getattr(runtime.config, "voice", None),
        "max_chunk_tokens": getattr(runtime.config, "chunk_size", 700),
    }
    runtime_spec = getattr(runtime.config, "tts_runtime", None)
    if runtime_spec is not None:
        kwargs.update(runtime_spec.factory_kwargs())
    return kwargs


def _create_tts_strategy(runtime: Any):
    """Create a strategy instance for the configured TTS engine."""
    engine_name = getattr(runtime.config, "tts_engine", "kokoro")
    return TTSEngineFactory.create(
        engine_name,
        **_tts_strategy_kwargs(runtime),
    )


def _tts_runtime_label(runtime: Any) -> str:
    """Human-readable runtime label for logs."""
    runtime_spec = getattr(runtime.config, "tts_runtime", None)
    if runtime_spec is not None:
        return runtime_spec.label()
    engine_name = getattr(runtime.config, "tts_engine", "kokoro")
    model_name = getattr(runtime.config, "tts_model_name", None)
    quantization = getattr(runtime.config, "tts_quantization", None)
    if model_name and quantization:
        return f"{engine_name}:{model_name}-{quantization}"
    if model_name:
        return f"{engine_name}:{model_name}"
    return str(engine_name)


def ensure_chapter_tts_engine(runtime: Any):
    """Create or reuse the chapter TTS strategy based on execution mode."""
    if not runtime.state.parallel_execution_active:
        if runtime.state.tts_engine is None:
            runtime.state.tts_engine = _create_tts_strategy(runtime)
            runtime.state.tts_engine.load()
        return runtime.state.tts_engine

    tts_engine = _create_tts_strategy(runtime)
    tts_engine.load()
    runtime.state.tts_engine = tts_engine
    return tts_engine


def _cleaner_config_from_runtime(runtime: Any):
    """Build CleanerConfig from pipeline runtime settings."""
    from modules.tts.cleaner_config import build_cleaner_config_from_pipeline

    return build_cleaner_config_from_pipeline(runtime.config)


def _maybe_warn_cleaner_cuda_memory_low(runtime: Any, cleaner_config: Any) -> None:
    """Log when live CUDA free memory may be insufficient for cleaner load."""
    if str(getattr(cleaner_config, "backend", "") or "").strip().lower() != "cuda":
        return

    estimated_bytes = estimate_cleaner_cuda_bytes(
        getattr(cleaner_config, "model_name", ""),
        getattr(cleaner_config, "cuda_quantization", "4bit"),
    )
    required_bytes = estimated_bytes + CLEANER_CUDA_LOAD_HEADROOM_BYTES
    snapshot = CUDADeviceMemoryPolicy(
        device=getattr(cleaner_config, "cuda_device", None),
    ).sample()
    if snapshot is None:
        return

    if snapshot.available_bytes < required_bytes:
        gib = 1024**3
        runtime.log_verbose(
            "[WARMUP] CUDA memory may be insufficient for cleaner load "
            f"(available={snapshot.available_bytes / gib:.1f} GiB, "
            f"estimate={estimated_bytes / gib:.1f} GiB + "
            f"headroom={CLEANER_CUDA_LOAD_HEADROOM_BYTES / gib:.1f} GiB)",
            "warning",
        )


def _should_defer_cleaner_load(runtime: Any) -> bool:
    """Return True when rule-only gating can avoid cleaner model load entirely."""
    return bool(getattr(runtime.config, "cleaner_skip_llm_when_not_needed", False))


def create_text_cleaner_instance(runtime: Any):
    """Create and load a text cleaner instance for current pipeline config."""
    from modules.tts.cleaner import TextCleaner

    cleaner_config = _cleaner_config_from_runtime(runtime)
    cleaner = TextCleaner(config=cleaner_config)
    if cleaner_config.backend == "basic":
        runtime.log_verbose("[CLEANER] Basic cleanup ready (no model)", "success")
        return cleaner
    if _should_defer_cleaner_load(runtime):
        runtime.log_verbose(
            "[CLEANER] Deferring model load until a chunk requires LLM cleanup",
            "info",
        )
        return cleaner

    _maybe_warn_cleaner_cuda_memory_low(runtime, cleaner_config)
    cleaner.load()
    runtime.log_verbose("[CLEANER] Model loaded - Cleaning text chunks...", "success")
    return cleaner


def get_text_cleaner_for_chapter(runtime: Any):
    """
    Return cleaner for chapter processing.

    In sequential mode the cleaner is cached; in parallel mode each worker uses
    a fresh cleaner instance.
    """
    if getattr(runtime.config, "cleaner_backend", "") == "basic":
        runtime.log_verbose("[CLEANER] Using rule-only cleanup (no model)", "info")
        if runtime.state.parallel_execution_active:
            return create_text_cleaner_instance(runtime)
        if runtime.state.text_cleaner is None:
            runtime.state.text_cleaner = create_text_cleaner_instance(runtime)
        return runtime.state.text_cleaner

    if not runtime.state.parallel_execution_active:
        if runtime.state.text_cleaner is None:
            runtime.log_verbose(
                f"[CLEANER] Loading Text Cleaner model: {runtime.config.cleaner_model_name}",
                "process",
            )
            runtime.state.text_cleaner = create_text_cleaner_instance(runtime)
        else:
            runtime.log_verbose("[CLEANER] Using cached Text Cleaner model", "success")
        return runtime.state.text_cleaner

    runtime.log_verbose(
        f"[CLEANER] Loading Text Cleaner model: {runtime.config.cleaner_model_name}",
        "process",
    )
    return create_text_cleaner_instance(runtime)


def warmup_models(runtime: Any) -> None:
    """
    Pre-load TTS and cleaner models before processing chapters.

    This ensures models are loaded once and reused across all chapters
    in sequential processing mode, avoiding per-chapter loading overhead.
    TTS warm-up performs a real probe synthesis after the model is loaded.
    """
    runtime.log_verbose("[WARMUP] Pre-loading models for sequential processing...", "process")
    memory_policy = getattr(runtime.config, "memory_policy", None)
    record_baseline = getattr(memory_policy, "record_baseline", None)
    record_warmup = getattr(memory_policy, "record_warmup", None)
    if callable(record_baseline):
        record_baseline()

    if runtime.state.tts_engine is None:
        runtime.log_verbose(
            f"[WARMUP] Loading TTS runtime: {_tts_runtime_label(runtime)}",
            "process",
        )
        runtime.state.tts_engine = _create_tts_strategy(runtime)
    if getattr(runtime.state.tts_engine, "is_loaded", False) is True:
        runtime.log_verbose("[WARMUP] Reusing warm TTS runtime", "success")
    else:
        runtime.state.tts_engine.load()
        runtime.state.tts_engine.warmup(voice=runtime.config.voice)
        runtime.log_verbose("[WARMUP] TTS runtime loaded and warmed", "success")

    if runtime.state.text_cleaner is None:
        from modules.tts.cleaner import TextCleaner

        cleaner_config = _cleaner_config_from_runtime(runtime)
        runtime.state.text_cleaner = TextCleaner(config=cleaner_config)
        if cleaner_config.backend == "basic":
            runtime.log_verbose("[WARMUP] Basic cleanup ready (no model)", "success")
        elif _should_defer_cleaner_load(runtime):
            runtime.log_verbose(
                "[WARMUP] Text Cleaner model load deferred until LLM cleanup is needed",
                "info",
            )
        else:
            runtime.log_verbose(
                f"[WARMUP] Loading Text Cleaner model: {runtime.config.cleaner_model_name}",
                "process",
            )
            _maybe_warn_cleaner_cuda_memory_low(runtime, cleaner_config)
            runtime.state.text_cleaner.load()
            runtime.log_verbose("[WARMUP] Text Cleaner model loaded and cached", "success")

    if callable(record_warmup):
        record_warmup()
    runtime.log_verbose("[WARMUP] Model warm-up complete - ready for chapter processing", "success")


def _should_keep_tts_engine_warm(runtime: Any) -> bool:
    if runtime.state.parallel_execution_active:
        return False
    if (
        bool(getattr(runtime.config, "enable_parallel", False))
        and int(runtime.state.selected_parallel_chapters or 1) > 1
    ):
        return False
    engine_name = str(getattr(runtime.config, "tts_engine", "") or "").strip().lower()
    if engine_name != "kokoro":
        return False
    return bool(getattr(runtime.config, "keep_tts_engine_warm", True))


def cleanup_models(runtime: Any, *, force: bool = False) -> bool:
    """Unload cached models to free memory."""
    keep_tts_warm = (
        not force
        and runtime.state.tts_engine is not None
        and _should_keep_tts_engine_warm(runtime)
    )

    # Unload failures must not abort run teardown, but they can leave device
    # memory allocated, so keep them visible in the backend log.
    if not keep_tts_warm:
        try:
            if runtime.state.tts_engine is not None:
                runtime.state.tts_engine.unload(keep_in_cache=False)
        except Exception:
            logger.warning("TTS engine unload failed", exc_info=True)
        runtime.state.tts_engine = None

    try:
        if runtime.state.text_cleaner is not None:
            runtime.state.text_cleaner.unload(keep_in_cache=True)
    except Exception:
        logger.warning("Text cleaner unload failed", exc_info=True)

    runtime.state.text_cleaner = None
    return keep_tts_warm
