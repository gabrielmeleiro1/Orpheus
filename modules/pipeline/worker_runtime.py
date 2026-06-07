"""Shared worker bootstrap/runtime helpers for process-backed chapter execution."""

from __future__ import annotations

import logging
import os
import signal
from typing import Any, Optional

from modules.pipeline.worker_config import build_worker_pipeline_config

logger = logging.getLogger(__name__)

_WORKER_TTS_ENGINE: Any = None
_WORKER_TEXT_CLEANER: Any = None
_WORKER_PIPELINE_CONFIG: Any = None
_WORKER_INIT_ERROR: Optional[str] = None


def _runtime_spec_from_worker_config(worker_init_config: dict[str, Any]):
    from modules.tts.runtime_spec import TTSRuntimeSpec

    serialized_runtime = worker_init_config.get("tts_runtime")
    if serialized_runtime is None:
        raise ValueError("worker_init_config must include serialized tts_runtime")
    return TTSRuntimeSpec.from_serialized(serialized_runtime)


def _worker_environment_from_config(
    worker_init_config: dict[str, Any],
    *,
    available_vram_gb: float,
    active_workers: int,
) -> dict[str, str]:
    from modules.tts.device_memory import device_memory_policy_from_serialized

    worker_environment: dict[str, str] = {}
    device_policy = device_memory_policy_from_serialized(
        worker_init_config.get("device_memory_policy")
    )
    if device_policy is not None:
        worker_environment.update(
            device_policy.configure_worker_environment(
                available_bytes=max(0, int(float(available_vram_gb) * 1024**3)),
                active_workers=max(1, int(active_workers or 1)),
            )
        )
    return worker_environment


def _init_worker(
    available_vram_gb: float,
    active_workers: int = 1,
    worker_init_config: Optional[dict[str, Any]] = None,
) -> None:
    """
    Initialize worker process.

    Called once per worker process in ProcessPoolExecutor.
    """
    global \
        _WORKER_TTS_ENGINE, \
        _WORKER_TEXT_CLEANER, \
        _WORKER_PIPELINE_CONFIG, \
        _WORKER_INIT_ERROR

    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:
        # Thread-backed fallback reuses this initializer, but signal handlers
        # can only be installed from the main interpreter thread.
        pass

    _WORKER_TTS_ENGINE = None
    _WORKER_TEXT_CLEANER = None
    _WORKER_PIPELINE_CONFIG = None
    _WORKER_INIT_ERROR = None

    if worker_init_config:
        try:
            from modules.tts.cleaner import CleanerConfig, TextCleaner
            from modules.tts.factory import TTSEngineFactory

            tts_warmup_error: Optional[str] = None
            runtime_spec = _runtime_spec_from_worker_config(worker_init_config)
            worker_environment = _worker_environment_from_config(
                worker_init_config,
                available_vram_gb=available_vram_gb,
                active_workers=active_workers,
            )
            for key, value in worker_environment.items():
                os.environ[str(key)] = str(value)
            tts_engine_name = runtime_spec.engine
            tts_kwargs = runtime_spec.factory_kwargs()
            tts_kwargs.update(
                {
                    "default_voice": worker_init_config["voice"],
                }
            )

            _WORKER_TTS_ENGINE = TTSEngineFactory.create(
                tts_engine_name,
                **tts_kwargs,
            )
            _WORKER_TTS_ENGINE.load()
            try:
                _WORKER_TTS_ENGINE.warmup(voice=worker_init_config["voice"])
            except Exception as exc:
                tts_warmup_error = str(exc)
                _WORKER_INIT_ERROR = tts_warmup_error
                logger.warning("Worker TTS warm-up failed: %s", exc)

            _WORKER_TEXT_CLEANER = TextCleaner(
                config=CleanerConfig(
                    model_name=worker_init_config["cleaner_model_name"],
                    backend=worker_init_config["cleaner_backend"],
                    api_base_url=worker_init_config["cleaner_api_base_url"],
                    api_key_env=worker_init_config["cleaner_api_key_env"],
                    api_key=worker_init_config.get("cleaner_api_key"),
                    api_model=worker_init_config["cleaner_api_model"],
                    api_batch_concurrency=worker_init_config.get(
                        "cleaner_api_global_concurrency", 4
                    ),
                    api_global_limiter=worker_init_config.get(
                        "cleaner_api_global_limiter"
                    ),
                    skip_llm_when_not_needed=worker_init_config.get(
                        "cleaner_skip_llm_when_not_needed", True
                    ),
                    cache_model=True,
                )
            )
            _WORKER_TEXT_CLEANER.load()
            if tts_warmup_error is None:
                logger.info("Worker warmed TTS model and loaded cleaner model")
            else:
                logger.info(
                    "Worker loaded TTS and cleaner models without TTS probe warm-up"
                )
        except Exception as exc:
            _WORKER_INIT_ERROR = str(exc)
            logger.warning(f"Worker model initialization failed: {exc}")

    # Store full pipeline config for reuse across chapter submissions.
    if worker_init_config and "pipeline_config" in worker_init_config:
        _WORKER_PIPELINE_CONFIG = build_worker_pipeline_config(
            worker_init_config["pipeline_config"]
        )

    logger.debug(
        "Worker initialized with device memory share: %.2fGB",
        available_vram_gb / max(1, int(active_workers)),
    )


def get_worker_cached_models() -> tuple[Any, Any]:
    """Return process-local cached (tts_engine, text_cleaner) models."""
    return _WORKER_TTS_ENGINE, _WORKER_TEXT_CLEANER


def get_worker_cached_config() -> Any:
    """Return process-local cached PipelineConfig, or None if unavailable."""
    return _WORKER_PIPELINE_CONFIG


def get_worker_init_error() -> Optional[str]:
    """Return worker initialization error if worker load or warm-up failed."""
    return _WORKER_INIT_ERROR
