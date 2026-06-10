"""Pure worker configuration helpers for pipeline process workers."""

from __future__ import annotations

from typing import Any

from modules.tts.runtime_spec import TTSRuntimeSpec
from modules.tts.runtime_options import TTSRuntimeOptions

from .models import PipelineConfig


def _serialize_worker_model_fields(config: PipelineConfig) -> dict[str, Any]:
    """Serialize top-level TTS/cleaner fields shared with nested pipeline_config."""
    serialized_tts_runtime = (
        config.tts_runtime.to_dict() if config.tts_runtime is not None else None
    )
    serialized_tts_runtime_options = (
        config.tts_runtime_options.to_dict()
        if config.tts_runtime_options is not None
        else None
    )
    serialized_device_memory_policy = (
        config.device_memory_policy.to_dict()
        if config.device_memory_policy is not None
        else None
    )
    return {
        "tts_runtime": serialized_tts_runtime,
        "tts_runtime_options": serialized_tts_runtime_options,
        "device_memory_policy": serialized_device_memory_policy,
        "tts_engine": config.tts_engine,
        "tts_model_name": config.tts_model_name,
        "tts_quantization": config.tts_quantization,
        "voice": config.voice,
        "cleaner_model_name": config.cleaner_model_name,
        "cleaner_backend": config.cleaner_backend,
        "cleaner_cuda_device": config.cleaner_cuda_device,
        "cleaner_cuda_quantization": config.cleaner_cuda_quantization,
        "cleaner_api_base_url": config.cleaner_api_base_url,
        "cleaner_api_key_env": config.cleaner_api_key_env,
        "cleaner_api_key": config.cleaner_api_key,
        "cleaner_api_model": config.cleaner_api_model,
        "cleaner_api_global_concurrency": config.cleaner_api_global_concurrency,
        "cleaner_api_global_limiter": config.cleaner_api_global_limiter,
        "cleaner_skip_llm_when_not_needed": config.cleaner_skip_llm_when_not_needed,
    }


def _serialize_pipeline_config_dict(config: PipelineConfig) -> dict[str, Any]:
    """Serialize the nested pipeline_config block for worker-local execution."""
    return {
        **_serialize_worker_model_fields(config),
        "save_cleaning_debug": config.save_cleaning_debug,
        "speed": config.speed,
        "output_dir": config.output_dir,
        "temp_dir": config.temp_dir,
        "mp3_bitrate": config.mp3_bitrate,
        "emit_chapter_mp3_artifacts": config.emit_chapter_mp3_artifacts,
        "normalize_volume": config.normalize_volume,
        "target_dbfs": config.target_dbfs,
        "chunk_size": config.chunk_size,
        "enable_pipelining": config.enable_pipelining,
        "enable_chunk_overlap": config.enable_chunk_overlap,
        "enable_tts_file_batch_results": config.enable_tts_file_batch_results,
        "tts_file_batch_results_auto_threshold_chunks": (
            config.tts_file_batch_results_auto_threshold_chunks
        ),
        "keep_tts_engine_warm": False,
        "tts_idle_timeout_seconds": config.tts_idle_timeout_seconds,
        "chapter_limit": config.chapter_limit,
        "pre_chunk_document_cleanup": config.pre_chunk_document_cleanup,
        "artifact_gated_cleaning": config.artifact_gated_cleaning,
        "speech_normalization": config.speech_normalization,
    }


def build_worker_init_config(config: PipelineConfig) -> dict[str, Any]:
    """Serialize worker model settings used for per-process load and warm-up."""
    return {
        **_serialize_worker_model_fields(config),
        "pipeline_config": _serialize_pipeline_config_dict(config),
    }


