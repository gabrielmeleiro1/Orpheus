"""Pipeline model lifecycle helpers extracted from orchestrator."""

from __future__ import annotations

from typing import Any

from modules.tts.factory import TTSEngineFactory


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
    from modules.tts.cleaner import CleanerConfig

    config = runtime.config
    return CleanerConfig(
        model_name=config.cleaner_model_name,
        backend=config.cleaner_backend,
        cuda_device=config.cleaner_cuda_device,
        cuda_quantization=config.cleaner_cuda_quantization,
        api_base_url=config.cleaner_api_base_url,
        api_key_env=config.cleaner_api_key_env,
        api_key=config.cleaner_api_key,
        api_model=config.cleaner_api_model,
        api_batch_concurrency=config.cleaner_api_global_concurrency,
        api_global_limiter=config.cleaner_api_global_limiter,
        skip_llm_when_not_needed=config.cleaner_skip_llm_when_not_needed,
        cache_model=True,
    )


def create_text_cleaner_instance(runtime: Any):
    """Create and load a text cleaner instance for current pipeline config."""
    from modules.tts.cleaner import TextCleaner

    cleaner = TextCleaner(config=_cleaner_config_from_runtime(runtime))
    cleaner.load()
    runtime.log_verbose("[CLEANER] Model loaded - Cleaning text chunks...", "success")
    return cleaner


def get_text_cleaner_for_chapter(runtime: Any):
    """
    Return cleaner for chapter processing.

    In sequential mode the cleaner is cached; in parallel mode each worker uses
    a fresh cleaner instance.
    """
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

        runtime.log_verbose(
            f"[WARMUP] Loading Text Cleaner model: {runtime.config.cleaner_model_name}",
            "process",
        )
        runtime.state.text_cleaner = TextCleaner(
            config=_cleaner_config_from_runtime(runtime),
        )
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

    if not keep_tts_warm:
        try:
            if runtime.state.tts_engine is not None:
                runtime.state.tts_engine.unload(keep_in_cache=False)
        except Exception:
            pass
        runtime.state.tts_engine = None

    try:
        if runtime.state.text_cleaner is not None:
            runtime.state.text_cleaner.unload(keep_in_cache=True)
    except Exception:
        pass

    runtime.state.text_cleaner = None
    return keep_tts_warm
