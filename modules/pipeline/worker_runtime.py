"""Shared worker bootstrap/runtime helpers for process-backed chapter execution."""

from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import threading
from typing import Any, Optional

from modules.pipeline.worker_config import build_worker_pipeline_config
from modules.pipeline.worker_errors import (
    WorkerStartupStatus,
    classify_worker_startup_error,
    is_warmup_failure_non_fatal,
    startup_status_from_failure,
)

logger = logging.getLogger(__name__)

_WORKER_TTS_ENGINE: Any = None
_WORKER_TEXT_CLEANER: Any = None
_WORKER_PIPELINE_CONFIG: Any = None
_WORKER_INIT_STATUS: Optional[WorkerStartupStatus] = None
_WORKER_PROVIDER: str = "unknown"
_WORKER_DEVICE: Optional[str] = None
_WORKER_STARTUP_BARRIER: Any = None


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


def _cleaner_startup_identity(
    worker_init_config: dict[str, Any],
) -> tuple[str, Optional[str]]:
    backend = str(worker_init_config.get("cleaner_backend") or "unknown").strip().lower()
    device: Optional[str] = None
    if backend == "cuda":
        device = worker_init_config.get("cleaner_cuda_device")
    return backend, device


def _record_worker_init_failure(
    exc: BaseException,
    *,
    provider: str,
    device: Optional[str],
    phase: str,
) -> None:
    global _WORKER_INIT_STATUS

    _WORKER_INIT_STATUS = classify_worker_startup_error(
        exc,
        provider=provider,
        device=device,
        phase=phase,
    )


_PARENT_GONE_EXIT_CODE = 70


def _start_parent_watchdog() -> None:
    """Exit this worker as soon as the process that spawned it is gone.

    A pool worker otherwise outlives a crashed or killed backend: it blocks on
    its call queue forever, keeping its models (and their device memory)
    resident. Thread-backed workers share the parent process and need none.
    """
    parent = multiprocessing.parent_process()
    if parent is None:
        return

    def wait_for_parent_exit() -> None:
        try:
            parent.join()
        finally:
            os._exit(_PARENT_GONE_EXIT_CODE)

    threading.Thread(
        target=wait_for_parent_exit, name="worker-parent-watchdog", daemon=True,
    ).start()


def _init_worker(
    available_vram_gb: float,
    active_workers: int = 1,
    worker_init_config: Optional[dict[str, Any]] = None,
    startup_barrier: Any = None,
) -> None:
    """
    Initialize worker process.

    Called once per worker process in ProcessPoolExecutor.
    """
    global \
        _WORKER_TTS_ENGINE, \
        _WORKER_TEXT_CLEANER, \
        _WORKER_PIPELINE_CONFIG, \
        _WORKER_INIT_STATUS, \
        _WORKER_PROVIDER, \
        _WORKER_DEVICE, \
        _WORKER_STARTUP_BARRIER

    _WORKER_STARTUP_BARRIER = startup_barrier
    _start_parent_watchdog()
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:
        # Thread-backed fallback reuses this initializer, but signal handlers
        # can only be installed from the main interpreter thread.
        pass

    _WORKER_TTS_ENGINE = None
    _WORKER_TEXT_CLEANER = None
    _WORKER_PIPELINE_CONFIG = None
    _WORKER_INIT_STATUS = None
    _WORKER_PROVIDER = "unknown"
    _WORKER_DEVICE = None

    if worker_init_config:
        runtime_spec = _runtime_spec_from_worker_config(worker_init_config)
        _WORKER_PROVIDER = runtime_spec.provider
        _WORKER_DEVICE = runtime_spec.device
        try:
            from modules.tts.cleaner import TextCleaner
            from modules.tts.cleaner_config import build_cleaner_config_from_worker_init
            from modules.tts.factory import TTSEngineFactory

            tts_warmup_failed = False
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
                    "max_chunk_tokens": (
                        worker_init_config.get("pipeline_config") or {}
                    ).get("chunk_size", 700),
                }
            )

            try:
                _WORKER_TTS_ENGINE = TTSEngineFactory.create(
                    tts_engine_name,
                    **tts_kwargs,
                )
                _WORKER_TTS_ENGINE.load()
            except Exception as exc:
                _WORKER_TTS_ENGINE = None
                _record_worker_init_failure(
                    exc,
                    provider=runtime_spec.provider,
                    device=runtime_spec.device,
                    phase="tts_load",
                )
                logger.warning("Worker TTS model load failed: %s", exc)
            else:
                try:
                    _WORKER_TTS_ENGINE.warmup(voice=worker_init_config["voice"])
                except Exception as exc:
                    if is_warmup_failure_non_fatal(
                        exc,
                        provider=runtime_spec.provider,
                    ):
                        tts_warmup_failed = True
                        logger.warning("Worker TTS warm-up failed: %s", exc)
                    else:
                        _record_worker_init_failure(
                            exc,
                            provider=runtime_spec.provider,
                            device=runtime_spec.device,
                            phase="tts_warmup",
                        )
                        logger.warning("Worker TTS warm-up failed fatally: %s", exc)

                if _WORKER_INIT_STATUS is None:
                    cleaner_provider, cleaner_device = _cleaner_startup_identity(
                        worker_init_config,
                    )
                    try:
                        cleaner_config = build_cleaner_config_from_worker_init(
                            worker_init_config,
                        )
                        _WORKER_TEXT_CLEANER = TextCleaner(config=cleaner_config)
                        # Mirror sequential warmup: rule-only gating loads the
                        # model on the first chunk that needs LLM cleanup.
                        if cleaner_config.backend != "basic" and not (
                            cleaner_config.skip_llm_when_not_needed
                        ):
                            _WORKER_TEXT_CLEANER.load()
                    except Exception as exc:
                        _record_worker_init_failure(
                            exc,
                            provider=cleaner_provider,
                            device=cleaner_device,
                            phase="cleaner_load",
                        )
                        logger.warning("Worker cleaner model load failed: %s", exc)

                if _WORKER_INIT_STATUS is None:
                    if not tts_warmup_failed:
                        logger.info("Worker warmed TTS model and loaded cleaner model")
                    else:
                        logger.info(
                            "Worker loaded TTS and cleaner models without TTS probe warm-up"
                        )
        except Exception as exc:
            if _WORKER_INIT_STATUS is None:
                _record_worker_init_failure(
                    exc,
                    provider=_WORKER_PROVIDER,
                    device=_WORKER_DEVICE,
                    phase="worker_init",
                )
                logger.warning("Worker model initialization failed: %s", exc)

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


def get_worker_startup_status() -> WorkerStartupStatus:
    """Return the worker startup status for startup probes.

    A process pool submits one probe per worker. Waiting on the startup barrier
    keeps the first ready worker from answering every probe while its siblings
    are still loading models. A worker whose own initialization failed breaks
    the barrier instead: startup has failed whatever its siblings report, so
    it answers at once with its real failure and releases any waiting worker.
    """
    if _WORKER_STARTUP_BARRIER is not None:
        if _WORKER_INIT_STATUS is not None:
            _WORKER_STARTUP_BARRIER.abort()
        else:
            try:
                _WORKER_STARTUP_BARRIER.wait()
            except threading.BrokenBarrierError:
                pass
    if _WORKER_INIT_STATUS is None:
        return WorkerStartupStatus(
            success=True,
            provider=_WORKER_PROVIDER,
            device=_WORKER_DEVICE,
            error_type=None,
            message=None,
        )
    return startup_status_from_failure(_WORKER_INIT_STATUS)