def build_worker_pipeline_config(
    serialized_config: dict[str, Any] | PipelineConfig,
) -> PipelineConfig:
    """Reconstruct a worker-local config with parallel execution disabled."""
    if isinstance(serialized_config, PipelineConfig):
        return serialized_config

    serialized_tts_runtime = serialized_config.get("tts_runtime")
    tts_runtime = (
        TTSRuntimeSpec.from_serialized(serialized_tts_runtime)
        if serialized_tts_runtime is not None
        else None
    )
    serialized_tts_runtime_options = serialized_config.get("tts_runtime_options")
    tts_runtime_options = (
        TTSRuntimeOptions.from_serialized(serialized_tts_runtime_options)
        if serialized_tts_runtime_options is not None
        else None
    )
    from modules.tts.device_memory import device_memory_policy_from_serialized

    device_memory_policy = device_memory_policy_from_serialized(
        serialized_config.get("device_memory_policy")
    )
    return PipelineConfig(
        tts_engine=serialized_config["tts_engine"],
        tts_model_name=serialized_config["tts_model_name"],
        tts_quantization=serialized_config["tts_quantization"],
        tts_runtime=tts_runtime,
        tts_runtime_options=tts_runtime_options,
        device_memory_policy=device_memory_policy,
        cleaner_model_name=serialized_config["cleaner_model_name"],
        cleaner_backend=serialized_config["cleaner_backend"],
        cleaner_cuda_device=serialized_config.get("cleaner_cuda_device", "cuda"),
        cleaner_cuda_quantization=serialized_config.get(
            "cleaner_cuda_quantization",
            "4bit",
        ),
        cleaner_api_base_url=serialized_config["cleaner_api_base_url"],
        cleaner_api_key_env=serialized_config["cleaner_api_key_env"],
        cleaner_api_key=serialized_config.get("cleaner_api_key"),
        cleaner_api_model=serialized_config["cleaner_api_model"],
        cleaner_skip_llm_when_not_needed=serialized_config.get(
            "cleaner_skip_llm_when_not_needed",
            True,
        ),
        save_cleaning_debug=serialized_config["save_cleaning_debug"],
        voice=serialized_config["voice"],
        speed=serialized_config["speed"],
        output_dir=serialized_config["output_dir"],
        temp_dir=serialized_config["temp_dir"],
        mp3_bitrate=serialized_config["mp3_bitrate"],
        emit_chapter_mp3_artifacts=serialized_config.get(
            "emit_chapter_mp3_artifacts",
            False,
        ),
        normalize_volume=serialized_config["normalize_volume"],
        target_dbfs=serialized_config["target_dbfs"],
        chunk_size=serialized_config["chunk_size"],
        enable_parallel=False,
        enable_pipelining=serialized_config["enable_pipelining"],
        enable_chunk_overlap=serialized_config["enable_chunk_overlap"],
        enable_tts_file_batch_results=serialized_config.get(
            "enable_tts_file_batch_results",
            False,
        ),
        tts_file_batch_results_auto_threshold_chunks=serialized_config.get(
            "tts_file_batch_results_auto_threshold_chunks",
            64,
        ),
        keep_tts_engine_warm=serialized_config.get("keep_tts_engine_warm", False),
        tts_idle_timeout_seconds=serialized_config.get(
            "tts_idle_timeout_seconds",
            300.0,
        ),
        chapter_limit=serialized_config.get("chapter_limit"),
        cleaner_api_global_concurrency=serialized_config[
            "cleaner_api_global_concurrency"
        ],
        cleaner_api_global_limiter=serialized_config["cleaner_api_global_limiter"],
        shared_worker_pool_manager=None,
        pre_chunk_document_cleanup=serialized_config.get(
            "pre_chunk_document_cleanup",
            True,
        ),
        artifact_gated_cleaning=serialized_config.get(
            "artifact_gated_cleaning",
            True,
        ),
        speech_normalization=serialized_config.get("speech_normalization", False),
    )


def build_shared_pool_fingerprint(
    config: PipelineConfig,
    max_workers: int,
) -> tuple[Any, ...]:
    """Build a stable fingerprint for shared worker-pool reuse decisions."""
    tts_runtime = config.tts_runtime
    tts_runtime_key = (
        tts_runtime.cache_key()
        if tts_runtime is not None
        else TTSRuntimeSpec.from_legacy(
            config.tts_engine,
            config.tts_quantization,
            model=config.tts_model_name,
        ).cache_key()
    )
    return (
        tts_runtime_key,
        config.voice,
        config.cleaner_model_name,
        config.cleaner_backend,
        config.cleaner_cuda_device,
        config.cleaner_cuda_quantization,
        config.cleaner_api_base_url,
        config.cleaner_api_key_env,
        config.cleaner_api_key,
        config.cleaner_api_model,
        config.cleaner_skip_llm_when_not_needed,
        config.cleaner_api_global_concurrency,
        max_workers,
    )


__all__ = [
    "build_worker_init_config",
    "build_worker_pipeline_config",
    "build_shared_pool_fingerprint",
]
